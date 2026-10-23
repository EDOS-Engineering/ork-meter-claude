#!/usr/bin/env bash
# Tests for hooks/stale-context.py (issue #101): opt-in, Plus-gated, held only
# when the cache expired and the context is large, once per idle episode,
# dismissable, and logged for measurement. All transcripts are synthetic.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

SID="stale-session-1"
transcript() { # <path> <context tokens> <minutes ago> <ttl 1h|5m|none> [sidechain minutes ago]
  python3 - "$@" <<'EOF'
import datetime, json, sys
path, tokens, ago, ttl = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
side = float(sys.argv[5]) if len(sys.argv) > 5 else None
now = datetime.datetime.now(datetime.timezone.utc)
stamp = lambda minutes: (now - datetime.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
split = {"1h": {"ephemeral_1h_input_tokens": 900, "ephemeral_5m_input_tokens": 0},
         "5m": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 900}}.get(ttl)
usage = {"input_tokens": 10, "cache_read_input_tokens": tokens - 1410, "cache_creation_input_tokens": 900, "output_tokens": 500}
if split:
    usage["cache_creation"] = split
with open(path, "w") as f:
    f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "PRIVATE_PROMPT_CANARY"}}) + "\n")
    f.write(json.dumps({"type": "assistant", "timestamp": stamp(ago), "message": {"id": "msg_1", "role": "assistant",
        "content": [{"type": "text", "text": "PRIVATE_REPLY_CANARY"}], "usage": usage}}) + "\n")
    if side is not None:
        f.write(json.dumps({"type": "assistant", "isSidechain": True, "timestamp": stamp(side), "message": {"id": "msg_2",
            "role": "assistant", "content": [], "usage": dict(usage, cache_read_input_tokens=100)}}) + "\n")
EOF
}
prompt() { # <transcript> [VAR=value ...]
  local path=$1
  shift
  run_raw stale-context.py '{"hook_event_name":"UserPromptSubmit","session_id":"'"$SID"'","prompt":"PRIVATE_PROMPT_CANARY","transcript_path":"'"$path"'","scratchpad_dir":"'"$SCRATCH"'"}' "$@"
}
ON=ORK_METER_STALE_GUARD=on
T="$tmp/transcript.jsonl"
HELD='o is not None and o.get("decision") == "block" and "PRIVATE" not in out'

transcript "$T" 260000 120 1h
prompt "$T"
silent "off by default: no hold without ORK_METER_STALE_GUARD"

reset_fixture_policy
transcript "$T" 260000 30 1h
prompt "$T" "$ON"
silent "on, 1-hour cache still warm (30 minutes): no hold"

transcript "$T" 60000 120 1h
prompt "$T" "$ON"
silent "on, cache expired but context below 100000 tokens: no hold"

transcript "$T" 260000 120 1h
prompt "$T" "$ON"
assert "on, 1-hour cache expired 2 hours ago at 260000 tokens: held with size, idle time and all three choices" \
  "$HELD"' and "260,000 tokens" in o["reason"] and "2 hours" in o["reason"] and "/clear" in o["reason"] and "/compact" in o["reason"] and "submit the prompt again" in o["reason"]'

prompt "$T" "$ON"
silent "resubmitted in the same idle episode: let through"
prompt "$T" "$ON"
silent "and again: still let through"

python3 - "$SCRATCH/ork-meter/savings.jsonl" "$XDG_STATE_HOME/ork-meter/stale-guard/$SID.json" <<'EOF' && echo "  ok   ledger: one stale_guard and one stale_guard_continued with sizes; state holds no content" || { echo "  FAIL ledger/state"; fails=$((fails + 1)); }
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
held = [r for r in rows if r["kind"] == "stale_guard"]
cont = [r for r in rows if r["kind"] == "stale_guard_continued"]
assert len(held) == 1 and len(cont) == 1, rows
assert held[0]["context_tokens"] == 260000 and 119 <= held[0]["idle_minutes"] <= 121, held
assert all(r["bytes_before"] == r["bytes_after"] == r["est_tokens_avoided"] == 0 for r in held + cont)
state = open(sys.argv[2]).read()
assert "PRIVATE" not in state and json.loads(state)["continued"] is True, state
EOF

transcript "$T" 300000 180 1h
prompt "$T" "$ON"
assert "a new idle episode (newer last reply, expired again): held again" "$HELD"' and "300,000 tokens" in o["reason"]'

SID="stale-session-2"
reset_fixture_policy
transcript "$T" 150000 10 5m
prompt "$T" "$ON"
assert "5-minute cache, 10 minutes idle: held" "$HELD"' and "10 minutes" in o["reason"]'

SID="stale-session-3"
reset_fixture_policy
transcript "$T" 150000 10 none
prompt "$T" "$ON"
silent "no cache split in the tail: assumes the 1-hour lifetime, so 10 minutes is not held"

