#!/usr/bin/env bash
# Tests for scripts/savings.py and scripts/audit.py against a ledger built from fixtures.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

run_hook trim-tool-output.py posttooluse.bash.large.json
run_hook trim-tool-output.py posttooluse.glob.large.json
run_hook dedupe-reads.py posttooluse.read.small.json
run_hook dedupe-reads.py posttooluse.read.small.json
run_hook trim-session.py sessionstart.resume-expired.json
run_hook agent-stop.py subagentstop.explore.json

expected=$(python3 - "$SCRATCH/ork-meter/savings.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
byte_rows = [r for r in rows if r["kind"] in ("trim", "dedupe")]
print(len(byte_rows), sum(r["bytes_before"] - r["bytes_after"] for r in byte_rows), sum(r["est_tokens_avoided"] for r in byte_rows))
EOF
)
set -- $expected
n=$1; avoided=$2; tokens=$3

out=$(bash hooks/py.sh --script scripts/savings.py --scratchpad "$SCRATCH"); status=$?
assert "savings via py.sh --script: table total matches the ledger (interventions, bytes avoided, est. tokens)" \
  "'total' in out and '$n' in out.split('total')[1].split()[0] and '{:,}'.format($avoided) in out and '{:,}'.format($tokens) in out"
assert "savings: heuristic sentence present verbatim" '"Estimated with a 4-bytes-per-token heuristic from avoided tool-output bytes. Not measured by the model'"'"'s tokenizer and not a billing figure." in out'
assert "savings: measured column from the transcript" '"Measured from transcript (partial measurement): 2 assistant messages; input 15, cache read 3,000, cache creation 200, output 75 tokens (prompt tokens = 3,215)." in out'
assert "savings: no ratio of the estimate to the measurement (#96)" '"measured prompt tokens:" not in out and "/ measured" not in out'
assert "savings: haiku agent and cache nudge lines" '"Haiku agents: 1 runs (ork-meter:explore)" in out and "Cache nudges: 1 shown, 0 followed" in out'
assert "savings: prints no percentage" '"%" not in out'

out=$(python3 scripts/savings.py --scratchpad "$SCRATCH" --json); status=$?
assert "savings --json: totals and labels" "o['estimated']['totals']['est_tokens_avoided'] == $tokens and o['measured']['status'] == 'partial' and 'heuristic' in o"

# --all aggregates every session under the temp root; here one session.
root="$tmp/claude-root"; mkdir -p "$root/-proj/sess-a" "$root/-proj/sess-b"
cp -R "$SCRATCH" "$root/-proj/sess-a/scratchpad"; cp -R "$SCRATCH" "$root/-proj/sess-b/scratchpad"
# sess-b's calls are its own: tool call ids are unique, so give its rows other ones.
python3 - "$root/-proj/sess-b/scratchpad/ork-meter/savings.jsonl" <<'EOF'
import hashlib, json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
for r in rows:
    if "tool_call_sha256" in r:
        r["tool_call_sha256"] = hashlib.sha256(("b" + r["tool_call_sha256"]).encode()).hexdigest()
open(sys.argv[1], "w").write("".join(json.dumps(r) + "\n" for r in rows))
EOF
out=$(python3 scripts/savings.py --all --scratchpad "$root/-proj/sess-a/scratchpad" --no-measure); status=$?
assert "savings --all: two sessions aggregated" '"all sessions found (2 with local records)" in out and "{:,}".format(2 * '"$tokens"') in out and "Duplicate rows" not in out'

# Rows repeating a tool call and kind (#98) are reported, not summed.
dup="$tmp/dup"; mkdir -p "$dup/ork-meter"
python3 - "$SCRATCH/ork-meter/savings.jsonl" "$dup/ork-meter/savings.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
byte = [r for r in rows if r["kind"] in ("trim", "dedupe") and r.get("tool_call_sha256")]
assert byte, "no byte rows with a tool call id"
open(sys.argv[2], "w").write("".join(json.dumps(r) + "\n" for r in rows + byte))
EOF
out=$(python3 scripts/savings.py --scratchpad "$dup" --no-measure); status=$?
assert "savings: a repeated tool call is reported as a duplicate and left out of the total (#98)" \
  "'Duplicate rows: ' in out and 'left out of the table and totals' in out and '{:,}'.format($avoided) in out and '$n' in out.split('total')[1].split()[0]"
