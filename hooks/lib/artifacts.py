"""Immutable local tool-result snapshots and bounded, selective recovery.

Only a digest is used as a handle. Stored JSON retains the tool's original
schema, while its text companion, the only copy of the text, can be read
with the ordinary Read tool. Neither artifact content nor handles are sent
to Ork Meter.
"""

import hashlib
import io
import json
import os
import re

import session

FORMAT_VERSION = 1
ID_RE = re.compile(r"^[a-f0-9]{64}$")
# The document's ``text`` lives in the ``.txt`` file, not in the ``.json``.
TEXT_IN_TXT = "txt"
_MAX_DEPTH = 8


def _text_at(value, text, path=(), depth=0):
    """The path (keys and indices) of the first string in ``value`` equal to
    ``text``, in key and index order, or None."""
    if type(value) is str:
        return list(path) if len(value) == len(text) and value == text else None
    if depth >= _MAX_DEPTH:
        return None
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, item in items:
        found = _text_at(item, text, path + (key,), depth + 1)
        if found is not None:
            return found
    return None


def _replaced(value, path, new):
    """``value`` with the slot at ``path`` set to ``new``; only the containers
    on the path are copied, so the caller's response is not changed."""
    if not path:
        return new
    copy = dict(value) if isinstance(value, dict) else list(value)
    copy[path[0]] = _replaced(value[path[0]], path[1:], new)
    return copy


def prepare(store, tool, response, text, completeness=None):
    """Name a snapshot without writing it: a ``Pending`` whose ``path`` is
    final, so a reply can cite it before deciding to keep it (#130).

    The ``.txt`` holds the exact text, the file a note names; it is stored
    only there (#143, #190). The ``.json`` keeps the tool's response, the
    string in it equal to the text (if any) set to null and ``text_at``
    naming where it was. Its digest covers ``received_text_sha256``, so the
    ID still names the exact text; ``load`` puts the text back after checking
    that hash."""
    at = _text_at(response, text) or None   # [] would be the whole response: keep it
    document = {"schema_version": FORMAT_VERSION, "tool": tool,
                "completeness": completeness or {"received_response": "complete", "before_hook": "unknown"},
                "received_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "response": response if at is None else _replaced(response, at, None),
                "text_stored_in": TEXT_IN_TXT, "text_at": at}
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True)
    identity = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # Only names: write_private creates the directory, inside the caller's
    # fail-open write, so naming a snapshot never touches the disk.
    folder = os.path.join(store.path, session.TOOL_OUTPUT_DIR)
    return Pending(os.path.join(folder, identity + ".txt"), text, os.path.join(folder, identity + ".json"), raw + "\n")


class Pending:
    """A named, unwritten snapshot: the text file and its JSON companion."""

    def __init__(self, path, text, companion, raw):
        self.path = path
        self._files = ((path, text), (companion, raw))
        self._created = []

    def write(self):
        """Write both files. On failure, remove what this call created and
        re-raise: a note never names a half-written snapshot. A file that
        already existed is identical (the name is its digest) and may be cited
        by an earlier reply, so it is left alone."""
        try:
            for path, content in self._files:
                existed = os.path.lexists(session.fs_path(path))
                # The identity is the file this call wrote, taken before it
                # replaced ``path``, so a concurrent writer's file never
                # passes for ours in ``discard``.
                written = session.write_private(path, content)
                if not existed:
                    self._created.append((path, written))
        except BaseException:
            self.discard()
            raise
        return self.path

    def discard(self):
        """Remove the files ``write`` created, best effort. A file another
        hook has since replaced (same digest, its own reply) is kept."""
        while self._created:
            path, identity = self._created.pop()
            try:
                info = os.lstat(session.fs_path(path))
                if (info.st_dev, info.st_ino) == identity:
                    os.unlink(session.fs_path(path))
            except OSError:
                pass


def save(store, tool, response, text, completeness=None):
    return prepare(store, tool, response, text, completeness).write()


