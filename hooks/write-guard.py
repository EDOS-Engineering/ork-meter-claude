"""The Write guard: refuse a Write that would delete lines a compacted Read left out.

A compacted Read (hooks/dedupe-reads.py, hooks/lib/read_compact.py) shows
every line number, with elided ranges as a marker and empty lines. Claude
Code's read-before-edit state holds the whole file it read, so it accepts a
Write rebuilt from that view, and the Write silently deletes the elided code
(reproduced with Claude Code 2.1.302: 1,006 non-blank lines became 257).

``PreToolUse`` on ``Write``: with a record for the file
(hooks/lib/write_guard.py), the record is re-synced to the disk and the
Write's lines are aligned with the file's; a Write that leaves an unread
non-blank line of the writing session's view unmatched is refused
(``permissionDecision: "deny"``), naming the lines at their current numbers
and saying to Read them with ``offset`` and ``limit``, or to use Edit. A
session with no view of its own (a fork, a subagent) is checked against
every view together (a subagent, a fork or a resumed session starts from
that union). Any problem checking a guarded file refuses the Write with that
recoverable reason: a busy record lock, a file or Write too long to compare,
an unreadable record (which becomes a tombstone until the file is delivered
whole to the writing session, or the override is set; no Edit clears it),
an internal error. With no record anywhere, hooks/py.sh doesn't start Python
for this hook at all.

``PostToolUse`` on ``Read``, ``Bash`` and ``Grep``: output that showed a
guarded file's lines marks them read in the session's view, and a whole-file
Read delivered unchanged drops the view, but only when the reducing hook for
that tool (dedupe-reads.py, trim-tool-output.py) won't: outside ``reduce``
mode, with its lever off or its circuit tripped. Those hooks do it
themselves from what they delivered, so the two never disagree about a
result one of them replaced. A ranged Read is never replaced, so it is
always observed here.

``ORK_METER_WRITE_GUARD_OVERRIDE=on`` (from the next session) lets every
Write through: the deliberate-replace path, for every file, not per file.
Guarding is not a saving: the hook runs in every mode (``EVERY_MODE``),
whatever the levers say, so switching to shadow or off mid-session keeps
guarding a view already in context. Without a record it prints nothing.
"""

import circuit
import features
import hook
import read_tool
import session
import write_guard

EVENTS = ("PreToolUse", "PostToolUse")
REDUCER = "write-guard"
EVERY_MODE = True
RECOVER = ("Read the lines of %s you have not seen with offset and limit (a plain Read of the whole file again "
           "returns nothing new), or use Edit for targeted changes.")


def handle(ctx):
    if ctx.event == "PostToolUse":
        observe(ctx)
        return None
    payload = ctx.payload
    if payload.get("tool_name") != "Write":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path, new = tool_input.get("file_path"), tool_input.get("content")
    if not isinstance(path, str) or not path or not isinstance(new, str):
        return None
    if not write_guard.exists(ctx.env):
        return None
    if features.value(ctx.env, "read_guard", "write_guard_override") == 1:
        return None
    try:
        result = write_guard.check(ctx.env, path, write_guard.view_key(payload), new)
    except Exception:
        try:
            guarded = write_guard.locate(ctx.env, path) is not None
        except Exception:
            guarded = True
        if not guarded:
            return None
        result = ("unavailable", "an internal error")
    if result is None:
        return None
    kind, detail = result
    if kind == "lines":
        reason = ("Ork Meter refused this Write: your Read of %s was compacted, and this Write would delete lines of it "
                  "you have not seen: %s (their current line numbers). " % (path, write_guard.describe(detail))
                  + RECOVER % path + " A Write of this file is allowed once those lines have been read, or when it "
                  "keeps them in place.")
    else:
        reason = ("Ork Meter refused this Write: it could not check it against your compacted Read of %s (%s). "
                  % (path, detail) + RECOVER % path + " A whole-file Read of it delivered whole also clears this.")
    return hook.denied(ctx.event, reason)


def reducer_runs(ctx, scope):
    """Whether the reducing hook for this result runs and so observes it itself."""
    if features.mode(ctx.env) != "reduce" or not features.enabled(ctx.env, "trim"):
        return False
    try:
        store = session.Store.existing_for_payload(ctx.payload, ctx.env)
        return store is None or not circuit.disabled(circuit.state(store), scope)
    except Exception:
        return True


def observe(ctx):
    payload = ctx.payload
    tool = payload.get("tool_name")
    if tool not in ("Read", "Bash", "Grep") or not write_guard.exists(ctx.env):
        return
    view = write_guard.view_key(payload)
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    response = payload.get("tool_response")
    if tool == "Read":
        if not isinstance(response, dict) or response.get("type") != "text" or not isinstance(response.get("file"), dict):
            return
        info = response["file"]
        path, content = info.get("filePath"), info.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return
        if read_tool.explicit_range(tool_input):
            write_guard.observe(ctx.env, path, view, **ranged(info, content))
        elif not reducer_runs(ctx, "dedupe-reads:Read") and whole(info):
            try:
                write_guard.forget(ctx.env, path, view)
            except Exception:
                pass
        return
    if not reducer_runs(ctx, "trim-tool-output:" + tool):
        write_guard.observe_output(ctx.env, view, tool, tool_input, response, payload.get("cwd"))


def ranged(info, content):
    """``start`` and ``shown`` for a ranged Read result: ``numLines`` lines
    from ``startLine``, so a trailing newline never adds one."""
    start = info.get("startLine") if isinstance(info.get("startLine"), int) else 1
    count = info.get("numLines") if isinstance(info.get("numLines"), int) else None
    shown = content.split("\n")
    return {"start": start, "shown": shown[:count] if count is not None else shown}


def whole(info):
    lines = info.get("numLines")
    return (info.get("startLine", 1) == 1 and not info.get("truncatedByTokenCap")
            and isinstance(lines, int) and lines == info.get("totalLines"))
