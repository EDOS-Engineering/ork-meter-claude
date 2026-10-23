"""Ork Meter status line: context size, cost per turn and prompt-cache state.

Usage: statusline.py              (Claude Code's status line JSON on stdin)
       statusline.py --snippet    (print the settings.json snippet to enable it)

Claude Code runs a status line command after each assistant message (and on
a few other events), with the session's details as JSON on stdin; it shows
whatever the command prints. A plugin can't set the person's ``statusLine``
(plugin settings only take ``agent`` and ``subagentStatusLine``), so this is
opt-in: ``/ork-meter:statusline`` prints the snippet, the person adds it.

It prints one line, for example::

    ctx 84.2k | turn ~$0.41 | session ~$12.30 | cache 1h: 37m left (est.)

* ``ctx``: the context size of the newest main-conversation request, in
  tokens (fresh input + cache reads + cache writes, as Claude Code counts
  it). Measured, from the transcript's usage record.
* ``turn``: the cost of the requests since the person's last prompt
  (subagents included), ``session``: of every request in this session's
  transcript and its subagent transcripts. **Estimates** at API list prices
  (scripts/cache_report.py's PRICES, including the Haiku long-prompt card),
  hence the ``~``; on a subscription they are relative weights, not a bill. A
  trailing ``+`` means part of a long transcript is still unread (it is
  read over the next runs), so the figure is a lower bound; ``?`` means a model was not recognised and was
  priced as Opus 5.5.
* ``cache``: the prompt-cache lifetime the conversation was last written
  with, as the usage records report it (``ephemeral_1h`` or ``ephemeral_5m``,
  not an assumed five minutes), and the time left, or ``expired``: the next
  prompt will likely re-send the whole context at the cache-write price.
  Omitted until a lifetime has been observed.
* ``plugin updated: rerun /ork-meter:statusline``: the snippet points at a
  plugin version Claude Code has replaced (it deletes it 14 days later).

Reads only usage counters, model ids, timestamps and record types from the
transcripts (never message content), sends nothing anywhere, and keeps a
small owner-only state file per session under
``$XDG_STATE_HOME/ork-meter/statusline`` (``~/.local/state`` by default) so
each run reads only what was appended since the last one, at most
``READ_BUDGET`` bytes. On any error it prints ``ork-meter`` and exits 0: a
status line must never fail loudly.
"""

import hashlib
import json
import os
import stat
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import cache_report  # noqa: E402  (PRICES, prices, priced, parse_time, count)

STATE_VERSION = 1
READ_BUDGET = 4 * 1024 * 1024   # bytes read per run across all transcripts
FIRST_TAIL = 2 * 1024 * 1024    # a transcript first seen larger than this is read from its tail
MAX_FILES = 256                 # subagent transcripts tracked per session
MAX_IDS = 128                   # recent message ids kept to deduplicate streamed blocks
MAX_STDIN = 1024 * 1024
FALLBACK = "ork-meter"
TTL_SECONDS = {"1h": 3600, "5m": 300}
PRUNE_SECONDS = 14 * 24 * 3600


