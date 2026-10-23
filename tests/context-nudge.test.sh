#!/usr/bin/env bash
# Tests for hooks/context-nudge.py and its acceptance in hooks/trim-session.py
# (issue #60): opt-in, Plus-gated, size-triggered, rate-limited, dismissable,
# person-only, and logged for measurement. All transcripts are synthetic.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

SID="ctx-session-1"
transcript() { # <path> <context tokens>: a synthetic transcript ending in an assistant turn of that size
  python3 - "$1" "$2" <<'EOF'
import json, sys
path, tokens = sys.argv[1], int(sys.argv[2])
with open(path, "w") as f:
    f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "PRIVATE_PROMPT_CANARY"}}) + "\n")
    f.write(json.dumps({"type": "assistant", "message": {"id": "msg_1", "role": "assistant",
        "content": [{"type": "text", "text": "PRIVATE_REPLY_CANARY"}],
        "usage": {"input_tokens": 10, "cache_read_input_tokens": tokens - 510,
                  "cache_creation_input_tokens": 0, "output_tokens": 500}}}) + "\n")
EOF
}
prompt() { # <transcript> [VAR=value ...]
  local path=$1
  shift
  run_raw context-nudge.py '{"hook_event_name":"UserPromptSubmit","session_id":"'"$SID"'","prompt":"PRIVATE_PROMPT_CANARY","transcript_path":"'"$path"'","scratchpad_dir":"'"$SCRATCH"'"}' "$@"
}
start() { # <source> [VAR=value ...]
  local source=$1
  shift
  run_raw trim-session.py '{"hook_event_name":"SessionStart","source":"'"$source"'","session_id":"'"$SID"'","scratchpad_dir":"'"$SCRATCH"'"}' "$@"
}
ON=ORK_METER_CONTEXT_HYGIENE=on
T="$tmp/transcript.jsonl"

transcript "$T" 130000
prompt "$T"
silent "off by default: no nudge without ORK_METER_CONTEXT_HYGIENE"

reset_fixture_policy
transcript "$T" 100000
prompt "$T" "$ON"
silent "on, below the 120000-token threshold: no nudge"

transcript "$T" 130000
prompt "$T" "$ON"
assert "on, at 130000 tokens: one systemMessage for the person only, with the size and both options" \
  'list(o) == ["systemMessage"] and "130,000 tokens" in o["systemMessage"] and "/clear" in o["systemMessage"] and "/compact" in o["systemMessage"] and "PRIVATE" not in out'

prompt "$T" "$ON"
silent "rate limit: the same size again gives no second nudge"
transcript "$T" 180000
prompt "$T" "$ON"
silent "rate limit: growth below the 60000-token step gives no nudge"
transcript "$T" 190000
prompt "$T" "$ON"
assert "rate limit: growth of a full step nudges again" '"190,000 tokens" in o["systemMessage"]'
transcript "$T" 250000
prompt "$T" "$ON"
assert "third nudge" '"250,000 tokens" in o["systemMessage"]'
transcript "$T" 400000
prompt "$T" "$ON"
silent "cap: at most three nudges per session"

python3 - "$SCRATCH/ork-meter/savings.jsonl" "$XDG_STATE_HOME/ork-meter/context-nudges/$SID.json" <<'EOF' && echo "  ok   ledger: three context_nudge rows with sizes; state holds no content" || { echo "  FAIL nudge ledger/state"; fails=$((fails + 1)); }
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
nudges = [r for r in rows if r["kind"] == "context_nudge"]
assert [r["context_tokens"] for r in nudges] == [130000, 190000, 250000], nudges
assert all(r["bytes_before"] == r["bytes_after"] == r["est_tokens_avoided"] == 0 for r in nudges)
state = open(sys.argv[2]).read()
assert "PRIVATE" not in state and json.loads(state)["nudges"] == 3, state
EOF

# Accepted: a /compact (or /clear) start of the same session within the window, once.
start compact "$ON"
start clear "$ON"
python3 - "$SCRATCH/ork-meter/savings.jsonl" <<'EOF' && echo "  ok   accepted: one context_nudge_accepted for the compact, none again for the later clear" || { echo "  FAIL acceptance"; fails=$((fails + 1)); }
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
accepted = [r for r in rows if r["kind"] == "context_nudge_accepted"]
assert len(accepted) == 1 and accepted[0]["action"] == "compact" and accepted[0]["context_tokens"] == 250000, accepted
assert accepted[0]["nudged_session_id"] == "ctx-session-1"
EOF