out=$(python3 scripts/savings.py --scratchpad "$dup" --no-measure --json); status=$?
assert "savings --json: duplicate_call_rows reported, totals unchanged (#98)" \
  "o['estimated']['totals']['duplicate_call_rows'] > 0 and o['estimated']['totals']['est_tokens_avoided'] == $tokens"

# Session lookup by id under a temp root (CLAUDE_CODE_TMPDIR), and a missing session.
root2="$tmp/tmproot"; mkdir -p "$root2/claude-$(id -u)/-proj/sess-c"; cp -R "$SCRATCH" "$root2/claude-$(id -u)/-proj/sess-c/scratchpad"
out=$(CLAUDE_CODE_TMPDIR="$root2" python3 scripts/savings.py --session sess-c --no-measure); status=$?
assert "savings --session: found under CLAUDE_CODE_TMPDIR" '"Ork Meter savings, this session" in out'
out=$(CLAUDE_CODE_TMPDIR="$root2" python3 scripts/savings.py --session nope --no-measure); s=$?
[ "$s" -eq 1 ] && [[ "$out" == *"No scratchpad found"* ]] && echo "  ok   savings --session: missing session reported" || { echo "  FAIL missing session"; fails=$((fails + 1)); }

# Tempdir fallback (no scratchpad_dir in the payload): the session is found by id and counted by --all.
fb_payload=$(sed -e 's#"scratchpad_dir": *"[^"]*",##' -e 's#"session_id": *"[^"]*"#"session_id": "sess-fb"#' tests/fixtures/posttooluse.bash.large.json)
grep -q scratchpad_dir <<<"$fb_payload" && { echo "  FAIL fixture still has scratchpad_dir"; fails=$((fails + 1)); }
fbroot="$tmp/fbtmp"; mkdir -p "$fbroot"
printf '%s' "$fb_payload" | TMPDIR="$fbroot" bash hooks/py.sh trim-tool-output.py >/dev/null
[ -f "$fbroot/claude-ork-meter-$(id -u)/sess-fb/ork-meter/savings.jsonl" ] && echo "  ok   fallback: ledger under <tmp>/claude-ork-meter-<uid>/<sid>/ork-meter" || { echo "  FAIL fallback ledger missing"; fails=$((fails + 1)); }
out=$(TMPDIR="$fbroot" CLAUDE_CODE_TMPDIR="$root2" python3 scripts/savings.py --session sess-fb --no-measure); status=$?
assert "savings --session: fallback session found (was invisible before)" '"Ork Meter savings, this session (sess-fb)" in out and "trim " in out'
out=$(TMPDIR="$fbroot" CLAUDE_CODE_TMPDIR="$root2" python3 scripts/savings.py --all --no-measure --json); status=$?
assert "savings --all: fallback session counted with the scratchpad sessions" 'o["sessions"] >= 2 and o["estimated"]["by_kind"]["trim"]["count"] >= 1'

# py.sh --script: errors stay visible and the exit status passes through (a skill wants the output).
err=$(bash hooks/py.sh --script scripts/savings.py 2>&1 >/dev/null); s=$?
[ "$s" -eq 2 ] && [[ "$err" == *"give --session"* ]] && echo "  ok   py.sh --script: argparse error on stderr, exit 2" || { echo "  FAIL py.sh --script error ($s): $err"; fails=$((fails + 1)); }
err=$(PATH="$tmp/nopath" "$(command -v bash)" hooks/py.sh --script scripts/savings.py --all 2>&1 >/dev/null); s=$?
[ "$s" -eq 1 ] && [[ "$err" == *"python3 (3.10 or later) is needed"* ]] && echo "  ok   py.sh --script without python3: told on stderr, exit 1" || { echo "  FAIL py.sh --script no python ($s): $err"; fails=$((fails + 1)); }
err=$(bash hooks/py.sh --script 2>&1 >/dev/null); s=$?
[ "$s" -eq 2 ] && [[ "$err" == usage:* ]] && echo "  ok   py.sh --script without a path: usage, exit 2" || { echo "  FAIL py.sh --script usage ($s)"; fails=$((fails + 1)); }

