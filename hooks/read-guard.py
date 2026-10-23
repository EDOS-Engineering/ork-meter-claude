"""PreToolUse hook for Read: page a whole-file Read of a large text file.

When a whole-file text Read exceeds the threshold, page its first 300 lines
(``ORK_METER_READ_GUARD_LINES``, the ``read_guard`` lever's ``lines`` tunable).
For parseable Python, use a 48-line exact page plus a bounded AST symbol map
when that reduces total delivered bytes. Exact ranges stay available.

With Read compaction on (``ORK_METER_READ_COMPACT=on``, opt-in since #316), a Read
the guard would page is let through whole when it is over the token
threshold (``ORK_METER_READ_GUARD_TOKENS``) and small enough for Claude Code
to deliver whole (``deferred``): dedupe-reads.py then compacts the result,
and pages it the same way only when the compacted text still doesn't fit.

Left alone: calls that set ``offset``, ``limit``, ``pages`` or
``allow_large``; files under the threshold; files with no more lines than
the page; files that look binary (by extension, or a NUL byte in the first
8 KiB); anything that isn't a readable regular file, including a path
that doesn't exist or text that isn't UTF-8. Those are the model's or the
file's problem, not the guard's: they pass through without counting as a
reducer fault, so routine wrong guesses can't trip the circuit breaker
(#128). Runs through hooks/lib/hook.py: nothing is printed on any other
error or when ``ORK_METER_TRIM=off`` or ``ORK_METER_READ_GUARD=off``.
"""

import io
import os
import stat
import time

import circuit
import features
import hook
import ledger
import read_compact
import read_tool
import session
import token_gate

EVENTS = ("PreToolUse",)
FEATURE = "read_guard"
REDUCER = "read-guard"
LIMIT = 300
# The Python outline (hooks/lib/read_tool.py) is shared with dedupe-reads.py,
# whose fallback after a compaction that doesn't fit pages the same way.
OUTLINE_LIMIT = read_tool.OUTLINE_LIMIT
OUTLINE_MIN_FILE = read_tool.OUTLINE_MIN_FILE
OUTLINE_MAX_FILE = read_tool.OUTLINE_MAX_FILE
CHEAP_CODE_BYTES = read_tool.CHEAP_CODE_BYTES
code_bytes = read_tool.code_bytes
python_outline = read_tool.python_outline
# Read compaction runs on the result (dedupe-reads.py), so the guard lets
# through whole a Read it would page when compaction may make it fit and
# Claude Code delivers it whole: over the token threshold, at most
# read_compact.DEFER_MAX_TOKENS rendered, and at most this many bytes.
# Claude Code checks a whole-file Read against its 25,000-token cap with a
# bytes-per-token estimate first (bytes / 4 in the localhost fixture, which
# has no token counting: a 109,000-byte text file was "27250 tokens") and
# counts with the API only above a quarter of the cap, so the byte bound is
# the deferral ceiling at 4 bytes per token. JSON is never compacted
# (read_compact.compactable), so it is never let through for it.
DEFER_MAX_BYTES = 4 * read_compact.DEFER_MAX_TOKENS
SNIFF = 8192
TEXT_MAX = 2 * 1024 * 1024           # characters the admission check reads; a longer file passes through
MAX_COUNT_BYTES = 32 * 1024 * 1024   # stop counting lines past this; say "more than"
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff", ".heic", ".svgz",
    ".pdf", ".ipynb", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".7z", ".rar", ".tar", ".jar", ".war",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".m4a", ".mov", ".avi", ".mkv", ".wav", ".ogg",
    ".wasm", ".so", ".dylib", ".dll", ".exe", ".o", ".a", ".class", ".pyc", ".pyo", ".bin", ".dat",
    ".sqlite", ".sqlite3", ".db", ".parquet", ".pkl", ".pickle", ".npy", ".npz", ".h5", ".hdf5",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".dmg", ".iso", ".img", ".psd", ".ai",
}


def count_lines(path, deadline=None, limit=LIMIT, data=None):
    """(bytes of the first ``limit`` lines or None, total lines or None, exact),
    of ``data`` when the caller has read the file already."""
    first_bytes = None
    newlines = 0
    read = 0
    exact = True
    ends_with_newline = False
    with open(path, "rb") if data is None else io.BytesIO(data) as fh:
        while True:
            if read >= MAX_COUNT_BYTES or (deadline is not None and time.monotonic() >= deadline):
                exact = False
                break
            chunk = fh.read(min(1024 * 1024, MAX_COUNT_BYTES - read))
            if not chunk:
                break
            ends_with_newline = chunk.endswith(b"\n")
            if first_bytes is None:
                pos = 0
                while True:
                    idx = chunk.find(b"\n", pos)
                    if idx < 0:
                        break
                    newlines += 1
                    pos = idx + 1
                    if newlines == limit:
                        first_bytes = read + pos
                        break
                if first_bytes is None:
                    read += len(chunk)
                    continue
                newlines += chunk.count(b"\n", pos)
            else:
                newlines += chunk.count(b"\n")
            read += len(chunk)
            if read >= MAX_COUNT_BYTES:
                exact = False
                break
    return first_bytes, newlines + (1 if exact and not ends_with_newline else 0), exact


def handle(ctx):
    try:
        return guard(ctx)
    except FileUnavailable:
        return None


class FileUnavailable(Exception):
    """The file is missing, unreadable or not UTF-8: pass through, not a fault."""


