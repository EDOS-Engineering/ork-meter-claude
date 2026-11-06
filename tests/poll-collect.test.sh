#!/usr/bin/env bash
# End-to-end tests for hooks/polling-completion.py (hooks/lib/polling_runtime.py) through hooks/py.sh:
# Plus gate, kill switch, request window, launch -> poll -> completion receipt, telemetry rows.
#
# The runtime replaced hooks/poll-collect.py. Its request clock is the deadline.json a
# UserPromptSubmit/PostToolBatch hook writes (not the transcript); a launch gets a completion
# receipt and runs in the foreground for the window; a poll of an owned log waits for that
# receipt instead of sleeping. hook.run's emission boundary (hooks/lib/native_text.py
# clean_reply) keeps no additionalContext, so every note the old hook sent the model now stays
# plugin-side: note-/delivery-/wait-*.json under the polling state directory, and the effects
# column of the telemetry row (hooks/lib/hook_telemetry.py, docs/hook-telemetry.md). Bare sleeps
# are no longer shortened and the ledger has no polling kinds.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

if [ "$(python3 -c 'import os; print(os.name)')" = "nt" ]; then
  echo "  ok   skipped: this test drives the launched command through a POSIX bash"
  exit 0
fi

# A resolved cwd: polling_runtime.launch records output paths under the resolved cwd while poll
# compares them against the payload's cwd as given, so a symlinked temp root (macOS /var) would
# never match its own launch.
work=$(mkdir -p "$tmp/work" && python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$tmp/work")
transcript="$tmp/transcript.jsonl"; : > "$transcript"
payload() { # <event> <tool_use_id> <tool_input JSON> [tool_response JSON] [tool_name]
  python3 -c 'import json, sys
event, tid, value, response, name = sys.argv[1:6]
p = {"hook_event_name": event, "session_id": "poll-session", "tool_use_id": tid, "tool_name": name or "Bash",
     "cwd": sys.argv[6], "transcript_path": sys.argv[7], "scratchpad_dir": sys.argv[8]}
if value:
    p["tool_input"] = json.loads(value)
if response:
    p["tool_response"] = json.loads(response)
print(json.dumps(p))' "$1" "$2" "$3" "${4:-}" "${5:-}" "$work" "$transcript" "$SCRATCH"
}
state() { # the one polling state directory of this session
  ls -d "$SCRATCH"/ork-meter/polling/*/ 2>/dev/null | head -1
}
set_origin() { # <seconds ago>: age the request window's origin (polling_runtime.remaining)
  python3 -c 'import json, sys, time
path = sys.argv[1] + "deadline.json"
d = json.load(open(path)); d["origin_monotonic"] = time.monotonic() - float(sys.argv[2])
json.dump(d, open(path, "w"))' "$(state)" "$1"
}
# state_check <name> <python code>: runs with `root` (the state directory), `ident` (polling_runtime.identity)
# and `wait(tool_use_id)` (the parsed wait-*.json receipt of that poll) in scope.
state_check() {
  pyexpr "$1" "
import json, os
from pathlib import Path
import polling_runtime
root = Path('$(state)')
ident = polling_runtime.identity
wait = lambda tid: json.load(open(root/('wait-'+ident(tid)+'.json')))
$2"
}

# The request window starts at a prompt boundary; nothing is printed for it.
run_raw polling-completion.py "$(payload UserPromptSubmit '' '')"
silent "UserPromptSubmit: the request window's origin is written silently"
[ -f "$(state)deadline.json" ] && echo "  ok   deadline.json written" || { echo "  FAIL no deadline.json under $SCRATCH/ork-meter/polling"; fails=$((fails + 1)); }

set_origin 10
run_raw polling-completion.py "$(payload PreToolUse launch '{"command": "python3 job.py > run.log 2>&1", "run_in_background": true}')"
# polling_runtime.launch: the command gains a receipt writer (polling_receipt.py <receipt> <nonce> "$?"),
# runs in the foreground, and its timeout is the remaining window (270 - 10 - 1 s) when none was given.
assert "launch: receipt writer appended, foreground for the remaining window, no note" \
  '(u := o["hookSpecificOutput"]["updatedInput"]) and u["command"].startswith("python3 job.py > run.log 2>&1\n") and "polling_receipt.py" in u["command"] and u["command"].endswith(chr(34) + "$?" + chr(34)) and u["run_in_background"] is False and 250000 <= u["timeout"] <= 260000 and "additionalContext" not in o["hookSpecificOutput"]'
launched=$(OUT="$out" python3 -c 'import json, os; print(json.loads(os.environ["OUT"])["hookSpecificOutput"]["updatedInput"]["command"])')

# The kill switch is the trim lever (polling-completion.py FEATURE = "trim"); ORK_METER_POLLING is gone.
run_raw polling-completion.py "$(payload PreToolUse launch-off '{"command": "python3 job.py > run.log 2>&1", "run_in_background": true}')" ORK_METER_TRIM=off
silent "kill switch: ORK_METER_TRIM=off passes the launch through"
write_entitlement false free
run_raw polling-completion.py "$(payload PreToolUse launch-free '{"command": "python3 job.py > run.log 2>&1", "run_in_background": true}')"
silent "without Plus: passed through"
write_entitlement true active
[ "$(ls "$(state)" | grep -c '^task-')" -eq 1 ] && echo "  ok   only the admitted launch has a task row" || { echo "  FAIL task rows: $(ls "$(state)")"; fails=$((fails + 1)); }

# The launch returned to the background (Claude Code assigned a task ID) before finishing: the ID is
# remembered on the task row so a read of Claude Code's tasks/<id>.output counts as a poll of it.
run_raw polling-completion.py "$(payload PostToolUse launch '{"command": "x", "run_in_background": true}' '{"stdout": "", "backgroundTaskId": "bg42"}')"
silent "launch still running: nothing printed"
state_check "launch still running: the native task ID is recorded" '
row = json.load(open(root/("task-"+ident("launch")+".json")))
assert row["background_id"] == "bg42" and row["cancelled"] is False and row["paths"] == [os.path.join("'"$work"'", "run.log")], row
assert not (root/("note-"+ident("launch")+".json")).exists()'

# The launched command runs its rewritten text in a real shell while the model polls: the poll's sleep
# becomes a wait for the completion receipt (polling_runtime.poll), the read and its suffix are kept.
printf 'import sys, time\ntime.sleep(1.5)\nprint("done")\nsys.exit(4)\n' > "$work/job.py"
(cd "$work" && bash -c "$launched" >/dev/null 2>&1; echo $? > "$tmp/launched.code") &
set_origin 200
run_raw polling-completion.py "$(payload PreToolUse poll1 '{"command": "sleep 540; tail -3 run.log; echo checked"}')"
assert "poll of the owned log: sleep replaced by a completion wait, read and suffix kept, no note" \
  '(u := o["hookSpecificOutput"]["updatedInput"]) and u["command"] == "tail -3 run.log; echo checked" and "additionalContext" not in o["hookSpecificOutput"]'
wait
[ "$(cat "$tmp/launched.code")" = "4" ] && echo "  ok   launched command: original exit status 4 kept" || { echo "  FAIL launched exit $(cat "$tmp/launched.code")"; fails=$((fails + 1)); }
state_check "poll of the owned log: the wait ended at the receipt (exit 4), under the requested sleep" '
w = wait("poll1")
assert w["completion_known"] and w["sleep_removed"] and w["requested_sleep_seconds"] == 540 and not w["cancelled"], w
assert 0.5 < w["waited_seconds"] < 60, w
note = json.load(open(root/("note-"+ident("poll1")+".json")))["text"]
assert "completed with exit 4" in note and "original sleep was not executed" in note, note'
run_raw polling-completion.py "$(payload PreToolUse poll2 '{"command": "sleep 540; tail -3 other.log"}')"
silent "a log no launch owns: native"

# A second launch that never runs: a poll past the window returns the read at once, with the
# completion unknown; one with a sliver of window left waits it out first.
run_raw polling-completion.py "$(payload PreToolUse launch-b '{"command": "python3 job2.py > run2.log", "run_in_background": true}')"
assert "second launch: admitted" 'o["hookSpecificOutput"]["updatedInput"]["run_in_background"] is False'
set_origin 400
run_raw polling-completion.py "$(payload PreToolUse poll3 '{"command": "sleep 540; tail -3 run2.log"}')"
assert "stale request window: the sleep is still removed; the read runs at once" 'o["hookSpecificOutput"]["updatedInput"]["command"] == "tail -3 run2.log"'
state_check "stale request window: completion unknown, no wait" '
w = wait("poll3")
assert not w["completion_known"] and w["sleep_removed"] and w["waited_seconds"] < 1, w
assert "unknown at the request wait deadline" in json.load(open(root/("note-"+ident("poll3")+".json")))["text"]'
set_origin 268.4
run_raw polling-completion.py "$(payload PreToolUse poll3b '{"command": "sleep 540; cat run2.log"}')"
assert "window nearly spent: waits the remainder, then reads" 'o["hookSpecificOutput"]["updatedInput"]["command"] == "cat run2.log"'
state_check "window nearly spent: the wait ran to the deadline" '
w = wait("poll3b")
assert not w["completion_known"] and 0.2 <= w["waited_seconds"] < 5, w'

# The note travels plugin-side: PostToolUse moves it to a delivery row, PostToolBatch consumes it
# and renews the window. Neither prints anything (clean_reply keeps no additionalContext).
run_raw polling-completion.py "$(payload PostToolUse poll1 '{"command": "x"}' '{"stdout": "done"}')"
silent "poll result: nothing printed"
state_check "poll result: the completion note became a delivery row" '
assert not (root/("note-"+ident("poll1")+".json")).exists()
text = json.load(open(root/("delivery-"+ident("poll1")+".json")))["text"]
assert "completed with exit 4" in text, text'
run_raw polling-completion.py "$(python3 -c 'import json, sys; p = json.loads(sys.argv[1]); p["tool_calls"] = [{"tool_use_id": "poll1"}]; print(json.dumps(p))' "$(payload PostToolBatch batch '')")"
silent "PostToolBatch: nothing printed"
state_check "PostToolBatch: the delivery row is consumed and the window renewed" '
import time
assert not (root/("delivery-"+ident("poll1")+".json")).exists()
assert time.monotonic() - json.load(open(root/"deadline.json"))["origin_monotonic"] < 5'

run_raw polling-completion.py "$(payload PreToolUse poll4 '{"command": "sleep 540; cat run.log"}')"
assert "finished task: the whole sleep is dropped" 'o["hookSpecificOutput"]["updatedInput"]["command"] == "cat run.log"'
state_check "finished task: no wait at all" '
w = wait("poll4")
assert w["completion_known"] and w["waited_seconds"] < 1, w'
run_raw polling-completion.py "$(payload PreToolUse nap '{"command": "sleep 900", "timeout": 600000}')"
silent "bare sleep: native (the runtime shortens nothing; sleep is not an admitted launcher)"
run_raw polling-completion.py "$(payload PreToolUse plain '{"command": "ls -la"}')"
silent "unrelated command: native"
run_raw polling-completion.py '{"hook_event_name": "PreToolUse", "tool_name": "Bash"'
silent "malformed payload: nothing printed"

# No ledger kinds for polling any more; the record is the telemetry stream (one start and one
# finish row per hook run, hook-invocations.jsonl) with the scalar polling effects.
pyexpr "telemetry: polling effects per invocation, nothing in the savings ledger" "
import glob, json
rows = [json.loads(l) for p in glob.glob('$SCRATCH/ork-meter/savings.jsonl') for l in open(p) if l.strip()]
assert not [r for r in rows if r['kind'].startswith('polling_')], rows
tele = [json.loads(l) for l in open('$SCRATCH/ork-meter/hook-invocations.jsonl') if l.strip()]
done = [r for r in tele if r['hook'] == 'polling-completion.py' and r['phase'] == 'finished']
assert done and all(r['schema'] == 'claude-hook-telemetry-v1' for r in done), tele[:2]
emitted = [r for r in done if r['reason'] == 'reply-emitted']
assert len(emitted) == 6, [r['reason'] for r in done]   # launch, poll1, launch-b, poll3, poll3b, poll4
assert all(r['stdout_emitted'] and r['effects']['input_updated'] for r in emitted)
polls = [r['effects'] for r in emitted if 'sleep_removed' in r['effects']]
assert len(polls) == 4 and all(p['sleep_removed'] for p in polls), polls
assert sorted(p['completion_known'] for p in polls) == [False, False, True, True], polls
assert all(p['waited_seconds'] >= 0 for p in polls)
assert not any('command' in json.dumps(r) or 'run.log' in json.dumps(r) for r in tele)
"

[ "$fails" -eq 0 ]