# Measurement: the active-lever file the runtime labels usage with names the lever only while it is on.
python3 - "$XDG_STATE_HOME/ork-meter/plugin-features.json" <<'EOF' && echo "  ok   active-lever file lists context_hygiene while it is on" || { echo "  FAIL features file (on)"; fails=$((fails + 1)); }
import json, sys
assert "context_hygiene" in json.load(open(sys.argv[1]))["features"]
EOF
reset_fixture_policy
start startup
python3 - "$XDG_STATE_HOME/ork-meter/plugin-features.json" <<'EOF' && echo "  ok   active-lever file omits context_hygiene when it is off" || { echo "  FAIL features file (off)"; fails=$((fails + 1)); }
import json, sys
assert "context_hygiene" not in json.load(open(sys.argv[1]))["features"]
EOF

# A fresh session: dismissing with the switch applies at once; outside the window nothing is accepted.
SID="ctx-session-2"
reset_fixture_policy
transcript "$T" 130000
prompt "$T" "$ON"
assert "second session: nudges again" '"130,000 tokens" in o["systemMessage"]'
transcript "$T" 200000
prompt "$T" ORK_METER_CONTEXT_HYGIENE=off
silent "dismiss: ORK_METER_CONTEXT_HYGIENE=off stops nudges at once, even in a pinned session"
python3 - "$XDG_STATE_HOME/ork-meter/context-nudges/$SID.json" <<'EOF'
import json, sys
p = sys.argv[1]; s = json.load(open(p)); s["ts"] -= 3600; json.dump(s, open(p, "w"))
EOF
start clear "$ON"
python3 - "$SCRATCH/ork-meter/savings.jsonl" <<'EOF' && echo "  ok   accepted only within ORK_METER_CONTEXT_NUDGE_ACCEPT_SECONDS" || { echo "  FAIL window"; fails=$((fails + 1)); }
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
assert not [r for r in rows if r["kind"] == "context_nudge_accepted" and r["nudged_session_id"] == "ctx-session-2"]
EOF

# Per-token default (#110): unset, the nudge is on in a session billed per token,
# decided from metadata when the session is pinned, and never flips mid-session.
SID="ctx-session-api"
reset_fixture_policy
transcript "$T" 130000
prompt "$T" ANTHROPIC_API_KEY=sk-ant-PRIVATE-KEY-CANARY
assert "per-token session (API key present): nudges without ORK_METER_CONTEXT_HYGIENE" \
  '"130,000 tokens" in o["systemMessage"] and "PRIVATE" not in out'
transcript "$T" 200000
prompt "$T"
assert "pinned: the default holds for the session after the signal is gone" '"200,000 tokens" in o["systemMessage"]'
transcript "$T" 270000
prompt "$T" ORK_METER_CONTEXT_HYGIENE=off
silent "an explicit off still wins at once"
SID="ctx-session-sub"
reset_fixture_policy
transcript "$T" 130000
prompt "$T"
silent "no billing signal: off by default"
prompt "$T" ANTHROPIC_API_KEY=x
silent "a signal that appears mid-session does not turn it on"

# Gates: Plus, mode and unreadable transcripts.
SID="ctx-session-3"
reset_fixture_policy
write_entitlement false free
transcript "$T" 300000
prompt "$T" "$ON"
silent "without Ork Meter Plus: no nudge"
write_entitlement true active
reset_fixture_policy
prompt "$T" "$ON" ORK_METER_MODE=shadow
silent "shadow mode: no nudge"
reset_fixture_policy
prompt "$tmp/missing.jsonl" "$ON"
silent "missing transcript: no nudge"
printf 'not json\n{"type":"assistant","message":{"usage":{"input_tokens":"many"}}}\n' > "$tmp/garbled.jsonl"
prompt "$tmp/garbled.jsonl" "$ON"
silent "unrecognised usage: no nudge, never a guess"

