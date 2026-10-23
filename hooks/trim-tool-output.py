"""PostToolUse hook for Bash, Grep, Glob and WebFetch: trim oversized results.

When the result is over its byte budget the reply is a
``hookSpecificOutput.updatedToolOutput`` that keeps the tool's output
shape and replaces only the text fields with a compact, faithful version
(see hooks/lib/trimmer.py). The full original result is written to
the session store's ``tool-output/<sha256>.txt`` (mode 0600)
and the trimmed result ends with that path, so Claude can read it when it
needs the rest: a plain ``cat``, ``head``, ``tail`` or ``sed -n 'A,Bp'`` of
files in that store passes through untrimmed and is not saved again (#129). A JSON companion preserves the exact original tool response.
Bash test-runner output also loses passing-test lines. With the opt-in
``delta`` lever (``ORK_METER_DELTA=on``), a re-run of a command already run
in this conversation (same agent, cwd and command text) is delivered as a
unified diff against the earlier run's full stdout (hooks/lib/delta.py).
The stored base is always the last stdout the model saw in full: any other
delivery of that command's output, including a failed run
(``PostToolUseFailure``), drops it, so the next run arrives in full.

A result Claude Code already persisted to disk (``persistedOutputPath`` in
``tool_response``) is left alone: the model gets Claude Code's own short
``<persisted-output>`` preview, never this hook's replacement (observed on
macOS with 2.1.280 and 2.1.308; tests/native_recovery_qualification.py
checks every CI host), so a trim would change nothing the model sees and its
saving, measured against the full output, would be one that never happened
(issues #100, #350). No reply, no full-output copy, no ledger row.

Runs through hooks/lib/hook.py: nothing is printed on any error, an
under-budget result, or ``ORK_METER_TRIM=off``. Read results are handled
by dedupe-reads.py.
"""

import os

import features
import artifacts
import delta
import hook
import ledger
import recovery
import session
import trimmer
import write_guard

EVENTS = ("PostToolUse", "PostToolUseFailure")
FEATURE = "trim"
REDUCER = "trim-tool-output"
TOOLS = ("Bash", "Grep", "Glob", "WebFetch")
NATIVE_SPILL_BYTES = 20 * 1024
NATIVE_PREVIEW_BUDGET = 1024


class Late(Exception):
    """The reply can no longer be emitted (``hook.may_emit``) before it is built."""


class FullOutput:
    """The full-output artifact one reply's note cites.

    ``path(text)`` names the artifact without writing it; ``handle`` writes
    it only once the reply has passed the token gate and the deadline (#130),
    so a refused or late reply leaves nothing on disk. One result has one
    original text, so every note in it names the same file."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.pending = None

    def path(self, text):
        if self.pending is None:
            if not hook.may_emit(self.ctx):
                # Naming it hashes a document several times the output's
                # size; a reply already past its deadline is never emitted.
                raise Late()
            self.pending = artifacts.prepare(self.ctx.store(), self.ctx.payload.get("tool_name"),
                                             self.ctx.payload.get("tool_response"), text)
        return self.pending.path


# Second line of the note when the saved output is outside the session's cwd
# (#269). No " (" in it: parsers take the path as the text before the last
# " (" after "Full output: ".
OUTSIDE_HINT = "Outside the workspace; Bash sed -n 'A,Bp', head, tail or cat reads it."


def note(ctx, path, text, suffix=""):
    """The recovery note: ``Full output: <path> (<n> bytes)`` (parsers key on
    that line, external pipeline estimators among them), then
    ``OUTSIDE_HINT`` when the artifact is outside the session's cwd. File
    tools confined to the workspace (``--restricted``, sandboxes, permission
    rules) refuse a Read or Grep of that path, a wasted request; Bash still
    reads it, and a plain ``sed -n``, ``head``, ``tail`` or ``cat`` of a saved
    artifact passes this reducer untrimmed (``passes_through``). The hint names
    no path again and states a fact rather than asking for a read, so a
    test-filter note still doesn't send Claude to the original (#212)."""
    line = "Full output: %s (%d bytes)%s" % (path, trimmer.nbytes(text), suffix)
    if not recovery.outside(path, ctx.payload.get("cwd")):
        return line
    return line + "\n" + OUTSIDE_HINT