# Empty ledger: table has a zero total, measurement unavailable with a reason.
mkdir -p "$tmp/empty/ork-meter"
out=$(python3 scripts/savings.py --scratchpad "$tmp/empty"); status=$?
assert "savings on an empty session: zero total, measurement unavailable" '"total" in out and "unavailable (no transcript path recorded" in out'

# Audit: instruction files up the tree, user CLAUDE.md, MCP server names only, ranking.
proj="$tmp/work/sub"; mkdir -p "$proj/.claude/rules" "$HOME/.claude"
python3 -c 'open("'"$tmp/work/CLAUDE.md"'","w").write("# parent\n" + "rule line\n" * 250)'
printf '# project\n- keep it short\n' > "$proj/CLAUDE.md"
printf '# a rule\n' > "$proj/.claude/rules/style.md"
printf '# user\nprefs\n' > "$HOME/.claude/CLAUDE.md"
printf '{"mcpServers":{"github":{"command":"secret-binary","env":{"TOKEN":"hunter2"}}},"projects":{"%s":{"mcpServers":{"local-db":{"command":"x"}}}}}' "$proj" > "$HOME/.claude.json"
printf '{"mcpServers":{"filesystem":{"command":"npx","args":["-y","server"]}}}' > "$proj/.mcp.json"
out=$(bash hooks/py.sh --script scripts/audit.py --cwd "$proj" --home "$HOME"); status=$?
assert "audit: lists project, parent, rules and user files with lines" '"project " in out and "parent " in out and "project rules" in out and "user " in out and "251 lines" in out'
assert "audit: flags the 251-line CLAUDE.md" '"has 251 lines; Anthropic advises keeping CLAUDE.md under 200" in out'
assert "audit: MCP server names only, never commands or env" '"github" in out and "local-db" in out and "filesystem" in out and "secret-binary" not in out and "hunter2" not in out and "npx" not in out'
assert "audit: ranked list and estimate label" '"Largest always-loaded items:" in out and "1. " + os.environ["HOME"].replace(os.environ["HOME"], "") in out and "estimates (bytes/4)" in out'
out=$(python3 scripts/audit.py --cwd "$proj" --home "$HOME" --json); status=$?
assert "audit --json: 3 MCP sources, est tokens per session" 'sum(len(s["servers"]) for s in o["mcp_servers"]) == 3 and o["est_tokens_per_session"] > 0'

# Audit with the project under $HOME (#132): walking up passes ~, but the
# ~/.claude files and settings are counted once, as the user's.
fakehome="$tmp/fakehome"; hproj="$fakehome/code/app"; mkdir -p "$hproj" "$fakehome/.claude/rules"
printf '# user\nprefs\n' > "$fakehome/.claude/CLAUDE.md"
printf '# user rule\n' > "$fakehome/.claude/rules/r.md"
printf '{"mcpServers":{"only-one":{"command":"x"}}}' > "$fakehome/.claude/settings.json"
printf '# project\n' > "$hproj/CLAUDE.md"
out=$(python3 scripts/audit.py --cwd "$hproj" --home "$fakehome" --json); status=$?
assert "audit under HOME: ~/.claude files and settings counted once, as the user's" \
  'sorted(i["scope"] for i in o["instruction_files"]) == ["project", "user", "user rules"] and [s["servers"] for s in o["mcp_servers"]] == [["only-one"]]'

[ "$fails" -eq 0 ]
