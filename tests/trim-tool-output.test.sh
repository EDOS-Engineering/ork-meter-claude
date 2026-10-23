#!/usr/bin/env bash
# Tests for hooks/trim-tool-output.py (PostToolUse on Bash, Grep, Glob, WebFetch).
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

# Under budget: untouched.
run_hook trim-tool-output.py posttooluse.bash.small.json
silent "Bash under budget: no output"

# Output Claude Code already persisted (persistedOutputPath, as 2.1.308 sends
# it): the model gets Claude Code's own preview whatever the hook replies, so
# the hook leaves it alone: no reply, no full-output copy, no ledger row
# (issues #100, #350).
ledger="$SCRATCH/ork-meter/savings.jsonl"
before=$( [ -f "$ledger" ] && wc -l < "$ledger" || echo 0 )
copies_before=$(ls "$SCRATCH/ork-meter/tool-output" 2>/dev/null | wc -l)
run_hook trim-tool-output.py posttooluse.bash.persisted.json
silent "Bash persisted by Claude Code: no reply"
after=$( [ -f "$ledger" ] && wc -l < "$ledger" || echo 0 )
if [ "$before" -eq "$after" ]; then echo "  ok   Bash persisted by Claude Code: no ledger row"; else echo "  FAIL persisted output recorded a saving"; fails=$((fails + 1)); fi
copies_after=$(ls "$SCRATCH/ork-meter/tool-output" 2>/dev/null | wc -l)
if [ "$copies_before" -eq "$copies_after" ]; then echo "  ok   Bash persisted by Claude Code: no full-output copy"; else echo "  FAIL persisted output was copied"; fails=$((fails + 1)); fi

# Over budget: shape kept, ANSI stripped, repeats collapsed, long line capped,
# head/tail marker present, error lines from the elided middle kept, full-output note.
run_hook trim-tool-output.py posttooluse.bash.large.json
assert "Bash: hookSpecificOutput for PostToolUse" 'o["hookSpecificOutput"]["hookEventName"] == "PostToolUse"'
assert "Bash: output shape kept (stdout, stderr, interrupted, isImage)" \
  'sorted(o["hookSpecificOutput"]["updatedToolOutput"]) == ["interrupted", "isImage", "stderr", "stdout"]'
assert "Bash: within budget (12 KiB + note)" 'len(o["hookSpecificOutput"]["updatedToolOutput"]["stdout"].encode()) < 12 * 1024 + 400'
assert "Bash: ANSI escapes stripped" '"\x1b" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "Bash: trailing whitespace stripped" '"module_000.go    " not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "module_000.go" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "Bash: repeated lines collapsed with count" '"… (repeated 6×)" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and o["hookSpecificOutput"]["updatedToolOutput"]["stdout"].count("warning: deprecated API") == 1'
assert "Bash: single long line capped at 2,000 chars" '"x" * 2000 + "… [+1000 chars]" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "x" * 2001 not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "Bash: elision marker counts normalized omissions and retained diagnostics" '"normalized lines (" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "diagnostic/context lines from that range follow]" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "Bash: error lines from the middle kept" '"ERROR: module_299 failed" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "panic: runtime error" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "Bash: head and tail kept" '"module_000.go" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "exit code 2" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "Bash: full-output note points into the scratchpad" '"Full output: " + os.environ["SCRATCH"] + "/ork-meter/tool-output/" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
full=$(OUT="$out" python3 -c 'import json,os; print(json.loads(os.environ["OUT"])["hookSpecificOutput"]["updatedToolOutput"]["stdout"].split("Full output: ")[-1].rsplit(" (",1)[0])')
if [ -f "$full" ] && [ "$(file_mode "$full")" = "600" ] && grep -q $'\x1b\[32m' "$full"; then
  echo "  ok   Bash: full original (with ANSI) written, mode 600"
else
  echo "  FAIL Bash: full output file missing, wrong mode or not original"; fails=$((fails + 1))
fi
dirmode=$(file_mode "$SCRATCH/ork-meter/tool-output")
[ "$dirmode" = "700" ] && echo "  ok   tool-output directory mode 700" || { echo "  FAIL tool-output dir mode $dirmode"; fails=$((fails + 1)); }

# Test-runner filter: passing-test lines dropped, failure kept, note states the count.
run_hook trim-tool-output.py posttooluse.bash.gotest.json
assert "go test: passing lines omitted" '"--- PASS" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "go test: failure block kept" '"--- FAIL: TestBroken" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "expected 4, got 5" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "go test: active failed test kept" '"=== RUN   TestBroken" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "go test: note states count and does not invite recovery" '"401 passing-test lines omitted; every other line is shown" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "recover" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"].lower()'
run_hook trim-tool-output.py posttooluse.bash.gotest-notest.json
silent "same output, non-test command, under budget: untouched"
run_hook trim-tool-output.py posttooluse.bash.gotest.json ORK_METER_TEST_FILTER=off
silent "ORK_METER_TEST_FILTER=off: untouched"