def handle_bash(ctx, response, budget, full):
    tool_input = ctx.payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    name = delta_name(ctx, command) if features.enabled(ctx.env, "delta") else None
    through = persisted(response) or passes_through(ctx, response, command)
    try:
        chosen = None if through else deliver_bash(ctx, response, budget, command, name, full)
    except Late:
        if name is not None:
            delta.forget(ctx.store(), name)
        raise
    if name is not None:
        # The base must be exactly the last stdout the model saw for this
        # command (#138, #144). Only an untouched, fully inline result is one;
        # a delta, a trimmed result, a native disk preview or a pass-through
        # drops it, so the next run is delivered in full.
        if (not through and chosen is None
                and trimmer.nbytes(response["stdout"]) < NATIVE_SPILL_BYTES):
            delta.save(ctx.store(), name, response["stdout"], inline=True)
        else:
            delta.forget(ctx.store(), name)
    return chosen


def passes_through(ctx, response, command):
    """Results the Bash reducers never touch: recovery reads and formats the
    trimmer cannot shorten safely."""
    exempt = recovery.exempt_script(command, ctx.env, ctx.payload.get("cwd"))   # parses the command once
    if exempt.script is not None:
        return True
    # The model following a "Full output: <path>" note with cat, head, tail or
    # sed -n gets those bytes as they are and no second artifact (#129).
    # Claude Code's own Bash output limit still bounds a huge one. Only such a
    # read opens the session store.
    if exempt.reads and recovery.reads_only_in(exempt.reads, os.path.join(ctx.store().path, session.TOOL_OUTPUT_DIR)):
        return True
    stdout = response.get("stdout")
    stderr = response.get("stderr")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return True
    if trimmer.unsupported_line_format(stdout) or trimmer.unsupported_line_format(stderr):
        return True
    return bool(response.get("isImage") or response.get("backgroundTaskId"))


def deliver_bash(ctx, response, budget, command, name, full):
    """(updated response, ledger rows) for one Bash result, or None."""
    stdout = response["stdout"]
    stderr = response["stderr"]
    original = stdout + ("\n--- stderr ---\n" + stderr if stderr else "")

    def path():
        return full.path(original)
    candidate = bash_delta(ctx, name, response, stdout, stderr, original, budget, path) if name is not None else None
    ordinary = reduce_bash(ctx, response, budget, command, stdout, stderr, original, path)
    chosen = ordinary
    if candidate is not None:
        # Deliver whichever is smaller: a delta can lose to the test filter.
        shown = ordinary[0] if ordinary is not None else response
        if (trimmer.nbytes(candidate[0]["stdout"]) + trimmer.nbytes(candidate[0]["stderr"])
                < trimmer.nbytes(shown["stdout"]) + trimmer.nbytes(shown["stderr"])):
            chosen = candidate
    return chosen


def reduce_bash(ctx, response, budget, command, stdout, stderr, original, full_path):
    """The ordinary Bash path: test filter, then head/tail trim.

    ``passes_through`` has checked the format of stdout and stderr; only a
    filtered stdout is new text that the trimmer must check again (#190)."""
    rows = []
    stdout_checked = True
    omitted = 0
    if features.enabled(ctx.env, "test_filter") and trimmer.is_test_command(command):
        filtered, omitted = trimmer.filter_test_output(stdout, checked=True)
        if omitted:
            rows.append(ledger.test_filtered("Bash", trimmer.nbytes(stdout), trimmer.nbytes(filtered)))
            stdout, stdout_checked = filtered, False
    total = trimmer.nbytes(stdout) + trimmer.nbytes(stderr)
    new_stdout, new_stderr = stdout, stderr
    if total > budget:
        err_budget = budget // 4
        if trimmer.nbytes(stderr) > err_budget:
            new_stderr, _ = trimmer.trim_text(stderr, err_budget, checked=True)
        new_stdout, _ = trimmer.trim_text(stdout, max(1024, budget - trimmer.nbytes(new_stderr)), checked=stdout_checked)
        if omitted and (new_stdout != stdout or new_stderr != stderr):
            new_stdout = trimmer.after_trim(new_stdout)
        # A trim that changed nothing (every line kept is a mandatory line or
        # its context) is no stage: the filtered output is delivered over
        # budget rather than not at all (#200).
        if new_stdout != stdout or new_stderr != stderr:
            rows.append(ledger.trimmed("Bash", total, trimmer.nbytes(new_stdout) + trimmer.nbytes(new_stderr)))
    if not rows:
        return None
    suffix = ("\n" if new_stdout else "") + note(ctx, full_path(), original)
    rows = charge_note(rows, trimmer.nbytes(suffix))
    if rows is None:
        return None
    new_stdout += suffix
    updated = dict(response)
    updated["stdout"] = new_stdout
    updated["stderr"] = new_stderr
    return updated, rows