# Early exit (#189): run.py ends before it imports the hook library only when
# the lever is certainly off from the environment alone. Unset, it may default
# on per token (#110), so only an explicit off spelling, or a mode that reads
# off, counts. Each step that could turn such a value back on is checked.
if python3 -I -B - <<'PY'
import importlib.util, itertools, os, subprocess, sys
root = os.getcwd()
sys.path.insert(0, os.path.join(root, "hooks", "lib"))
import features, project_env, run, trace
spec = importlib.util.spec_from_file_location("context_nudge", os.path.join(root, "hooks", "context-nudge.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
name = features.OPT_INS[module.FEATURE]
assert run.OFF_WHEN_OFF == {"context-nudge": name}, run.OFF_WHEN_OFF
assert name in features.PER_TOKEN_DEFAULTS.values()
assert (run.ON_VALUES, run.OFF_VALUES, run.MODE_ENV) == (features.ON_VALUES, features.OFF_VALUES, features.MODE_ENV)
# Both early-exit hooks: automatic levers, on an event without shadow capture.
assert {module.FEATURE, "stale_guard"} <= set(features.AUTOMATIC_FEATURES)
assert set(module.EVENTS) == {"UserPromptSubmit"} and not set(module.EVENTS) & set(trace.EVENTS)

spellings = ["on", "off", "OFF", " no ", "0", "false", "1", "yes", "true", "", " ", "maybe", '["off"]', "shadow", "reduce", " Reduce "]
for value in spellings + [None]:
    env = {} if value is None else {name: value, features.MODE_ENV: value}
    assert run.explicitly_off(name, env) == features.is_off(env, name), value
    assert run.mode_off(env) == (features.mode(env) == "off"), value

# Soundness: whatever the pin holds (lever on per token or not, any mode), a
# live value that run.skips treats as off leaves the lever off for the hook.
pins = [features.policy_settings(features.with_billing_defaults({features.MODE_ENV: m}, per_token))
        for m, per_token in itertools.product(("reduce", "shadow", "off"), (True, False))]
skipped = 0
for lever, mode in itertools.product(spellings + [None], spellings + [None]):
    live = {}
    if lever is not None:
        live[name] = lever
    if mode is not None:
        live[features.MODE_ENV] = mode
    if not run.skips("context-nudge", live):
        continue
    skipped += 1
    # A project's settings can't turn it back on: project_env keeps the live value.
    if run.explicitly_off(name, live):
        assert project_env.honoured(name, live[name]), live
    if run.mode_off(live) and features.MODE_ENV in live:
        assert project_env.honoured(features.MODE_ENV, live[features.MODE_ENV]), live
    for per_token in (True, False):   # a new pin, written now
        pinned = features.policy_settings(features.with_billing_defaults(live, per_token))
        assert not features.enabled(features.apply_policy_settings(live, pinned), module.FEATURE), (live, per_token)
    for pinned in pins:               # an existing pin
        assert not features.enabled(features.apply_policy_settings(live, pinned), module.FEATURE), (live, pinned)
    assert not features.enabled(features.pass_through_environment(live), module.FEATURE)
assert skipped > 40, skipped
for live in ({}, {name: "on"}, {name: "maybe"}, {features.MODE_ENV: "shadow"}, {features.MODE_ENV: "reduce"}):
    assert not run.skips("context-nudge", live), live   # may be on: the hook runs

probe = """import io, sys
sys.path.insert(0, sys.argv[1])
import run
sys.stdin = io.TextIOWrapper(io.BytesIO(b'{"hook_event_name": "UserPromptSubmit", "session_id": "early-1"}'))
run.main(["run.py", sys.argv[2]])
print("hook" in sys.modules, sys.stdin.read() == "")
"""
def started(**values):
    child = {k: v for k, v in os.environ.items() if k not in (name, features.MODE_ENV)}
    child.update(values)
    return subprocess.run([sys.executable, "-I", "-B", "-c", probe, os.path.join(root, "hooks", "lib"),
                           os.path.join(root, "hooks", "context-nudge.py")],
                          env=child, capture_output=True, text=True, check=True).stdout.split()
assert started(ORK_METER_CONTEXT_HYGIENE="off") == ["False", "True"], "off: no hook library, payload consumed"
assert started(ORK_METER_MODE="off") == ["False", "True"], "mode off: no hook library"
assert started()[0] == "True", "unset: may default on per token, so the hook runs"
assert started(ORK_METER_CONTEXT_HYGIENE="on")[0] == "True"
assert started(ORK_METER_MODE="shadow")[0] == "True", "a project's shadow could be replaced: the hook runs"
PY
then echo "  ok   early exit: only an explicit off (or mode off) skips the hook library"; else echo "  FAIL early exit"; fails=$((fails + 1)); fi

# End to end through py.sh: in a per-token session with the lever off, nothing
# runs at all (no session store); unset, the same session nudges.
SID="ctx-session-early"
rm -rf "$SCRATCH/ork-meter"
reset_fixture_policy
transcript "$T" 130000
prompt "$T" ANTHROPIC_API_KEY=x ORK_METER_CONTEXT_HYGIENE=off
if [ "$status" -eq 0 ] && [ -z "$out" ] && [ ! -e "$SCRATCH/ork-meter" ]; then
  echo "  ok   py.sh, lever off: the hook never started (no session store)"
else
  echo "  FAIL py.sh, lever off: $status $out"; fails=$((fails + 1))
fi
prompt "$T" ANTHROPIC_API_KEY=x
assert "py.sh, lever unset in a per-token session: the hook runs and nudges" '"130,000 tokens" in o["systemMessage"]'

exit $(( fails > 0 ))
