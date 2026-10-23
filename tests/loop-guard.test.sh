#!/usr/bin/env bash
# Tests for hooks/loop-guard.py (PostToolUseFailure): repeated identical failures (#63).
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

run_hook loop-guard.py posttoolusefailure.bash.json
silent "first failure: silent"
run_hook loop-guard.py posttoolusefailure.bash.json
silent "second failure: silent"
run_hook loop-guard.py posttoolusefailure.bash.json
assert "third identical failure: systemMessage and additionalContext, no stop" '"failed 3 times" in o["systemMessage"] and o["hookSpecificOutput"]["hookEventName"] == "PostToolUseFailure" and "3 times" in o["hookSpecificOutput"]["additionalContext"] and "continue" not in o'
run_hook loop-guard.py posttoolusefailure.bash.json
silent "fourth: silent (warns again at multiples of 3)"

# A different input is a different counter.
other=$(sed 's/npm test/npm run lint/' tests/fixtures/posttoolusefailure.bash.json | sed "s#__SCRATCH__#$SCRATCH#g")
run_raw loop-guard.py "$other"
silent "different input: its own counter, silent"

# Interrupts don't count.
intr=$(sed 's/"is_interrupt": false/"is_interrupt": true/' tests/fixtures/posttoolusefailure.bash.json | sed "s#__SCRATCH__#$SCRATCH#g")
run_raw loop-guard.py "$intr"
silent "interrupted call: not counted"

# Hard stop at a user-set threshold; never without it.
reset_fixture_policy
run_hook loop-guard.py posttoolusefailure.bash.json ORK_METER_LOOP_STOP=5
assert "ORK_METER_LOOP_STOP=5 at the fifth failure: continue false with a reason" 'o["continue"] is False and "ORK_METER_LOOP_STOP=5" in o["stopReason"]'
run_hook loop-guard.py posttoolusefailure.bash.json ORK_METER_LOOP_STOP=off
assert "sixth failure, immediate stop opt-out: warning only" '"continue" not in o and "failed 6 times" in o["systemMessage"]'
run_hook loop-guard.py posttoolusefailure.bash.json ORK_METER_LOOP_GUARD=off
silent "ORK_METER_LOOP_GUARD=off: silent"
run_hook loop-guard.py posttoolusefailure.bash.json ORK_METER_LOOP_WARN=2 ORK_METER_LOOP_STOP=off
assert "ORK_METER_LOOP_WARN=2: warns at multiples of 2 (count 7 isn't one)" 'out == ""' || true

# Counters don't store the input, and reset at session start.
if python3 -c '
import json, sys
c = json.load(open(sys.argv[1]))
assert all(len(k) == 64 for k in c), c
assert "npm" not in json.dumps(c)' "$SCRATCH/ork-meter/failures.json"; then echo "  ok   counters keyed by hash, no input stored"; else echo "  FAIL counters"; fails=$((fails + 1)); fi
reset_fixture_policy
run_hook trim-session.py sessionstart.startup.json
run_hook loop-guard.py posttoolusefailure.bash.json
silent "after session start: counter reset, first failure silent"
run_hook loop-guard.py posttoolusefailure.bash.json ORK_METER_LOOP_STOP=0
silent "ORK_METER_LOOP_STOP=0 means unset: second failure, no stop"
run_hook loop-guard.py posttoolusefailure.bash.json ORK_METER_LOOP_STOP=off ORK_METER_LOOP_WARN=4
assert "stop opt-out is immediate; warn override is deferred: third failure warns without stopping" '"continue" not in o and "failed 3 times" in o["systemMessage"]'

# Ledger rows: loop_warning and loop_stop, zero bytes.
if python3 - "$SCRATCH/ork-meter/savings.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
kinds = [r["kind"] for r in rows]
assert kinds.count("loop_warning") == 3 and kinds.count("loop_stop") == 1, kinds
assert all(r["bytes_before"] == 0 and r["est_tokens_avoided"] == 0 for r in rows)
EOF
then echo "  ok   ledger: 3 loop_warning, 1 loop_stop"; else echo "  FAIL ledger"; fails=$((fails + 1)); fi

run_raw loop-guard.py '{'
silent "malformed stdin: silent, exit 0"

# Parallel identical failures are all counted (#131). Six processes run the
# handler at once; each read of failures.json is slowed so that, unlocked,
# every one of them reads the same count and all but one update is lost.
# The lock wait is raised so the result doesn't depend on machine load.
if python3 - "$SCRATCH" "$tmp/race" <<'EOF2'
import json, os, subprocess, sys, time
scratch, race = sys.argv[1], sys.argv[2]
os.makedirs(race)
payload = json.loads(open("tests/fixtures/posttoolusefailure.bash.json").read().replace("__SCRATCH__", race))
payload["session_id"] = "race"
child = r"""
import importlib.util, json, os, sys, time
sys.path.insert(0, "hooks/lib")
import features, hook, session
read = session.read_json
def slow(path, default=None):
    value = read(path, default)
    time.sleep(0.01)
    return value
session.read_json = slow
session.LOCK_WAIT_SECONDS = 30   # a loaded runner must not reach the wait limit and run unlocked
spec = importlib.util.spec_from_file_location("loop_guard", "hooks/loop-guard.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ctx = hook.Context({}, json.loads(sys.argv[1]), "PostToolUseFailure", time.monotonic() + 10)
while not os.path.exists(sys.argv[2]):
    time.sleep(0.001)
module.handle(ctx)
"""
go = os.path.join(race, "go")
procs = [subprocess.Popen([sys.executable, "-I", "-B", "-c", child, json.dumps(payload), go]) for _ in range(6)]
time.sleep(1.0)
open(go, "w").close()
assert all(p.wait(30) == 0 for p in procs)
store = os.path.join(race, "ork-meter", "failures.json")
counts = json.load(open(store))
assert [e["count"] for e in counts.values()] == [6], counts
EOF2
then echo "  ok   six parallel identical failures: all six counted"; else echo "  FAIL parallel failures undercounted"; fails=$((fails + 1)); fi

[ "$fails" -eq 0 ]