# Grep content, Glob list, WebFetch result.
run_hook trim-tool-output.py posttooluse.grep.large.json
assert "Grep: content trimmed under 8 KiB + note, other fields kept" 'len(o["hookSpecificOutput"]["updatedToolOutput"]["content"].encode()) < 8 * 1024 + 400 and o["hookSpecificOutput"]["updatedToolOutput"]["numFiles"] == 50 and o["hookSpecificOutput"]["updatedToolOutput"]["mode"] == "content"'
assert "Grep: marker present" '"… [Ork Meter trimmed " in o["hookSpecificOutput"]["updatedToolOutput"]["content"]'
run_hook trim-tool-output.py posttooluse.glob.large.json
assert "Glob: filenames list trimmed with marker entry and note" 'any(f.startswith("… [Ork Meter trimmed ") for f in o["hookSpecificOutput"]["updatedToolOutput"]["filenames"]) and o["hookSpecificOutput"]["updatedToolOutput"]["filenames"][-1].startswith("Full output: ")'
assert "Glob: head and tail entries kept" 'o["hookSpecificOutput"]["updatedToolOutput"]["filenames"][0].endswith("handler_000.go") and o["hookSpecificOutput"]["updatedToolOutput"]["filenames"][-2].endswith("handler_119.go")'
run_hook trim-tool-output.py posttooluse.webfetch.large.json
assert "WebFetch: result trimmed, other fields kept" 'len(o["hookSpecificOutput"]["updatedToolOutput"]["result"].encode()) < 16 * 1024 + 400 and o["hookSpecificOutput"]["updatedToolOutput"]["code"] == 200'

# Budget override and global switch.
reset_fixture_policy
run_hook trim-tool-output.py posttooluse.bash.large.json ORK_METER_TRIM_BUDGET_BASH=64K
silent "ORK_METER_TRIM_BUDGET_BASH=64K: 21 KiB result untouched"
run_hook trim-tool-output.py posttooluse.bash.large.json ORK_METER_TRIM=off
silent "ORK_METER_TRIM=off: no output"

# Robustness: malformed input, empty input, wrong event, no python.
run_raw trim-tool-output.py '{not json'
silent "malformed stdin: no output, exit 0"
run_raw trim-tool-output.py ''
silent "empty stdin: no output, exit 0"
run_raw trim-tool-output.py '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_response":{"stdout":"x"}}'
silent "wrong event: no output"
out=$(printf '{}' | env -i PATH="$tmp/nopath" "$(command -v bash)" hooks/py.sh trim-tool-output.py); status=$?
silent "no python on PATH: no output, exit 0"

# Ledger: one line per intervention, well-formed, no content.
ledger="$SCRATCH/ork-meter/savings.jsonl"
if python3 - "$ledger" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
need = {"ts", "session_id", "tool", "kind", "bytes_before", "bytes_after", "est_tokens_before", "est_tokens_after", "est_tokens_avoided"}
assert rows, "no rows"
for r in rows:
    assert need <= set(r), r
    assert r["kind"] in ("trim", "test_filter"), r["kind"]
    assert r["est_tokens_avoided"] == -(-r["bytes_before"] // 4) - (-(-r["bytes_after"] // 4)), r
    assert r["bytes_after"] <= r["bytes_before"]
raw = open(sys.argv[1]).read()
for secret in ("module_000", "TestBroken", "handler_000", "Paragraph", "/tmp/project"):
    assert secret not in raw, secret
kinds = [r["kind"] for r in rows]
assert kinds.count("test_filter") == 1 and kinds.count("trim") == 4, kinds
EOF
then echo "  ok   ledger: 5 well-formed rows, no content, no paths"; else echo "  FAIL ledger"; fails=$((fails + 1)); fi
[ "$(file_mode "$ledger")" = "600" ] && echo "  ok   ledger mode 600" || { echo "  FAIL ledger mode"; fails=$((fails + 1)); }

# Tool call identity (#98): the same body from two calls gives two rows, told apart
# by tool_call_sha256 (the SHA-256 of tool_use_id, never the id itself).
idscratch="$tmp/ids"; mkdir -p "$idscratch"
for id in toolu_call_one toolu_call_two; do
  sed -e "s#__SCRATCH__#$idscratch#g" -e "s#__TRANSCRIPT__#$TRANSCRIPT#g" -e "s#toolu_fixture_bash#$id#" \
    tests/fixtures/posttooluse.bash.large.json | bash hooks/py.sh trim-tool-output.py >/dev/null
done
if python3 - "$idscratch/ork-meter/savings.jsonl" <<'EOF'
import hashlib, json, sys
raw = open(sys.argv[1]).read()
rows = [json.loads(l) for l in raw.splitlines() if l.strip()]
assert [r["kind"] for r in rows] == ["trim", "trim"], rows
assert {k: v for k, v in rows[0].items() if k not in ("ts", "tool_call_sha256")} == \
       {k: v for k, v in rows[1].items() if k not in ("ts", "tool_call_sha256")}, rows
want = [hashlib.sha256(i.encode()).hexdigest() for i in ("toolu_call_one", "toolu_call_two")]
assert [r["tool_call_sha256"] for r in rows] == want, rows
assert "toolu_call" not in raw
EOF
then echo "  ok   ledger: identical bodies from two calls give two rows with distinct tool_call_sha256 (#98)"
else echo "  FAIL ledger tool_call_sha256"; fails=$((fails + 1)); fi

# Time: well inside the 2-second cap.
start=$(python3 -c 'import time; print(int(time.time()*1000))')
for _ in 1 2 3 4 5; do run_hook trim-tool-output.py posttooluse.bash.large.json; done
end=$(python3 -c 'import time; print(int(time.time()*1000))')
per=$(( (end - start) / 5 ))
[ "$per" -lt 800 ] && echo "  ok   ${per} ms per run (cap 2 s)" || { echo "  FAIL ${per} ms per run"; fails=$((fails + 1)); }

[ "$fails" -eq 0 ]