SID="stale-session-4"
reset_fixture_policy
transcript "$T" 260000 120 1h 1
prompt "$T" "$ON"
assert "a recent subagent (sidechain) turn does not hide the expired main conversation" "$HELD"

SID="stale-session-5"
reset_fixture_policy
transcript "$T" 260000 4000 1h
prompt "$T" "$ON"
assert "idle for days is reported in days" "$HELD"' and "2 days" in o["reason"]'

SID="stale-session-6"
reset_fixture_policy
prompt "$T" ORK_METER_STALE_GUARD=off
silent "dismiss: ORK_METER_STALE_GUARD=off stops it at once"

# Compaction after the last turn: the context it described is gone (review of #102).
SID="stale-session-8"
reset_fixture_policy
transcript "$T" 260000 120 1h
printf '%s\n' '{"type":"system","subtype":"compact_boundary","timestamp":"2026-10-15T00:00:00Z"}' '{"type":"user","isCompactSummary":true,"message":{"content":"PRIVATE_SUMMARY_CANARY"}}' >> "$T"
prompt "$T" "$ON"
silent "compacted after the last turn: no hold, no stale size quoted"

# Claude Code's own <synthetic> error row last: the real turn before it decides.
SID="stale-session-9"
reset_fixture_policy
transcript "$T" 260000 120 1h
printf '%s\n' '{"type":"assistant","timestamp":"2099-01-01T00:00:00Z","message":{"id":"syn","model":"<synthetic>","content":[],"usage":{"input_tokens":0,"cache_read_input_tokens":0,"cache_creation_input_tokens":0,"output_tokens":0}}}' >> "$T"
prompt "$T" "$ON"
assert "a trailing <synthetic> row is skipped: held on the real turn" "$HELD"' and "260,000 tokens" in o["reason"]'

# Unattended prompts are never held: nobody would resubmit them (#329 review).
SID="stale-session-unattended"
reset_fixture_policy
transcript "$T" 260000 120 1h
# Each case gets its own idle episode (a different last-reply time), so a pass is never a resubmission.
ago=120
for entry in sdk-cli sdk-ts sdk-py; do
  ago=$((ago + 1)); transcript "$T" 260000 $ago 1h
  prompt "$T" "$ON" CLAUDE_CODE_ENTRYPOINT=$entry
  silent "non-interactive session ($entry), cache expired: let through"
done
harness_prompt() { # <prompt text> [VAR=value ...]
  local text=$1
  shift
  run_raw stale-context.py '{"hook_event_name":"UserPromptSubmit","session_id":"'"$SID"'","prompt":"'"$text"'","transcript_path":"'"$T"'","scratchpad_dir":"'"$SCRATCH"'"}' "$@"
}
ago=$((ago + 1)); transcript "$T" 260000 $ago 1h
harness_prompt "<task-notification> <task-id>b1</task-id> PRIVATE_PROMPT_CANARY" "$ON"
silent "a background task's notification, cache expired: let through"
ago=$((ago + 1)); transcript "$T" 260000 $ago 1h
harness_prompt "  <cross-session-message from=peer> PRIVATE_PROMPT_CANARY" "$ON"
silent "another session's message, cache expired: let through"
ago=$((ago + 1)); transcript "$T" 260000 $ago 1h
harness_prompt "PRIVATE_PROMPT_CANARY <task-notification> later in the text" "$ON" CLAUDE_CODE_ENTRYPOINT=cli
assert "the marker only counts at the start; an interactive cli prompt is held, so the passes above were the guard's choice" "$HELD"

# With the context nudge also on, the guard speaks for an expired prompt and the nudge stays quiet.
SID="stale-session-10"
reset_fixture_policy
transcript "$T" 260000 120 1h
nudge() { run_raw context-nudge.py '{"hook_event_name":"UserPromptSubmit","session_id":"'"$SID"'","prompt":"PRIVATE_PROMPT_CANARY","transcript_path":"'"$T"'","scratchpad_dir":"'"$SCRATCH"'"}' "$@"; }
nudge "$ON" ORK_METER_CONTEXT_HYGIENE=on
silent "both on, cache expired: the context nudge defers to the guard"
nudge "$ON" ORK_METER_CONTEXT_HYGIENE=on CLAUDE_CODE_ENTRYPOINT=sdk-cli
assert "both on, non-interactive: the guard won't hold, so the context nudge speaks" '"260,000 tokens" in o["systemMessage"]'
SID="stale-session-11"
reset_fixture_policy
nudge ORK_METER_CONTEXT_HYGIENE=on
assert "guard off: the context nudge still speaks" '"260,000 tokens" in o["systemMessage"]'
SID="stale-session-12"
reset_fixture_policy
transcript "$T" 260000 30 1h
nudge "$ON" ORK_METER_CONTEXT_HYGIENE=on
assert "both on, cache still warm: the context nudge speaks" '"260,000 tokens" in o["systemMessage"]'

