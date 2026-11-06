#!/usr/bin/env bash
# Tests for hooks/trim-tool-output.py (PostToolUse on Bash, Grep, Glob, WebFetch).
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

# hook.run's emission boundary (hooks/lib/native_text.py clean_reply / source_output)
# keeps, in updatedToolOutput, only lines the tool itself returned: the trimmer's
# markers and notes ("… (repeated N×)", "… [+N chars]", "… [Ork Meter trimmed …",
# "Full output: …", "N passing-test lines omitted …") never reach the model, and
# which lever acted shows only in the ledger (reconcile_records recharges the last
# row from the emitted text). `last_kind` reads the newest savings row.
last_kind() { # <name> <kind>
  assert "$1" '"additionalContext" not in o["hookSpecificOutput"] and __import__("json").loads([l for l in open("'"$SCRATCH"'/ork-meter/savings.jsonl") if l.strip()][-1])["kind"] == "'"$2"'"'
}
# A clean row: bytes_after is the emitted stdout+stderr (reconcile_records), and the
# row says so (native_text_boundary_reconciled).
reconciled_bash() { # <name>
  assert "$1" '(lambda r, u: r["native_text_boundary_reconciled"] is True and r["bytes_after"] == len((u["stdout"] + ("" if r["kind"] == "test_filter" else u["stderr"])).encode()) and r["est_tokens_avoided"] == r["est_tokens_before"] - r["est_tokens_after"])(__import__("json").loads([l for l in open("'"$SCRATCH"'/ork-meter/savings.jsonl") if l.strip()][-1]), o["hookSpecificOutput"]["updatedToolOutput"])'
}

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

# Over budget: shape kept, ANSI stripped, repeats collapsed to their first line, a long
# line cut to its 2,000-char prefix, error lines from the elided middle kept. No marker,
# count or full-output note is emitted (source_output); the full original is still saved.
run_hook trim-tool-output.py posttooluse.bash.large.json
assert "Bash: hookSpecificOutput for PostToolUse" 'o["hookSpecificOutput"]["hookEventName"] == "PostToolUse"'
assert "Bash: output shape kept (stdout, stderr, interrupted, isImage)" \
  'sorted(o["hookSpecificOutput"]["updatedToolOutput"]) == ["interrupted", "isImage", "stderr", "stdout"]'
