"""Context-resident delta delivery: send what changed, not the whole result again.

A whole-file Read of a file Claude already read this conversation, or a
Bash command it already ran, usually returns text that is mostly identical
to a tool result that is still in the model's context. Re-sending it costs
its full size as new (cache-written) input and then as cached input on every
later turn. This module renders the new result as a line diff against the
earlier delivered result, so the model receives only the changed lines.

The base must be exact text already delivered inline to the model. A recovery
path does not make trimmed or native-persisted Bash output a visible base.
Bases are kept in the session store's ``delta-bases/`` directory,
keyed by a hash of (agent, tool, identity), and are deleted with the
repeat-read index on every SessionStart (compaction included), because after
compaction the earlier result is no longer in context. A base is only ever
a result delivered whole; after a delta is delivered the base is dropped,
so deltas never chain (#144).

Storage failures are not the hooks' failures (#139): ``load``, ``save``
and ``forget`` swallow ``OSError``, so the ordinary trim or dedupe reply
still goes out and nothing counts toward the reducer circuit breaker. A
failed ``save`` drops any older base, so it cannot be mistaken for the
current one.

A base is only kept current while its hook's handler runs with the lever on.
When it does not (entitlement lapsed, circuit breaker tripped, ``trim`` or
``delta`` off, shadow mode, a handler exception), the result still reaches
the model unchanged, so ``hook.run`` calls ``invalidate`` to drop the base
for that tool call: the next run is delivered in full, never diffed against
output the model no longer holds as the latest (#144).

Pure diff rendering lives here (``render``); storage goes through
``session.Store``. Nothing here reads the environment: the ``delta`` lever
(opt-in, ``ORK_METER_DELTA=on``) is checked by the hooks.
"""

import os

import ledger
import session
import trimmer

CONTEXT_LINES = 2          # unchanged lines kept around each change
MAX_RATIO = 0.5            # a delta larger than half the new text is not worth it
MAX_LINES = 2500           # conservative bound below the native two-second hook timeout
MAX_BYTES = 1024 * 1024    # either side over this: skip
MIN_BYTES = 2048           # smaller results are not worth a delta


def key(tool, agent, *parts):
    """The store key for one (agent, tool, identity); never a raw path."""
    raw = "\x00".join([tool, agent if isinstance(agent, str) else ""] + ["" if p is None else str(p) for p in parts])
    return ledger.sha256_hex(raw)


def load(store, name):
    """(text, meta) of a stored base, or (None, None)."""
    try:
        meta = session.read_json(store.delta_meta_path(name))
        if not isinstance(meta, dict):
            return None, None
        with open(session.fs_path(store.delta_base_path(name)), "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return None, None
    if ledger.sha256_hex(text) != meta.get("sha256"):
        return None, None   # torn or tampered: never diff against an unverified base
    return text, meta


def save(store, name, text, **meta):
    """Remember ``text`` as the base the model now holds for ``name``.

    Text under MIN_BYTES can never be a delta base, so it is not written to
    disk at all (short outputs are often tokens or credentials, #143); any
    older base for ``name`` is dropped, since the model now holds this text."""
    if trimmer.nbytes(text) < MIN_BYTES:
        forget(store, name)
        return
    meta = dict(meta)
    meta["sha256"] = ledger.sha256_hex(text)
    meta["lines"] = text.count("\n") + 1
    meta["bytes"] = trimmer.nbytes(text)
    try:
        session.write_private(store.delta_base_path(name), text)
        session.write_json(store.delta_meta_path(name), meta)
    except OSError:
        forget(store, name)


def forget(store, name):
    """Drop the base for ``name``: the model no longer holds it as the latest result."""
    try:
        store.forget_delta(name)
    except OSError:
        pass


# The hooks that keep bases (their ``REDUCER``), the tool each keeps bases for
# and the events that deliver that tool's result to the model.
KEEPERS = {
    "trim-tool-output": ("Bash", ("PostToolUse", "PostToolUseFailure")),
    "dedupe-reads": ("Read", ("PostToolUse",)),
}


def stale_keys(reducer, payload):
    """The base keys a tool call handled by ``reducer`` makes stale when its
    result reaches the model without the handler updating them. Pure: the
    same keys ``trim-tool-output.py`` and ``dedupe-reads.py`` derive."""
    if reducer not in KEEPERS or not isinstance(payload, dict):
        return []
    tool, events = KEEPERS[reducer]
    if payload.get("tool_name") != tool or payload.get("hook_event_name") not in events:
        return []
    agent = payload.get("agent_id")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if tool == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return []
        return [key("Bash", agent, payload.get("cwd"), command)]
    if tool_input.get("offset") is not None or tool_input.get("limit") is not None:
        return []   # dedupe-reads keeps bases for whole-file Reads only
    response = payload.get("tool_response")
    file_info = response.get("file") if isinstance(response, dict) else None
    paths = [file_info.get("filePath") if isinstance(file_info, dict) else None, tool_input.get("file_path")]
    keys = []
    for path in paths:
        if isinstance(path, str) and path:
            name = key("Read", agent, path)
            if name not in keys:
                keys.append(name)
    return keys


def invalidate(reducer, payload, env):
    """Drop the bases this tool call made stale. Cheap and side-effect free
    when there is nothing to drop: no store or directory is created, and no
    lever, entitlement or tokenizer state is read. Never raises."""
    try:
        names = stale_keys(reducer, payload)
        if not names:
            return
        store = session.Store.existing_for_payload(payload, env)
        if store is None or not os.path.isdir(session.fs_path(os.path.join(store.path, session.DELTA_DIR))):
            return
        for name in names:
            forget(store, name)
    except Exception:
        pass


def render(old, new, old_label, new_label):
    """A unified diff from ``old`` to ``new`` with ``CONTEXT_LINES`` of context,
    or None when the inputs are out of bounds, identical-by-hash checks belong
    to the caller, or the diff is not at most ``MAX_RATIO`` of ``new``.

    Returns (text, info). ``text`` is "" for identical inputs."""
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    if max(trimmer.nbytes(old), trimmer.nbytes(new)) > MAX_BYTES:
        return None
    old_lines = old.split("\n")
    new_lines = new.split("\n")
    if max(len(old_lines), len(new_lines)) > MAX_LINES:
        return None
    if old == new:
        return "", {"added": 0, "removed": 0, "hunks": 0}
    import difflib   # only a delta pays for it; delta is opt-in (#190)
    lines = list(difflib.unified_diff(old_lines, new_lines, old_label, new_label, n=CONTEXT_LINES, lineterm=""))
    added = sum(1 for l in lines[2:] if l.startswith("+"))
    removed = sum(1 for l in lines[2:] if l.startswith("-"))
    hunks = sum(1 for l in lines if l.startswith("@@"))
    text = "\n".join(lines)
    if trimmer.nbytes(text) > MAX_RATIO * trimmer.nbytes(new):
        return None
    return text, {"added": added, "removed": removed, "hunks": hunks}