STAGES = {"trim": ledger.trimmed, "test_filter": ledger.test_filtered}


def charge_note(rows, cost):
    """``rows`` (a chain of Bash stages, each one's ``bytes_before`` the
    previous one's ``bytes_after``) with the recovery note's ``cost`` charged
    once, or None when no stage's saving covers it.

    The note is charged to the final stage, as before #200. When that stage
    saved less than the note, it goes to the last stage that saved enough, and
    every later stage carries it in both its before and after, so the chain
    still telescopes to the delivered size and no stage expands: stage
    estimates clamp at zero, and an expanding stage would hide its cost behind
    an earlier stage's positive estimate."""
    for at in range(len(rows) - 1, -1, -1):
        if rows[at]["bytes_before"] - rows[at]["bytes_after"] >= cost:
            break
    else:
        return None
    charged = rows[:at]
    for index, row in enumerate(rows[at:], at):
        before = row["bytes_before"] + (cost if index > at else 0)
        charged.append(STAGES[row["kind"]]("Bash", before, row["bytes_after"] + cost))
    return charged


def delta_name(ctx, command):
    """The delta key for one command in one cwd and agent, or None."""
    if not isinstance(command, str) or not command.strip():
        return None
    return delta.key("Bash", ctx.payload.get("agent_id"), ctx.payload.get("cwd"), command)


def bash_delta(ctx, name, response, stdout, stderr, original, budget, full_path):
    """Deliver a re-run's stdout as a diff against the earlier run's full stdout.

    The base must have been delivered inline and in full (``handle_bash``).
    A raw artifact that was not delivered to the model is never sufficient.
    State is updated only after the final choice between the ordinary and
    delta paths.
    Returns (updated response, ledger rows) or None to use the ordinary path."""
    base, meta = delta.load(ctx.store(), name)
    if (base is None or meta.get("inline") is not True
            or trimmer.nbytes(stdout) >= NATIVE_SPILL_BYTES
            or trimmer.nbytes(stdout) < delta.MIN_BYTES):
        return None
    if trimmer.nbytes(stderr) > budget // 4:
        return None
    rendered = delta.render(base, stdout, "earlier run", "this run")
    if rendered is None:
        return None
    diff, info = rendered
    lines = stdout.count("\n") + 1
    if not diff:
        body = ("Ork Meter delta: stdout is identical to the earlier run of this same command in this conversation "
                "(%d lines, sha256 %s); see that earlier result." % (lines, meta["sha256"][:8]))
    else:
        body = ("Ork Meter delta: this command already ran earlier in this conversation. Its stdout now (%d lines) is the "
                "earlier run's stdout with this unified diff applied (%d hunks, +%d/-%d lines; unchanged lines omitted):\n%s"
                % (lines, info["hunks"], info["added"], info["removed"], diff))
    new_stdout = body + "\n" + note(ctx, full_path(), original, "; set ORK_METER_DELTA=off to always get full output.")
    before = trimmer.nbytes(stdout) + trimmer.nbytes(stderr)
    after = trimmer.nbytes(new_stdout) + trimmer.nbytes(stderr)
    if after >= before:
        return None
    updated = dict(response)
    updated["stdout"] = new_stdout
    return updated, [ledger.delta_delivered("Bash", before, after, info["hunks"])]


def handle_text_field(ctx, tool, response, field, budget, full):
    text = response.get(field)
    if not isinstance(text, str):
        return None
    # Large non-Bash results may already get a short native disk preview.
    # An 8-16 KiB replacement can defeat that preview and grow model context.
    # Keep an already-eligible large replacement below the native preview.
    if trimmer.nbytes(text) > budget and trimmer.nbytes(text) >= NATIVE_SPILL_BYTES:
        budget = min(budget, NATIVE_PREVIEW_BUDGET)
    new_text, info = trimmer.trim_text(text, budget)
    if info is None:
        return None
    updated = dict(response)
    updated[field] = new_text + "\n" + note(ctx, full.path(text), text)
    if trimmer.nbytes(updated[field]) >= info["bytes_before"]:
        return None
    return updated, [ledger.trimmed(tool, info["bytes_before"], trimmer.nbytes(updated[field]))]