def state_dir(env):
    """``$XDG_STATE_HOME/ork-meter/statusline``, owner-only. Refuses a link
    or a directory another user owns (as hooks/lib/session.py does; that
    module isn't imported here because its imports cost a status line ~10 ms)."""
    base = env.get("XDG_STATE_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
    path = os.path.join(base, "ork-meter", "statusline")
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise OSError("state directory must not be a link")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise PermissionError("state directory belongs to another user")
    return path


def state_file(env, session_id):
    """The session's state file: named by a hash of its id, never the id."""
    if not isinstance(session_id, str) or not session_id:
        return None
    return os.path.join(state_dir(env), hashlib.sha256(session_id.encode("utf-8", "replace")).hexdigest()[:32] + ".json")


def read_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_state(path, state):
    """Owner-only (0600), atomically: a temporary file beside it replaces it."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def request_cost(model, usage):
    """(list-price $ estimate, recognised model?) for one request's usage,
    priced as scripts/cache_report.py prices it."""
    count = cache_report.count
    name = cache_report.priced(model)
    read, write, fresh, out = (count(usage, "cache_read_input_tokens"), count(usage, "cache_creation_input_tokens"),
                               count(usage, "input_tokens"), count(usage, "output_tokens"))
    base, read_price, out_price = cache_report.prices(name or cache_report.FALLBACK_MODEL, read + write + fresh)
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    hour = count(split, "ephemeral_1h_input_tokens")
    five = count(split, "ephemeral_5m_input_tokens") if split else max(write - hour, 0)
    cost = (fresh * base + read * read_price + (five * 1.25 + hour * 2.0) * base + out * out_price) / 1e6
    return cost, name is not None


def observed_ttl(usage):
    """The cache lifetime this request wrote with, from its usage split."""
    split = usage.get("cache_creation")
    if not isinstance(split, dict):
        return None
    if cache_report.count(split, "ephemeral_1h_input_tokens"):
        return "1h"
    if cache_report.count(split, "ephemeral_5m_input_tokens"):
        return "5m"
    return None


def is_prompt(row):
    """A person's prompt in the main conversation (not a tool result, not a
    meta row). Looks only at the shape of the content, never at its text."""
    if row.get("isSidechain") or row.get("isMeta"):
        return False
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get("type") in ("text", "image") for item in content)


def new_state(transcript):
    return {"v": STATE_VERSION, "transcript": transcript, "files": {}, "ids": {}, "session": 0.0, "turn": 0.0,
            "turn_start": None, "unpriced": False, "last": None, "ttl": None, "ttl_t": None}


def apply_row(state, row, main):
    """Fold one transcript row into the state. Rows may arrive out of time
    order (a long transcript's tail is read before its beginning), so every
    "latest" value is kept by timestamp."""
    kind = row.get("type")
    when = cache_report.parse_time(row.get("timestamp"))
    if when is None:
        return
    stamp = when.timestamp()
    last = state["last"]
    if main and kind == "user" and is_prompt(row):
        if state["turn_start"] is None or stamp > state["turn_start"]:
            state["turn_start"], state["turn"] = stamp, 0.0
        return
    if main and kind == "system" and row.get("subtype") == "compact_boundary":
        if last is not None and stamp >= last["t"]:
            state["last"] = {"t": stamp, "context": None, "cached": False}   # the context it described is gone
        return
    if kind != "assistant":
        return
    message = row.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict) or not message.get("id") or message.get("model") == "<synthetic>":
        return
    cost, known = request_cost(message.get("model"), usage)
    ids = state["ids"]
    key = str(message["id"])
    # Streamed blocks of one message repeat (and may grow) its usage: count
    # only what a repeat adds.
    delta = cost - ids.get(key, 0.0)
    ids[key] = max(cost, ids.get(key, 0.0))
    if len(ids) > MAX_IDS:
        for old in list(ids)[:len(ids) - MAX_IDS]:
            del ids[old]
    if delta > 0:
        state["session"] += delta
        if state["turn_start"] is not None and stamp >= state["turn_start"]:
            state["turn"] += delta
    state["unpriced"] = state["unpriced"] or not known
    if main and not row.get("isSidechain"):
        count = cache_report.count
        if last is None or stamp >= last["t"]:
            context = sum(count(usage, k) for k in ("input_tokens", "cache_read_input_tokens",
                                                    "cache_creation_input_tokens"))
            cached = count(usage, "cache_read_input_tokens") + count(usage, "cache_creation_input_tokens")
            state["last"] = {"t": stamp, "context": context, "cached": cached > 0}
        ttl = observed_ttl(usage)
        if ttl and (state["ttl_t"] is None or stamp >= state["ttl_t"]):
            state["ttl"], state["ttl_t"] = ttl, stamp


def read_chunk(fh, start, end, live):
    """Complete lines in ``[start, end)``: (lines, offset after the last
    complete line). With no line end in the range, a live read waits for the
    line to be finished; an earlier range is mid-way through one very long
    line, so it moves on (the line's remainder fails to parse and is skipped)."""
    fh.seek(start)
    data = fh.read(end - start)
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], (start if live else start + len(data))
    return data[:cut].split(b"\n"), start + cut + 1


def read_new(path, entry, budget):
    """Lines of ``path`` not read yet, at most ``budget`` bytes: first what
    was appended since the last run, then (with what budget is left) the
    earlier part of a file that was too long to read at once. ``entry`` keeps
    the file's identity, its live ``offset`` and the ``back`` range still to
    read. Returns (lines, bytes read)."""
    try:
        info = os.stat(path)
    except OSError:
        return [], 0
    size = info.st_size
    ident = [info.st_dev, info.st_ino]
    if entry.get("id") != ident or entry.get("offset", 0) > size:
        entry.clear()   # a new or replaced file: start over
        entry.update({"id": ident, "offset": 0})
    lines, used = [], 0
    with open(path, "rb") as fh:
        start = entry["offset"]
        if size - start > min(budget, FIRST_TAIL):
            # Too far behind: read the tail now (the live figures) and the
            # skipped part later, from its first line to where the tail starts.
            jump = size - min(budget, FIRST_TAIL)
            fh.seek(jump)
            fh.readline()   # finish the line the jump landed in
            tail_start = fh.tell()
            if tail_start > start:
                back = entry.get("back") or [start, tail_start]
                entry["back"] = [min(back[0], start), tail_start]
                start = tail_start
        if size > start:
            new, entry["offset"] = read_chunk(fh, start, size, True)
            lines += new
            used += size - start
        back = entry.get("back")
        if back and budget - used > 0:
            first = back[0]
            end = min(back[1], first + budget - used)
            old, back[0] = read_chunk(fh, first, end, False)
            lines += old
            used += end - first
            if back[0] >= back[1]:
                del entry["back"]
    return lines, used


def scan(state, transcript):
    """Bring the state up to date with the transcript and its subagents.
    Returns True when some of it is still unread (figures are lower bounds)."""
    paths = [(transcript, True)]
    sub = os.path.join(os.path.splitext(transcript)[0], "subagents")
    try:
        names = sorted(n for n in os.listdir(sub) if n.endswith(".jsonl"))
    except OSError:
        names = []
    paths += [(os.path.join(sub, n), False) for n in names[-MAX_FILES:]]
    budget = READ_BUDGET
    files = state["files"]
    behind = len(names) > MAX_FILES
    for path, main in paths:
        key = "main" if main else os.path.basename(path)
        entry = files.setdefault(key, {})
        lines, used = read_new(path, entry, budget)
        budget -= used
        behind = behind or "back" in entry
        for raw in lines:
            if b'"usage"' not in raw and b'"user"' not in raw and b'compact_boundary' not in raw:
                continue
            if b'"tool_result"' in raw and b'"usage"' not in raw:
                continue   # a tool result, not a prompt: skip the parse
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if isinstance(row, dict):
                apply_row(state, row, main)
    if len(files) > MAX_FILES + 1:
        for key in sorted(k for k in files if k != "main")[:len(files) - MAX_FILES - 1]:
            del files[key]
    return behind


def tokens(n):
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fk" % (n / 1e3)
    return str(n)


def money(value, state, behind):
    return "~$%.2f%s%s" % (value, "+" if behind else "", "?" if state["unpriced"] else "")


def render(state, now, behind=False, payload_ttl=None):
    """The status line: ASCII only, no message content, no percentages."""
    parts = []
    last = state["last"]
    if last is not None:
        parts.append("ctx " + ("compacted" if last["context"] is None else tokens(last["context"])))
    if state["turn_start"] is not None:
        parts.append("turn " + money(state["turn"], state, behind))
    parts.append("session " + money(state["session"], state, behind))
    ttl = state["ttl"] or (payload_ttl if payload_ttl in TTL_SECONDS else None)
    if last is not None and last["cached"] and ttl:
        left = last["t"] + TTL_SECONDS[ttl] - now
        parts.append("cache %s: %s" % (ttl, "%dm left" % max(1, left // 60) if left > 0 else "expired"))
    line = " | ".join(parts) + " (est.)"
    if os.path.exists(os.path.join(ROOT, ".orphaned_at")):
        # Claude Code marks a replaced plugin version and deletes it 14 days
        # later; the snippet still points here.
        line += " | plugin updated: rerun /ork-meter:statusline"
    return line


def prune(directory, now):
    """Drop state files of sessions not seen for PRUNE_SECONDS (runs only
    when a session's state is first created)."""
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        try:
            if (entry.name.endswith((".json", ".tmp")) and entry.is_file(follow_symlinks=False)
                    and now - entry.stat().st_mtime > PRUNE_SECONDS):
                os.unlink(entry.path)
        except OSError:
            pass


def status_line(payload, env, now):
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return FALLBACK
    cache = payload.get("prompt_cache")
    payload_ttl = cache.get("ttl") if isinstance(cache, dict) else None
    try:
        path = state_file(env, payload.get("session_id"))
    except OSError:
        path = None   # no state: this run reads the transcript tail only
    state = read_state(path) if path else None
    if not isinstance(state, dict) or state.get("v") != STATE_VERSION or state.get("transcript") != transcript:
        state = new_state(transcript)
        if path:
            prune(os.path.dirname(path), now)
    behind = scan(state, transcript)
    if path:
        try:
            state["seen"] = now
            write_state(path, state)
        except OSError:
            pass
    return render(state, now, behind, payload_ttl)


def snippet():
    """The settings.json snippet for this install. ``python3`` from PATH, as
    the plugin's hooks use. The path uses forward slashes: on Windows Claude
    Code runs status line commands through Git Bash when it is installed (and
    PowerShell otherwise), and Git Bash eats backslashes."""
    script = os.path.join(HERE, "statusline.py").replace("\\", "/")
    command = 'python3 -I -B "%s"' % script
    return json.dumps({"statusLine": {"type": "command", "command": command, "refreshInterval": 60}}, indent=2)


def main(argv, stdin, env):
    if argv[1:] == ["--snippet"]:
        return snippet()
    payload = json.loads(stdin.read(MAX_STDIN) or "{}")
    if not isinstance(payload, dict):
        return FALLBACK
    return status_line(payload, env, time.time())


if __name__ == "__main__":
    try:
        line = main(sys.argv, sys.stdin, os.environ)
    except BaseException:  # noqa: BLE001 - a status line never fails loudly
        line = FALLBACK
    try:
        # Bytes, so Windows text mode doesn't turn the line end into CRLF.
        sys.stdout.buffer.write((line + "\n").encode("ascii", "replace"))
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        pass
    sys.exit(0)