def admit(ctx, reply, pending):
    """Write ``pending`` for ``reply`` only if the reply will be printed, and
    say whether it may be returned (#130, #140).

    The rule every hook that cites a snapshot follows: the reply must pass
    the token gate and ``hook.may_emit`` before the write, and ``may_emit``
    again after it, or the files it wrote are removed. A failed write is not
    a reducer fault (#145): the result passes through, and a note never
    cites a file that is not there. ``hook.run`` gates the reply again; the
    token gate keeps its verdict, so that costs nothing."""
    import circuit
    import hook
    import token_gate
    circuit.validate_reply(ctx, reply)
    if not token_gate.permits_reply(ctx.payload, reply) or not hook.may_emit(ctx):
        return False
    if pending is not None:
        try:
            pending.write()
        except Exception:
            return False
        if not hook.may_emit(ctx):
            pending.discard()
            return False
    return True


def _read_text(path, newline):
    """The artifact file ``path`` decoded as UTF-8 exactly as ``open(path,
    "r", encoding="utf-8", newline=newline).read()`` would. Read without
    following links: FileNotFoundError when it is missing (pruned), ValueError
    when it is a link or not a regular file, ``session.FileChanged`` when it
    was replaced while being opened. No size limit: an artifact is as large
    as the output it saved."""
    try:
        raw = session.read_bounded(path, None, follow=False)
    except session.FileChanged:
        raise
    except ValueError:
        raise ValueError("artifact must be a regular file") from None
    return io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", newline=newline).read()


def _legacy_text_at(response):
    """Where a record handle written before #190 (no ``text_at``) kept its
    text: the one text block of an MCP result (#143)."""
    blocks = response.get("content") if isinstance(response, dict) else response
    if not isinstance(blocks, list) or len(blocks) != 1 or not isinstance(blocks[0], dict):
        raise ValueError("unsupported artifact layout")
    return ["content", 0, "text"] if isinstance(response, dict) else [0, "text"]


def load(store, identity):
    """The snapshot's document, with ``text`` and the text inside ``response``.

    Every layout written so far loads: the text kept in the ``.json`` (before
    #190), a record handle's text in the ``.txt`` (#143), and the text in the
    ``.txt`` with ``text_at`` (#190)."""
    if not isinstance(identity, str) or not ID_RE.fullmatch(identity):
        raise ValueError("artifact ID must be a 64-character lowercase SHA-256")
    document = json.loads(_read_text(session.fs_path(store.tool_output_path(identity, ".json")), None))
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True)
    if hashlib.sha256(raw.encode("utf-8")).hexdigest() != identity:
        raise ValueError("artifact integrity check failed")
    if document.get("schema_version") != FORMAT_VERSION:
        raise ValueError("unsupported artifact schema")
    stored = document.pop("text_stored_in", None)
    if stored is None:
        return document
    if stored != TEXT_IN_TXT:
        raise ValueError("unsupported artifact layout")
    text = _read_text(session.fs_path(store.tool_output_path(identity)), "")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != document.get("received_text_sha256"):
        raise ValueError("artifact integrity check failed")
    at = document.pop("text_at") if "text_at" in document else _legacy_text_at(document.get("response"))
    if at is not None:
        if not isinstance(at, list) or not at:
            raise ValueError("unsupported artifact layout")
        container = document.get("response")
        for step, key in enumerate(at):
            if not (isinstance(container, dict) and type(key) is str and key in container
                    or isinstance(container, list) and type(key) is int and 0 <= key < len(container)):
                raise ValueError("unsupported artifact layout")
            if step == len(at) - 1:
                if container[key] is not None:
                    raise ValueError("unsupported artifact layout")
                container[key] = text
            else:
                container = container[key]
    document["text"] = text
    return document


def select(document, start=1, limit=100, query=None):
    """Literal search or exact lines, with complete scope and omission counts."""
    lines = io.StringIO(document["text"], newline="").readlines()
    if start < 1 or limit < 1:
        raise ValueError("start and limit must be positive")
    matches = [(i + 1, line) for i, line in enumerate(lines)
               if query is None or query in line]
    selected = matches[start - 1:start - 1 + limit]
    return {"tool": document["tool"], "total_lines": len(lines),
            "completeness": document.get("completeness", {"received_response": "complete", "before_hook": "unknown"}),
            "search_scope": "entire artifact", "query": query,
            "total_matches": len(matches), "returned": len(selected),
            "omitted": len(matches) - len(selected),
            "lines": [{"line": i, "text": line} for i, line in selected]}