# Gates: Plus, mode and unreadable transcripts.
SID="stale-session-7"
reset_fixture_policy
write_entitlement false free
transcript "$T" 260000 120 1h
prompt "$T" "$ON"
silent "without Ork Meter Plus: no hold"
write_entitlement true active
reset_fixture_policy
prompt "$T" "$ON" ORK_METER_MODE=shadow
silent "shadow mode: no hold"
reset_fixture_policy
prompt "$tmp/missing.jsonl" "$ON"
silent "missing transcript: no hold"
printf 'not json\n{"type":"assistant","timestamp":"yesterday","message":{"usage":{"input_tokens":1,"cache_read_input_tokens":900000,"cache_creation_input_tokens":0,"output_tokens":1}}}\n' > "$tmp/garbled.jsonl"
prompt "$tmp/garbled.jsonl" "$ON"
silent "unreadable timestamp: no hold, never a guess"

# Early exit (#189): with the lever off in the hook's environment, hooks/lib/run.py
# ends before it imports the hook library; on, the hook runs.
if python3 -I -B - <<'EOF'
import importlib.util, os, subprocess, sys
root = os.getcwd()
sys.path.insert(0, os.path.join(root, "hooks", "lib"))
import features, run
spec = importlib.util.spec_from_file_location("stale_context", os.path.join(root, "hooks", "stale-context.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert run.OFF_UNLESS_ON == {"stale-context": features.OPT_INS[module.FEATURE]}, run.OFF_UNLESS_ON
# An opt-in that can default on (per token, #110) is never certainly off from the environment alone.
assert not set(run.OFF_UNLESS_ON.values()) & set(getattr(features, "PER_TOKEN_DEFAULTS", {}).values())
assert run.ON_VALUES == features.ON_VALUES
for value in (None, "", "off", "0", "maybe", '["on"]'):
    assert run.certainly_off("ORK_METER_STALE_GUARD", {} if value is None else {"ORK_METER_STALE_GUARD": value}), value
for value in ("on", "ON", " 1 ", "true", "yes"):
    assert not run.certainly_off("ORK_METER_STALE_GUARD", {"ORK_METER_STALE_GUARD": value}), value

# main(): a fresh interpreter each, so sys.modules shows whether the hook library loaded.
probe = """import io, sys
sys.path.insert(0, sys.argv[1])
import run
sys.stdin = io.TextIOWrapper(io.BytesIO(b'{"hook_event_name": "UserPromptSubmit", "session_id": "early-1"}'))
run.main(["run.py", sys.argv[2]])
print("hook" in sys.modules, sys.stdin.read() == "")
"""
def started(script, value=None):
    child = {k: v for k, v in os.environ.items()
             if k not in ("ORK_METER_STALE_GUARD", "ORK_METER_CONTEXT_HYGIENE", "ORK_METER_MODE")}
    if value is not None:
        child["ORK_METER_STALE_GUARD"] = value
    return subprocess.run([sys.executable, "-I", "-B", "-c", probe, os.path.join(root, "hooks", "lib"),
                           os.path.join(root, "hooks", script)],
                          env=child, capture_output=True, text=True, check=True).stdout.split()
assert started("stale-context.py") == ["False", "True"], "off: no hook library, payload consumed"
assert started("stale-context.py", "off")[0] == "False"
assert started("stale-context.py", "on")[0] == "True", "on: the hook runs"
assert started("context-nudge.py")[0] == "True", "other hooks are unaffected"
for value in ("off", "", "reduce-ish"):   # mode reads off: no automatic lever runs
    child = dict(os.environ, ORK_METER_STALE_GUARD="on", ORK_METER_MODE=value)
    assert run.skips("stale-context", child), value
assert not run.skips("stale-context", dict(os.environ, ORK_METER_STALE_GUARD="on", ORK_METER_MODE="shadow"))
EOF
then echo "  ok   early exit: lever off skips the hook library; on runs it"; else echo "  FAIL early exit"; fails=$((fails + 1)); fi

# End to end through py.sh: off, nothing runs at all; on, the hook binds the
# session (its pin appears) and holds.
SID="stale-session-early"
rm -rf "$SCRATCH/ork-meter"
transcript "$T" 260000 120 1h
prompt "$T"
if [ "$status" -eq 0 ] && [ -z "$out" ] && [ ! -e "$SCRATCH/ork-meter" ]; then
  echo "  ok   py.sh, lever off: the hook never started (no session store)"
else
  echo "  FAIL py.sh, lever off: $status $out"; fails=$((fails + 1))
fi
prompt "$T" "$ON"
assert "py.sh, lever on: the hook runs and holds" "$HELD"
if [ -f "$SCRATCH/ork-meter/session-policy.json" ]; then
  echo "  ok   py.sh, lever on: the session was bound (its pin was written)"
else
  echo "  FAIL py.sh, lever on: no session pin"; fails=$((fails + 1))
fi

exit $(( fails > 0 ))
