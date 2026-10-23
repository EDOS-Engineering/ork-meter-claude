#!/usr/bin/env bash
# Tests for hooks/lib/hook.py and hooks/lib/run.py: the contract every Python hook runs through.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

mods="$tmp/mods"; mkdir -p "$mods"
run_module() { # <module path> <stdin-text> [VAR=value ...]: through run.py, as py.sh does
  local script=$1 text=$2
  shift 2
  out=$(printf '%s' "$text" | env "$@" python3 -I -B hooks/lib/run.py "$script" 2>/dev/null)
  status=$?
}

cat > "$mods/echo.py" <<'EOF'
EVENTS = ("PostToolUse",)
def handle(ctx):
    return {"hookSpecificOutput": {"hookEventName": ctx.event, "additionalContext": "hi"}, "remaining_ok": 0 < ctx.remaining() <= 1.5}
EOF
cat > "$mods/gated.py" <<'EOF'
EVENTS = ("PostToolUseFailure",)
FEATURE = "loop_guard"
def handle(ctx):
    return {"systemMessage": "on"}
EOF
cat > "$mods/raises.py" <<'EOF'
EVENTS = ("SessionEnd",)
def handle(ctx):
    raise RuntimeError("boom")
EOF
cat > "$mods/quiet.py" <<'EOF'
EVENTS = ("SessionEnd",)
BUDGET = 0.25
def handle(ctx):
    import sys
    assert ctx.remaining() <= 0.25
    return None
EOF
cat > "$mods/broken-import.py" <<'EOF'
import no_such_module_anywhere
EVENTS = ("SessionEnd",)
def handle(ctx):
    return {"x": 1}
EOF

# run: parse, guard, opt-out, reply, fail-open.
run_module "$mods/echo.py" '{"hook_event_name":"PostToolUse"}'
assert "matching event: the handler's dict is printed as one JSON line" 'o == {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "hi"}, "remaining_ok": True} and "\n" not in out'
run_module "$mods/echo.py" '{"hook_event_name":"PreToolUse"}'
silent "wrong event: prints nothing"
run_module "$mods/echo.py" '{"hook_event_name":["PostToolUse"]}'
silent "odd hook_event_name type: prints nothing"
run_module "$mods/echo.py" '["PostToolUse"]'
silent "payload not an object: prints nothing"
run_module "$mods/echo.py" '{not json'
silent "malformed stdin: prints nothing, exit 0"
run_module "$mods/echo.py" ''
silent "empty stdin: prints nothing, exit 0"
run_module "$mods/gated.py" '{"hook_event_name":"PostToolUseFailure"}'
assert "feature on: reply printed" 'o == {"systemMessage": "on"}'
run_module "$mods/gated.py" '{"hook_event_name":"PostToolUseFailure"}' ORK_METER_LOOP_GUARD=off
silent "feature off (ORK_METER_LOOP_GUARD=off): prints nothing"
run_module "$mods/gated.py" '{"hook_event_name":"PostToolUseFailure"}' ORK_METER_LOOP_GUARD=0
silent "feature off (=0): prints nothing"
run_module "$mods/raises.py" '{"hook_event_name":"SessionEnd"}'
silent "handler raising: prints nothing, exit 0"
run_module "$mods/quiet.py" '{"hook_event_name":"SessionEnd"}'
silent "handler returning None: prints nothing; BUDGET honoured"
run_module "$mods/broken-import.py" '{"hook_event_name":"SessionEnd"}'
silent "module that fails to import: prints nothing, exit 0"
run_module "$tmp/does-not-exist.py" '{"hook_event_name":"SessionEnd"}'
silent "run.py with a missing script path: prints nothing, exit 0"
out=$(python3 -I -B hooks/lib/run.py </dev/null 2>/dev/null); status=$?
silent "run.py with no argument: prints nothing, exit 0"

# Reply builders: the exact shapes the hooks emit.
pyexpr "reply builders: exact shapes" '
import json, hook
def same(got, want):
    assert json.dumps(got) == json.dumps(want), (json.dumps(got), json.dumps(want))
same(hook.updated_tool_output("PostToolUse", {"stdout": "x"}),
     {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {"stdout": "x"}}})
same(hook.updated_tool_output("PostToolUse", {"stdout": "x"}, "note"),
     {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {"stdout": "x"}, "additionalContext": "note"}})
same(hook.updated_tool_output("PostToolUse", {}, ""),
     {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": {}}})
same(hook.updated_input("PreToolUse", {"file_path": "/f", "limit": 300}, "paged"),
     {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {"file_path": "/f", "limit": 300}, "additionalContext": "paged"}})
same(hook.updated_input("PreToolUse", {"a": 1}),
     {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {"a": 1}}})