def file_access(action, *args):
    try:
        return action(*args)
    except (OSError, UnicodeError) as exc:
        raise FileUnavailable() from exc


def read_prefix(path, limit, binary=True):
    with open(path, "rb") if binary else open(path, encoding="utf-8") as stream:
        return stream.read(limit)


def decode(raw):
    """``raw`` as text read in text mode: UTF-8, universal newlines."""
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def deferred(ctx, raw, extension):
    """This Read is left whole for Read compaction (hooks/lib/read_compact.py)
    on its result: compaction is on, its hook (dedupe-reads) and the Write
    guard (write-guard) aren't stopped by the circuit breaker, the file isn't
    JSON, and the text, rendered as Claude Code numbers it,
    is over the token threshold and no larger than ``DEFER_MAX_TOKENS``, and
    the file is within ``DEFER_MAX_BYTES``, so Claude Code itself delivers it
    whole. Anything else pages as before."""
    if (len(raw) > DEFER_MAX_BYTES or not read_compact.compactable("x" + extension)
            or not features.read_compaction(ctx.env)):
        return False
    try:
        state = circuit.state(ctx.store())
        if circuit.disabled(state, "dedupe-reads:Read") or circuit.disabled(state, "write-guard:Write"):
            return False
    except (OSError, ValueError, TypeError, KeyError):
        return False
    text = file_access(decode, raw)
    if not token_gate.bounded(text):
        return False
    count, _exact = token_gate.counter()
    tokens = count(read_compact.render(text.split("\n")))
    return features.value(ctx.env, "read_guard", "tokens") < tokens <= read_compact.DEFER_MAX_TOKENS


def guard(ctx):
    payload, env = ctx.payload, ctx.env
    if payload.get("tool_name") != "Read":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    if (session.is_recovery_path(path) or read_tool.explicit_range(tool_input)
            or session.is_host_tool_result(path, payload.get("session_id"))):
        return None
    extension = os.path.splitext(path)[1].lower()
    if extension in BINARY_EXTS:
        return None
    st = file_access(os.stat, path)
    if not stat.S_ISREG(st.st_mode):
        return None
    large = st.st_size > features.value(env, "read_guard", "bytes")
    python_candidate = extension == ".py" and OUTLINE_MIN_FILE < st.st_size <= OUTLINE_MAX_FILE
    if not large and not python_candidate:
        return None
    if wanted_whole(ctx, path):
        return None
    # A file the admission check can read whole is read once, and every check
    # below uses those bytes (#190). One that grew past that since the stat
    # passes through.
    raw = file_access(read_prefix, path, TEXT_MAX + 1) if st.st_size <= TEXT_MAX else None
    if raw is not None and len(raw) > TEXT_MAX:
        return None
    if b"\0" in (raw[:SNIFF] if raw is not None else file_access(read_prefix, path, SNIFF)):
        return None
    if raw is not None and deferred(ctx, raw, extension):
        return None
    limit = features.value(env, "read_guard", "lines")
    first_bytes, total_lines, exact = file_access(count_lines, path, time.monotonic() + ctx.remaining(), limit, raw)
    if first_bytes is None or first_bytes >= st.st_size:
        return None   # ``limit`` lines or fewer: a limit wouldn't shorten anything

    page_limit, page_bytes, outline = limit, first_bytes, None
    if python_candidate and raw is not None:
        candidate = read_tool.outline_page(raw, first_bytes, ctx.remaining())
        if candidate is not None:
            page_limit, (page_bytes, outline) = OUTLINE_LIMIT, candidate
    if not large and outline is None:
        return None
    updated = dict(tool_input)
    updated["limit"] = page_limit
    kib = (st.st_size + 1023) // 1024
    lines_text = ("%d lines" % total_lines) if exact else ("more than %d lines" % total_lines)
    context = (
        "Ork Meter read guard: %s is %d KiB and %s. This Read loaded only its first %d lines (limit=%d). "
        "The rest is available with further Read calls that set offset and limit, for example offset=%d. "
        "Set ORK_METER_TRIM=off to turn the guard off."
        % (path, kib, lines_text, page_limit, page_limit, page_limit + 1)
    )
    if outline:
        context += "\n" + outline
    # Bound the comparison to the native default first page, including line
    # labels and long-line clipping. Never charge the entire file as delivered.
    # Host fixtures qualify rendering; this is an offline admission reserve.
    text = file_access(decode, raw) if raw is not None else file_access(read_prefix, path, TEXT_MAX + 1, False)
    if len(text) > TEXT_MAX:
        return None
    original_lines = ["%6d\t%s" % (number, line[:2000])
                      for number, line in enumerate(text.splitlines(keepends=True)[:2000], 1)]
    if not token_gate.permits("".join(original_lines), "".join(original_lines[:page_limit]), context):
        return None
    try:
        ctx.store().mark_partial_view(session.path_key(path))   # a later ranged Read past it is demand
    except (OSError, ValueError, TypeError):
        pass
    ctx.record(ledger.read_guarded(st.st_size, page_bytes + len(context.encode("utf-8")),
                                   ledger.sha256_hex(path)))
    return hook.updated_input(ctx.event, updated, context)


def wanted_whole(ctx, path):
    """Earlier this session the model went back for lines a partial view of
    this file left out (hooks/dedupe-reads.py): page it no more (#329)."""
    try:
        store = session.Store.existing_for_payload(ctx.payload, ctx.env)
        return store is not None and store.wants_whole(session.path_key(path))
    except (OSError, ValueError, TypeError):
        return False
