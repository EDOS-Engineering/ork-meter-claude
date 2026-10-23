#!/usr/bin/env bash
# Tests for scripts/cache_report.py: measured token totals, misses attributed
# by cause, project scoping, --json, malformed input, and no message content.
# All transcripts are synthetic.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

export CLAUDE_CONFIG_DIR="$tmp/claude"
PROJECT="$tmp/work/my.project"
mkdir -p "$PROJECT"
# Named the way Claude Code names it: from the real path (/var is /private/var on macOS).
DIR="$CLAUDE_CONFIG_DIR/projects/$(python3 -c 'import os,re,sys; print(re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(sys.argv[1])))' "$PROJECT")"
mkdir -p "$DIR" "$CLAUDE_CONFIG_DIR/projects/-other-project"

python3 - "$DIR/session.jsonl" "$CLAUDE_CONFIG_DIR/projects/-other-project/other.jsonl" <<'EOF'
import datetime, json, sys
t0 = datetime.datetime(2026, 10, 14, 9, 0, tzinfo=datetime.timezone.utc)
stamp = lambda minutes: (t0 + datetime.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
def turn(f, mid, minutes, read, write, model="claude-opus-5-5", effort="high", ttl="1h", out=100, side=False):
    split = {"ephemeral_1h_input_tokens": write if ttl == "1h" else 0, "ephemeral_5m_input_tokens": write if ttl == "5m" else 0}
    usage = {"input_tokens": 5, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
             "output_tokens": out, "cache_creation": split}
    row = {"type": "assistant", "timestamp": stamp(minutes), "effort": effort, "isSidechain": side,
           "message": {"id": mid, "model": model, "content": [{"type": "text", "text": "PRIVATE_REPLY_CANARY"}], "usage": usage}}
    f.write(json.dumps(row) + "\n")
    f.write(json.dumps(row) + "\n")  # a streamed message repeats its usage; counted once
with open(sys.argv[1], "w") as f:
    f.write(json.dumps({"type": "user", "timestamp": stamp(0), "message": {"content": "PRIVATE_PROMPT_CANARY"}}) + "\n")
    f.write(json.dumps({"type": "api-request", "timestamp": stamp(0), "shapeHash": "A"}) + "\n")
    turn(f, "m1", 0, 0, 20000)                       # first request
    turn(f, "m2", 2, 20000, 1000)                    # warm
    turn(f, "m3", 180, 0, 21500)                     # idle 178 min past the 1-hour cache
    turn(f, "m4", 182, 0, 22000, model="claude-sonnet-5-5")       # model switch
    turn(f, "m5", 184, 0, 22500, model="claude-sonnet-5-5", effort="low")  # effort change
    f.write(json.dumps({"type": "api-request", "timestamp": stamp(185), "shapeHash": "B"}) + "\n")
    turn(f, "m6", 186, 0, 23000, model="claude-sonnet-5-5", effort="low")  # shape changed
    f.write(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": stamp(187)}) + "\n")
    turn(f, "m7", 188, 0, 9000, model="claude-sonnet-5-5", effort="low")   # after compaction
    turn(f, "m8", 190, 0, 9500, model="claude-sonnet-5-5", effort="low")   # unexplained
    turn(f, "m9", 200, 0, 9000, model="claude-sonnet-5-5", effort="low", ttl="5m")  # idle past 5-minute cache? previous 1h write -> uses this turn's split
    turn(f, "g1", 201, 9000, 30000, model="claude-sonnet-5-5", effort="low", ttl="5m")  # context grew, prefix read: not a miss
    turn(f, "s1", 3, 0, 15000, side=True)            # subagent stream: its own first request
    f.write("not json\n")
    f.write(json.dumps({"type": "assistant", "timestamp": "never", "message": {"id": "bad", "usage": {"input_tokens": 1}}}) + "\n")
    f.write(json.dumps({"type": "assistant", "timestamp": stamp(1), "message": {"id": "syn", "model": "<synthetic>", "usage": {"input_tokens": 99}}}) + "\n")
with open(sys.argv[2], "w") as f:
    turn(f, "o1", 0, 0, 10000, model="claude-mystery-9")
    turn(f, "o2", 1, 10000, 1000, model="claude-haiku-4-5-20251001")   # dated id: Haiku 4.5 prices
    turn(f, "m1", 0, 0, 20000)   # a forked session repeats an earlier message: counted once
EOF

out=$(python3 -I -B scripts/cache_report.py --cwd "$PROJECT" --json); status=$?
assert "project scope: 1 transcript, 11 requests, streamed copies, bad and synthetic rows skipped" \
  'o["transcripts"] == 1 and o["requests"] == 11 and o["scope"].endswith("my.project")'
assert "measured token totals" \
  'o["tokens"] == {"input": 55, "cache_read": 29000, "cache_write": 182500, "output": 1100}'
assert "each cause attributed once; a context that grew and read its prefix is not a miss" \
  'sorted((k, v["misses"]) for k, v in o["causes"].items()) == sorted([("idle past the 60-minute cache", 1), ("model switch", 1), ("effort change", 1), ("tools, system prompt or MCP changed", 1), ("after compaction", 1), ("unexplained", 1), ("idle past the 5-minute cache", 1)])'
assert "first requests are reported separately, not as misses" 'o["first_requests"]["requests"] == 2 and o["first_requests"]["tokens_written"] == 35000'
assert "idle miss carries its size and a list-price write cost (2x Opus input at 1-hour TTL)" \
  'o["causes"]["idle past the 60-minute cache"]["tokens_written"] == 21500 and abs(o["causes"]["idle past the 60-minute cache"]["write_cost"] - 21500 * 8 / 1e6) < 0.01'
assert "gap bands" 'o["gaps"] == {"under_5_min": 7, "5_to_60_min": 1, "over_60_min": 1}'
assert "no content in the report" '"PRIVATE" not in out'

out=$(python3 -I -B scripts/cache_report.py --cwd "$PROJECT"); status=$?
assert "text report: labels measured tokens and estimated dollars, lists causes" \
  '"Estimated cost at API list prices" in out and "idle past the 60-minute cache" in out and "Token counts are measured" in out and "not a bill" in out and "PRIVATE" not in out'

out=$(python3 -I -B scripts/cache_report.py --all --json); status=$?
assert "--all covers every project, counts a forked repeat once, prices a dated id, flags an unrecognised model" \
  'o["transcripts"] == 2 and o["requests"] == 13 and o["unpriced_models"] == ["claude-mystery-9"]'

# Per-model prices (review of #103): Opus 5 is $5 base input, not Opus 5.5's $4.
out=$(python3 -I -B -c 'import sys; sys.path.insert(0, "scripts"); import cache_report as c
print(c.PRICES[c.priced("claude-opus-5")][0], c.PRICES[c.priced("claude-opus-5-5")][0], c.PRICES[c.priced("claude-haiku-4-5-20251001")][0], c.priced("claude-opus-50"))'); status=$?
assert "prices by model id, longest prefix, dated ids; no false prefix match" 'out.split() == ["5.0", "4.0", "1.0", "None"]'

# Haiku 5.5 has a second rate card above a 100K-token prompt (#132): $0.50 / $2.50, cache reads 0.1x.
out=$(python3 -I -B -c 'import sys; sys.path.insert(0, "scripts"); import cache_report as c
print(*c.prices("claude-haiku-5-5", 100000), *c.prices("claude-haiku-5-5", 100001), *c.prices("claude-opus-5-5", 900000))'); status=$?
assert "Haiku 5.5 long-prompt card above 100K prompt tokens only; other models unchanged" \
  'out.split() == ["0.1", "0.01", "0.5", "0.5", "0.05", "2.5", "4.0", "0.2", "20.0"]'

# From a subdirectory, and through a symlink, the project is still found.
mkdir -p "$PROJECT/src/deep"
ln -s "$PROJECT" "$tmp/link"
out=$(cd "$PROJECT/src/deep" && python3 -I -B "$OLDPWD/scripts/cache_report.py" --json); status=$?
assert "run from a subdirectory: walks up to the project" 'o["requests"] == 11'
out=$(python3 -I -B scripts/cache_report.py --cwd "$tmp/link" --json); status=$?
assert "a symlinked path resolves to the real project folder" 'o["requests"] == 11'

out=$(python3 -I -B scripts/cache_report.py --cwd "$tmp/nowhere"); status=$?
assert "a project with no transcripts says so" '"No Claude Code transcripts with usage found." in out'

out=$(python3 -I -B scripts/cache_report.py --all --cwd "$PROJECT" 2>&1); status=$?
if [ "$status" -ne 0 ]; then echo "  ok   --all and --cwd together are refused"; else echo "  FAIL --all with --cwd accepted"; fails=$((fails + 1)); fi

# A resumed session copies earlier messages into a new file (#132). Whichever
# file comes first, the first new request after an idle resume is an idle miss,
# and the copied messages are counted once.
python3 - "$tmp/resume" <<'EOF'
import datetime, json, os, sys
t0 = datetime.datetime(2026, 10, 14, 9, 0, tzinfo=datetime.timezone.utc)
def row(mid, minutes, read, write):
    usage = {"input_tokens": 5, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
             "output_tokens": 100, "cache_creation": {"ephemeral_5m_input_tokens": write, "ephemeral_1h_input_tokens": 0}}
    when = (t0 + datetime.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    return json.dumps({"type": "assistant", "timestamp": when,
                       "message": {"id": mid, "model": "claude-opus-5-5", "usage": usage}}) + "\n"
original = row("r1", 0, 0, 20000) + row("r2", 2, 20000, 1000)
resumed = original + row("r3", 30, 0, 21500)   # 28 minutes idle: past the 5-minute cache
for order, files in (("a", (original, resumed)), ("b", (resumed, original))):
    os.makedirs(os.path.join(sys.argv[1], order))
    for name, text in zip(("1.jsonl", "2.jsonl"), files):
        with open(os.path.join(sys.argv[1], order, name), "w") as f:
            f.write(text)
EOF
out=$(python3 -I -B -c 'import json, sys; sys.path.insert(0, "scripts"); import cache_report as c
d = sys.argv[1]
print(json.dumps([c.analyse([d + "/a/1.jsonl", d + "/a/2.jsonl"]), c.analyse([d + "/b/1.jsonl", d + "/b/2.jsonl"])]))' "$tmp/resume"); status=$?
assert "resumed session: same report in either file order, idle miss counted, copies counted once" \
  'o[0] == o[1] and o[0]["requests"] == 3 and o[0]["first_requests"]["requests"] == 1 and {k: v["misses"] for k, v in o[0]["causes"].items()} == {"idle past the 5-minute cache": 1}'

exit $(( fails > 0 ))