same(hook.additional_context("PostToolUseFailure", "again"),
     {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": "again"}})
same(hook.system_message("hello"), {"systemMessage": "hello"})
same(hook.system_message("hello", stop=True), {"systemMessage": "hello", "continue": False})
same(hook.system_message("hello", stop="why"), {"systemMessage": "hello", "continue": False, "stopReason": "why"})
same(hook.system_message(None, stop="why"), {"continue": False, "stopReason": "why"})
same(hook.system_message(None), {})
'

# Context: store() resolves once and remembers the session; record() stamps and appends a constructed ledger row.
pyexprs \
  "ctx.store(): directory created once (0700), session.json written once, store cached" '
import json, os, time, hook, ledger, session
scratch = os.environ["SCRATCH"]
calls = []
real = session.Store.for_payload
session.Store.for_payload = classmethod(lambda cls, payload: calls.append(1) or real(payload))
payload = {"hook_event_name": "PostToolUse", "scratchpad_dir": scratch, "session_id": "sid-1", "transcript_path": "/t/1.jsonl"}
ctx = hook.Context(os.environ, payload, "PostToolUse", time.monotonic() + 1.5)
assert not os.path.exists(os.path.join(scratch, "ork-meter"))
st = ctx.store(); st2 = ctx.store()
assert st is st2 and isinstance(st, session.Store), (st, st2)
a = st.path
assert a == os.path.join(scratch, "ork-meter"), a
assert calls == [1], calls
assert oct(os.stat(a).st_mode & 0o777) == "0o700"
s = st.session_path
assert json.load(open(s)) == {"session_id": "sid-1", "transcript_path": "/t/1.jsonl"}
assert oct(os.stat(s).st_mode & 0o777) == "0o600"
row = ctx.record(ledger.trimmed("Bash", 1000, 400))
assert row["tool"] == "Bash" and row["kind"] == "trim" and row["bytes_before"] == 1000 and row["bytes_after"] == 400
assert row["est_tokens_avoided"] == 250 - 100 and row["session_id"] == "sid-1"
rows = [json.loads(l) for l in open(st.ledger_path) if l.strip()]
assert rows == [row], rows
assert ctx.env is os.environ and ctx.payload is payload and ctx.event == "PostToolUse"
' \
  "ctx.remaining(): decreases, never negative" '
from unittest.mock import patch
import hook
ctx = hook.Context({}, {}, "SessionEnd", 100.2)
with patch.object(hook.time, "monotonic", side_effect=[100.0, 100.05, 101.0]):
    r1 = ctx.remaining(); r2 = ctx.remaining()
    assert 0 < r2 < r1 and abs(r1 - 0.2) < 1e-9, (r1, r2)
    assert ctx.remaining() == 0.0
'

# The real hooks still launch by name through py.sh, as hooks.json names them.
run_raw trim-session.py '{"hook_event_name":"SessionStart","source":"startup","session_id":"s","scratchpad_dir":"'"$SCRATCH"'"}'
assert "py.sh trim-session.py: runs through the runtime" 'o["systemMessage"].startswith("Ork Meter trim: on")'
reset_fixture_policy
run_raw loop-guard.py '{"hook_event_name":"PostToolUseFailure","tool_name":"Bash","tool_input":{"command":"x"},"session_id":"s","scratchpad_dir":"'"$SCRATCH"'"}' ORK_METER_LOOP_WARN=1 ORK_METER_LOOP_STOP=1
assert "py.sh loop-guard.py: composed reply keeps its key order" 'list(o) == ["systemMessage", "hookSpecificOutput", "continue", "stopReason"] and o["continue"] is False'
run_raw nope.py '{"hook_event_name":"SessionStart"}'
silent "py.sh with an unknown script: prints nothing, exit 0"

# The Write guard skips the interpreter while no record exists, and runs once one does.
guarded="$tmp/guarded.py"
python3 -c 'import sys; open(sys.argv[1], "w").write("".join("line %d\n" % i for i in range(60)))' "$guarded"
write_payload='{"hook_event_name":"PreToolUse","tool_name":"Write","session_id":"rt","scratchpad_dir":"'"$SCRATCH"'","tool_use_id":"w1","tool_input":{"file_path":"'"$guarded"'","content":"short"}}'
run_raw write-guard.py "$write_payload"
silent "py.sh write-guard.py with no record: prints nothing (no interpreter started)"
[ ! -d "$XDG_STATE_HOME/ork-meter/read-guard" ] && echo "  ok   no record directory was created" || { echo "  FAIL a record directory appeared"; fails=$((fails + 1)); }
python3 -I -c 'import sys; sys.path.insert(0, "hooks/lib")
import os, write_guard
text = open(sys.argv[1]).read()
write_guard.remember(os.environ, sys.argv[1], text, [(41, 50, "body")], write_guard.view_key({"session_id": "rt"}))' "$guarded"
run_raw write-guard.py "$write_payload"
assert "py.sh write-guard.py with a record: the guard runs and refuses a Write deleting unread lines" 'o["hookSpecificOutput"]["permissionDecision"] == "deny" and "41\u201350 body" in o["hookSpecificOutput"]["permissionDecisionReason"]'

[ "$fails" -eq 0 ]