def handle_list_field(ctx, tool, response, field, budget, full):
    items = response.get(field)
    if not isinstance(items, list):
        return None
    if all(isinstance(item, str) for item in items):
        size = sum(trimmer.nbytes(item) + 1 for item in items)
        if size > budget and size >= NATIVE_SPILL_BYTES:
            budget = min(budget, NATIVE_PREVIEW_BUDGET)
    new_items, info = trimmer.trim_list(items, budget)
    if info is None:
        return None
    original = "\n".join(items)
    updated = dict(response)
    updated[field] = new_items + [note(ctx, full.path(original), original)]
    after = sum(trimmer.nbytes(i) + 1 for i in updated[field])
    if after >= info["bytes_before"]:
        return None
    return updated, [ledger.trimmed(tool, info["bytes_before"], after)]


def trim(ctx, full):
    """(updated tool_response, ledger rows) or None when nothing changes.
    A note in the update names ``full``'s artifact, which is not yet written."""
    tool = ctx.payload.get("tool_name")
    response = ctx.payload.get("tool_response")
    if tool not in TOOLS or not isinstance(response, dict):
        return None
    budget = features.trim_budget(ctx.env, tool)
    if tool == "Bash":
        return handle_bash(ctx, response, budget, full)
    if persisted(response):
        return None
    if tool == "WebFetch":
        return handle_text_field(ctx, tool, response, "result", budget, full)
    if tool == "Grep":
        if isinstance(response.get("content"), str) and response.get("content"):
            return handle_text_field(ctx, tool, response, "content", budget, full)
        return handle_list_field(ctx, tool, response, "filenames", budget, full)
    if tool == "Glob":
        return handle_list_field(ctx, tool, response, "filenames", budget, full)
    return None


def persisted(response):
    """Claude Code saved this output to disk (``persistedOutputPath``, with
    ``persistedOutputSize``, in ``tool_response``) and gives the model only its
    ``<persisted-output>`` preview, whatever this hook replies."""
    path = response.get("persistedOutputPath") if isinstance(response, dict) else None
    return isinstance(path, str) and bool(path)


def handle(ctx):
    reply = _handle(ctx)
    if ctx.event == "PostToolUse" and ctx.payload.get("tool_name") in ("Bash", "Grep"):
        # Output that showed lines of a compacted file marks them read for the
        # Write guard (hooks/lib/write_guard.py), as delivered: the trimmed
        # text when this hook replaced it. Best effort, never the reply's fault.
        # A persisted output showed the model only Claude Code's preview, so
        # none of its lines count as read.
        if persisted(ctx.payload.get("tool_response")):
            return reply
        delivered = reply["hookSpecificOutput"]["updatedToolOutput"] if reply else ctx.payload.get("tool_response")
        try:
            write_guard.observe_output(ctx.env, write_guard.view_key(ctx.payload), ctx.payload.get("tool_name"),
                                       ctx.payload.get("tool_input"), delivered, ctx.payload.get("cwd"))
        except Exception:
            pass
    return reply


def _handle(ctx):
    if ctx.event == "PostToolUseFailure":
        # A failed run reached the model unchanged: its base is now stale (#138).
        if ctx.payload.get("tool_name") == "Bash" and features.enabled(ctx.env, "delta"):
            tool_input = ctx.payload.get("tool_input")
            name = delta_name(ctx, tool_input.get("command") if isinstance(tool_input, dict) else None)
            if name is not None:
                delta.forget(ctx.store(), name)
        return None
    full = FullOutput(ctx)
    try:
        result = trim(ctx, full)
    except Late:
        return None
    if result is None:
        return None
    updated, rows = result
    reply = hook.updated_tool_output(ctx.event, updated)
    # The full-output file is written exactly when the reply will be printed
    # (#130, #140): a refused or late reply leaves nothing behind.
    if not artifacts.admit(ctx, reply, full.pending):
        return None
    # ``trim`` never replies to a persisted output (``persisted``), so every
    # row here measures text the model would otherwise have received.
    for row in rows:
        ctx.record(row)
    return reply