assert "Bash: within budget (12 KiB)" 'len(o["hookSpecificOutput"]["updatedToolOutput"]["stdout"].encode()) < 12 * 1024'
assert "Bash: ANSI escapes stripped" '"\x1b" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "Bash: trailing whitespace stripped" '"module_000.go    " not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "module_000.go" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
# source_output drops the "(repeated 6×)" marker; the one received line stays.
assert "Bash: repeated lines collapsed to one, no count marker emitted" '"(repeated" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and o["hookSpecificOutput"]["updatedToolOutput"]["stdout"].count("warning: deprecated API") == 1'
# source_output replaces the "<prefix>… [+N chars]" line by the received prefix alone.
assert "Bash: single long line cut to its 2,000-char prefix, no [+N chars] marker" '"x" * 2000 in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "x" * 2001 not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "chars]" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
# The elision marker is plugin prose: clean_reply / source_output remove it.
assert "Bash: no elision marker or Ork Meter prose reaches the model" '"normalized lines (" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "Ork Meter" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "Bash: every emitted line was received (ANSI stripped, rstripped) or is a capped line's received prefix" '(lambda got, src: all(l in src or any(s.startswith(l) for s in src) for l in got.split("\n")))(o["hookSpecificOutput"]["updatedToolOutput"]["stdout"], {__import__("re").sub("\x1b\\[[0-?]*[ -/]*[@-~]", "", l).rstrip() for l in json.load(open("tests/fixtures/posttooluse.bash.large.json"))["tool_response"]["stdout"].split("\n")})'
assert "Bash: error lines from the middle kept" '"ERROR: module_299 failed" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "panic: runtime error" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "Bash: head and tail kept" '"module_000.go" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "exit code 2" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
# The "Full output: <path>" note is not a received line, so it is dropped at emission;
# the trim row records the saving, charged for the emitted text.
assert "Bash: no full-output note reaches the model" '"Full output: " not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
last_kind "Bash: trim row recorded" trim
reconciled_bash "Bash: trim row charged for the emitted stdout+stderr"
full=$(ls "$SCRATCH"/ork-meter/tool-output/*.txt 2>/dev/null | head -1)
if [ -f "$full" ] && [ "$(file_mode "$full")" = "600" ] && grep -q $'\x1b\[32m' "$full"; then
  echo "  ok   Bash: full original (with ANSI) written, mode 600"
else
  echo "  FAIL Bash: full output file missing, wrong mode or not original"; fails=$((fails + 1))
fi
dirmode=$(file_mode "$SCRATCH/ork-meter/tool-output")
[ "$dirmode" = "700" ] && echo "  ok   tool-output directory mode 700" || { echo "  FAIL tool-output dir mode $dirmode"; fails=$((fails + 1)); }

# Test-runner filter: passing-test lines dropped, failure kept; the count note is plugin
# prose and never emitted (source_output), the test_filter row carries the saving.
run_hook trim-tool-output.py posttooluse.bash.gotest.json
assert "go test: passing lines omitted" '"--- PASS" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "go test: failure block kept" '"--- FAIL: TestBroken" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "expected 4, got 5" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "go test: active failed test kept" '"=== RUN   TestBroken" in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]'
assert "go test: no count note or recovery wording reaches the model" '"passing-test lines omitted" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"] and "recover" not in o["hookSpecificOutput"]["updatedToolOutput"]["stdout"].lower()'
assert "go test: only received lines, failure first and summary last" 'o["hookSpecificOutput"]["updatedToolOutput"]["stdout"].startswith("=== RUN   TestBroken") and o["hookSpecificOutput"]["updatedToolOutput"]["stdout"].endswith("example.com/pkg\t0.412s")'
last_kind "go test: test_filter row recorded" test_filter
reconciled_bash "go test: test_filter row charged for the emitted stdout"
run_hook trim-tool-output.py posttooluse.bash.gotest-notest.json
silent "same output, non-test command, under budget: untouched"
run_hook trim-tool-output.py posttooluse.bash.gotest.json ORK_METER_TEST_FILTER=off
silent "ORK_METER_TEST_FILTER=off: untouched"

# Grep content, Glob list, WebFetch result.
run_hook trim-tool-output.py posttooluse.grep.large.json
assert "Grep: content trimmed under 8 KiB, other fields kept" 'len(o["hookSpecificOutput"]["updatedToolOutput"]["content"].encode()) < 8 * 1024 and o["hookSpecificOutput"]["updatedToolOutput"]["numFiles"] == 50 and o["hookSpecificOutput"]["updatedToolOutput"]["mode"] == "content"'
# The "… [Ork Meter trimmed …" marker is dropped by source_output; only received lines remain.
assert "Grep: no marker; every emitted line was received" '"Ork Meter" not in o["hookSpecificOutput"]["updatedToolOutput"]["content"] and set(o["hookSpecificOutput"]["updatedToolOutput"]["content"].split("\n")) <= set(json.load(open("tests/fixtures/posttooluse.grep.large.json"))["tool_response"]["content"].split("\n"))'
last_kind "Grep: trim row recorded" trim
run_hook trim-tool-output.py posttooluse.glob.large.json
# source_output keeps only received "filenames" entries: no marker entry, no note entry.
assert "Glob: filenames list trimmed to received entries only" '(lambda got, src: len(got) < len(src) and all(f in src for f in got) and not any("Ork Meter" in f or f.startswith("Full output: ") for f in got))(o["hookSpecificOutput"]["updatedToolOutput"]["filenames"], json.load(open("tests/fixtures/posttooluse.glob.large.json"))["tool_response"]["filenames"])'
assert "Glob: head and tail entries kept" 'o["hookSpecificOutput"]["updatedToolOutput"]["filenames"][0].endswith("handler_000.go") and o["hookSpecificOutput"]["updatedToolOutput"]["filenames"][-1].endswith("handler_119.go")'
last_kind "Glob: trim row recorded" trim
run_hook trim-tool-output.py posttooluse.webfetch.large.json
assert "WebFetch: result trimmed, other fields kept" 'len(o["hookSpecificOutput"]["updatedToolOutput"]["result"].encode()) < 16 * 1024 and o["hookSpecificOutput"]["updatedToolOutput"]["code"] == 200 and "Ork Meter" not in o["hookSpecificOutput"]["updatedToolOutput"]["result"]'

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
