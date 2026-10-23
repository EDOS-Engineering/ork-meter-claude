#!/usr/bin/env bash
# Tests for hooks/session-summary.py (SessionEnd), hooks/agent-stop.py (SubagentStop) and hooks/lib/measure.py.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

summary="$SCRATCH/ork-meter/session-summary.json"

# Build a ledger first: one trim, one dedupe.
run_hook trim-tool-output.py posttooluse.bash.large.json
run_hook dedupe-reads.py posttooluse.read.small.json
run_hook dedupe-reads.py posttooluse.read.small.json

# Haiku agent stop: counted and measured from the agent transcript; other agents ignored.
run_hook agent-stop.py subagentstop.explore.json
silent "SubagentStop for ork-meter:explore: silent"
run_hook agent-stop.py subagentstop.other.json
silent "SubagentStop for another agent: silent"

run_hook session-summary.py sessionend.json
silent "SessionEnd: silent"
if python3 - "$summary" <<'EOF'
import json, sys
o = json.load(open(sys.argv[1]))
est = o["estimated_tokens_avoided"]
assert est["by_kind"]["trim"]["count"] == 1 and est["by_kind"]["dedupe"]["count"] == 1, est
assert est["totals"]["est_tokens_avoided"] == est["by_kind"]["trim"]["est_tokens_avoided"] + est["by_kind"]["dedupe"]["est_tokens_avoided"]
assert "estimated" in est["note"] and "not a billing figure" in est["note"]
m = o["measured_from_transcript"]
# transcript.sample.jsonl: msg_1 (10, 1000 cache read, 200 cache creation, 50 out) written twice, msg_2 (5, 2000, 0, 25), msg_3 without usage
assert m["status"] == "partial" and m["source"] == "transcript", m
assert m["assistant_messages"] == 2 and m["assistant_lines_without_usage"] == 1, m
assert (m["input_tokens"], m["cache_read_input_tokens"], m["cache_creation_input_tokens"], m["output_tokens"]) == (15, 3000, 200, 75), m
assert m["prompt_tokens"] == 3215
assert "measured from transcript" in m["note"]
assert o["features"] == sorted(o["features"]) and "trim" in o["features"]
agents = est["by_kind"]["haiku_agent"]
assert agents["count"] == 1 and agents["bytes_before"] == 0
EOF
then echo "  ok   summary: estimates per kind, measured usage deduped by message id, labelled"; else echo "  FAIL summary content"; fails=$((fails + 1)); fi
[ "$(file_mode "$summary")" = "600" ] && echo "  ok   summary mode 600" || { echo "  FAIL summary mode"; fails=$((fails + 1)); }

if python3 - "$SCRATCH/ork-meter/savings.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
agent = [r for r in rows if r["kind"] == "haiku_agent"]
assert len(agent) == 1 and agent[0]["agent_type"] == "ork-meter:explore", agent
assert agent[0]["measured_status"] == "partial" and agent[0]["measured_output_tokens"] == 75 and agent[0]["measured_cache_read_input_tokens"] == 3000
assert "last_assistant_message" not in agent[0] and "Found it" not in json.dumps(agent[0])
EOF
then echo "  ok   ledger: haiku_agent row measured from the agent transcript, no message text"; else echo "  FAIL haiku_agent row"; fails=$((fails + 1)); fi

# Unreadable or oddly shaped transcripts say unavailable, never invent numbers.
TRANSCRIPT="$tmp/missing.jsonl" run_hook session-summary.py sessionend.json
python3 -c 'import json,sys; m=json.load(open(sys.argv[1]))["measured_from_transcript"]; assert m["status"]=="unavailable" and "not readable" in m["reason"], m' "$summary" \
  && echo "  ok   missing transcript: unavailable with reason" || { echo "  FAIL missing transcript"; fails=$((fails + 1)); }
printf '{"type":"assistant","message":{"id":"m","content":[]}}\n{"type":"user"}\n' > "$tmp/nousage.jsonl"
TRANSCRIPT="$tmp/nousage.jsonl" run_hook session-summary.py sessionend.json
python3 -c 'import json,sys; m=json.load(open(sys.argv[1]))["measured_from_transcript"]; assert m["status"]=="unavailable" and "shape not recognised" in m["reason"], m' "$summary" \
  && echo "  ok   assistant lines without usage: unavailable, shape not recognised" || { echo "  FAIL no-usage transcript"; fails=$((fails + 1)); }

# Time: a 15 MB transcript still finishes inside the SessionEnd budget (scan stops at 1 s and says partial).
python3 - "$tmp/huge.jsonl" <<'EOF'
import json, sys
with open(sys.argv[1], "w") as f:
    row = {"type": "assistant", "requestId": "r", "message": {"id": "msg_%d", "usage": {"input_tokens": 1, "cache_read_input_tokens": 1, "cache_creation_input_tokens": 1, "output_tokens": 1}, "content": [{"type": "text", "text": "y" * 2000}]}}
    for i in range(6000):
        row["message"]["id"] = "msg_%d" % i
        f.write(json.dumps(row) + "\n")
EOF
start=$(python3 -c 'import time; print(int(time.time()*1000))')
TRANSCRIPT="$tmp/huge.jsonl" run_hook session-summary.py sessionend.json
end=$(python3 -c 'import time; print(int(time.time()*1000))')
ms=$((end - start))
if [ "$ms" -lt 1400 ] && python3 -c 'import json,sys; m=json.load(open(sys.argv[1]))["measured_from_transcript"]; assert m["status"] in ("ok","partial"), m' "$summary"; then
  echo "  ok   15 MB transcript: ${ms} ms (SessionEnd budget 1.5 s)"; else echo "  FAIL large transcript took ${ms} ms"; fails=$((fails + 1)); fi

run_raw session-summary.py '[]'
silent "non-object stdin: silent, exit 0"

[ "$fails" -eq 0 ]
