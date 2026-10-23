#!/usr/bin/env bash
# Tests for hooks/read-guard.py (PreToolUse on Read): page whole-file reads of large text files.
set -uo pipefail
cd "$(dirname "$0")/.."
. tests/lib.sh

big="$tmp/big.txt"; small="$tmp/small.txt"; binary="$tmp/blob.dat"; nul="$tmp/looks-text.txt"; fewlines="$tmp/few.txt"
python3 - "$big" "$small" "$binary" "$nul" "$fewlines" <<'EOF'
import sys
big, small, binary, nul, fewlines = sys.argv[1:]
with open(big, "w") as f:
    for i in range(3000):
        f.write("line %d of a big text file with padding to make it large enough\n" % i)
with open(small, "w") as f:
    for i in range(500):
        f.write("line %d\n" % i)
with open(binary, "wb") as f:
    f.write(b"\x00\x01\x02" * 40000)
with open(nul, "wb") as f:
    f.write(b"text\n" * 100 + b"\x00" + b"more\n" * 30000)
with open(fewlines, "w") as f:
    f.write(("x" * 1000 + "\n") * 120)   # 120 KiB, 120 lines
EOF

export FILE="$big"; run_hook read-guard.py pretooluse.read.plain.json
assert "large text file: updatedInput adds limit 300 and keeps the path" 'o["hookSpecificOutput"]["hookEventName"] == "PreToolUse" and o["hookSpecificOutput"]["updatedInput"] == {"file_path": os.environ["FILE"], "limit": 300}'
assert "large text file: context gives size, lines, limit and offset hint" '"193 KiB and 3000 lines" in o["hookSpecificOutput"]["additionalContext"] and "first 300 lines" in o["hookSpecificOutput"]["additionalContext"] and "offset=301" in o["hookSpecificOutput"]["additionalContext"]'
assert "large text file: no permissionDecision (normal permission flow)" '"permissionDecision" not in o["hookSpecificOutput"]'

# Claude Code's own saved output of a too-large tool result (<session_id>/tool-results/), Read because the
# model needs that output: never paged (#329). The same name under another session's directory still is.
saved="$tmp/projects/p/00000000-0000-0000-0000-000000000000/tool-results/b1.txt"
other="$tmp/projects/p/11111111-1111-1111-1111-111111111111/tool-results/b1.txt"
mkdir -p "$(dirname "$saved")" "$(dirname "$other")"; cp "$big" "$saved"; cp "$big" "$other"
export FILE="$saved"; run_hook read-guard.py pretooluse.read.plain.json
silent "Claude Code's saved tool result for this session: untouched"
export FILE="$other"; run_hook read-guard.py pretooluse.read.plain.json
assert "a tool-results file of another session: paged as before" 'o["hookSpecificOutput"]["updatedInput"]["limit"] == 300'

export FILE="$big"; run_hook read-guard.py pretooluse.read.offset.json
silent "offset/limit already set: untouched"
export FILE="$big"; run_hook read-guard.py pretooluse.read.allow-large.json
silent "allow_large: untouched"
export FILE="$small"; run_hook read-guard.py pretooluse.read.plain.json
silent "file under 96 KiB: untouched"
export FILE="$binary"; run_hook read-guard.py pretooluse.read.plain.json
silent "binary extension: untouched"
export FILE="$nul"; run_hook read-guard.py pretooluse.read.plain.json
silent "NUL byte in first 8 KiB: untouched"
export FILE="$fewlines"; run_hook read-guard.py pretooluse.read.plain.json
silent "large file with fewer than 300 lines: untouched"
export FILE="$tmp/missing.txt"; run_hook read-guard.py pretooluse.read.plain.json
silent "missing file: untouched"
export FILE="$big"; run_hook read-guard.py pretooluse.read.plain.json ORK_METER_READ_GUARD=off
silent "ORK_METER_READ_GUARD=off: untouched"
export FILE="$big"; run_hook read-guard.py pretooluse.read.plain.json ORK_METER_TRIM=off
silent "ORK_METER_TRIM=off: untouched"
reset_fixture_policy
export FILE="$small"; run_hook read-guard.py pretooluse.read.plain.json ORK_METER_READ_GUARD_BYTES=4096
assert "threshold configurable: 4.4 KiB file guarded at a 4 KiB threshold" 'o["hookSpecificOutput"]["updatedInput"]["limit"] == 300 and "500 lines" in o["hookSpecificOutput"]["additionalContext"]'
reset_fixture_policy
run_raw read-guard.py '{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":"nope"}'
silent "odd tool_input: no output"
run_raw read-guard.py ''
silent "empty stdin: no output, exit 0"

# The automatic Python outline exposes a distant symbol without replacing
# exact source lines or changing an explicit range request.
python3 - "$tmp/large.py" <<'EOF'
import sys
with open(sys.argv[1], "w") as f:
    f.write("def first():\n    return 1\n")
    for i in range(700):
        f.write("# filler %d %s\n" % (i, "x" * 150))
        if i == 500:
            f.write("def distant():\n    return 2\n")
EOF
export FILE="$tmp/large.py"; run_hook read-guard.py pretooluse.read.plain.json
assert "large Python file (119 KB, over the 80,000-byte deferral ceiling): shorter exact page and distant AST symbol" 'o["hookSpecificOutput"]["updatedInput"]["limit"] == 48 and "distant" in o["hookSpecificOutput"]["additionalContext"]'
export FILE="$tmp/large.py"; run_hook read-guard.py pretooluse.read.offset.json
silent "Python explicit range remains untouched"
python3 - "$tmp/medium.py" <<'EOF'
import sys
with open(sys.argv[1], "w") as f:
    f.write("def first():\n    return 1\n")
    for i in range(350):
        f.write("# line %d %s\n" % (i, "x" * 100))
        if i == 340:
            f.write("def later():\n    return 3\n")
EOF
export FILE="$tmp/medium.py"; run_hook read-guard.py pretooluse.read.plain.json
assert "medium Python read, compaction off by default (#316): AST map activates below ordinary guard threshold" 'o["hookSpecificOutput"]["updatedInput"]["limit"] == 48 and "later" in o["hookSpecificOutput"]["additionalContext"]'
reset_fixture_policy   # a switch turned on takes effect from the next session
export FILE="$tmp/medium.py"; run_hook read-guard.py pretooluse.read.plain.json ORK_METER_READ_COMPACT=on
silent "medium Python read, compaction opted in: let through whole for Read compaction on its result"
python3 - "$tmp/compact.py" <<'EOF'
import sys
with open(sys.argv[1], "w") as f:
    f.write("def first():\n    return 1\n")
    for i in range(350):
        f.write("# line %d %s\n" % (i, "x" * 32))
    f.write("def last():\n    return 2\n")
EOF
export FILE="$tmp/compact.py"; run_hook read-guard.py pretooluse.read.plain.json
silent "compact Python read: no guard below the qualified 24 KiB cutoff"
python3 - "$tmp/invalid.py" <<'EOF'
import sys
with open(sys.argv[1], "w") as f:
    f.write("def broken(:\n")
    for i in range(350):
        f.write("# line %d %s\n" % (i, "x" * 100))
EOF
export FILE="$tmp/invalid.py"; run_hook read-guard.py pretooluse.read.plain.json
silent "invalid medium Python source remains an exact native Read"

# Ledger includes the model-visible context note, including an AST outline when present.
if python3 - "$SCRATCH/ork-meter/savings.jsonl" "$big" <<'EOF'
import json, sys, hashlib
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
big = sys.argv[2]
rows = [r for r in rows if r["path_sha256"] == hashlib.sha256(big.encode()).hexdigest()]
assert len(rows) == 1, rows
r = rows[0]
data = open(big, "rb").read()
first = data.split(b"\n")[:300]
first_bytes = sum(len(l) + 1 for l in first)
assert r["kind"] == "read_guard" and r["bytes_before"] == len(data) and first_bytes < r["bytes_after"] < first_bytes + 500, r
assert r["est_tokens_avoided"] == (len(data) + 3) // 4 - (r["bytes_after"] + 3) // 4, r
assert big not in open(sys.argv[1]).read()
EOF
then echo "  ok   ledger: read_guard row with the expected estimate"; else echo "  FAIL ledger"; fails=$((fails + 1)); fi


# #128: missing, unreadable and non-UTF-8 files are the environment's problem,
# not a reducer fault. Two of each, as separate tool calls, must not trip the
# circuit breaker: a large Read afterwards is still paged.
read_call() { # <file> <tool_use_id>
  run_raw read-guard.py "$(sed -e "s#__SCRATCH__#$SCRATCH#g" -e "s#__FILE__#$1#g" -e "s#__TRANSCRIPT__#$TRANSCRIPT#g" \
    -e "s#toolu_fixture_pre_read#$2#" \
    tests/fixtures/pretooluse.read.plain.json)"
}
python3 - "$tmp/latin1.log" "$tmp/unreadable.txt" <<'EOF2'
import sys
with open(sys.argv[1], "wb") as f:
    for i in range(3000):
        f.write(b"caf\xe9 line %d with padding to make the log large enough\n" % i)
with open(sys.argv[2], "w") as f:
    for i in range(3000):
        f.write("line %d of a file nobody may read, padded to be large enough\n" % i)
EOF2
chmod 000 "$tmp/unreadable.txt"
read_call "$tmp/missing-one.txt" toolu_missing_1; silent "missing file 1: untouched"
read_call "$tmp/missing-two.txt" toolu_missing_2; silent "missing file 2: untouched"
read_call "$tmp/latin1.log" toolu_latin1_1; silent "non-UTF-8 file 1: untouched"
read_call "$tmp/latin1.log" toolu_latin1_2; silent "non-UTF-8 file 2: untouched"
if [ "$(id -u)" != 0 ]; then
  read_call "$tmp/unreadable.txt" toolu_unreadable_1; silent "unreadable file 1: untouched"
  read_call "$tmp/unreadable.txt" toolu_unreadable_2; silent "unreadable file 2: untouched"
fi
chmod 600 "$tmp/unreadable.txt"
# #145: Python too deeply nested for ast.parse (RecursionError on a long
# expression chain) gets the normal page, twice, without a reducer fault.
python3 - "$tmp/nested.py" <<'EOF2'
import sys
with open(sys.argv[1], "w") as f:
    for i in range(400):
        f.write("def f%d():\n    return %d\n\n" % (i, i))
    f.write("x = " + "1 + " * 100000 + "1\n")
EOF2
read_call "$tmp/nested.py" toolu_nested_1
assert "deeply nested Python 1: normal 300-line page" 'o["hookSpecificOutput"]["updatedInput"]["limit"] == 300'
read_call "$tmp/nested.py" toolu_nested_2
assert "deeply nested Python 2: normal 300-line page" 'o["hookSpecificOutput"]["updatedInput"]["limit"] == 300'
read_call "$big" toolu_after_misses
assert "after missing, non-UTF-8 and unreadable files the guard still pages a large Read" \
  'o["hookSpecificOutput"]["updatedInput"]["limit"] == 300'
if find "$SCRATCH" "$XDG_STATE_HOME" "$CLAUDE_PLUGIN_DATA" -path '*reducer-health*' -name '*.json' 2>/dev/null | grep -q .; then
  echo "  FAIL no reducer faults recorded for file errors or unparseable Python"; fails=$((fails + 1))
else
  echo "  ok   no reducer faults recorded for file errors or unparseable Python"
fi

# Demand (#329): after a page, a ranged Read past line 1 of the same file (seen by dedupe-reads.py) means the
# model needed the rest; the guard pages that file no more this session. Another file is still paged.
demand="$tmp/demand.txt"; cp "$big" "$demand"
export FILE="$demand"; run_hook read-guard.py pretooluse.read.plain.json
assert "demand: first whole Read paged" 'o["hookSpecificOutput"]["updatedInput"]["limit"] == 300'
run_raw dedupe-reads.py "$(TRANSCRIPT="$TRANSCRIPT" python3 -c '
import json, os, sys
path = sys.argv[1]
lines = open(path).read().split("\n")[300:400]
print(json.dumps({"session_id": "00000000-0000-0000-0000-000000000000", "scratchpad_dir": os.environ["SCRATCH"],
                  "transcript_path": os.environ["TRANSCRIPT"], "cwd": "/tmp", "hook_event_name": "PostToolUse",
                  "prompt_id": "11111111-1111-1111-1111-111111111111", "tool_name": "Read",
                  "tool_input": {"file_path": path, "offset": 301, "limit": 100},
                  "tool_response": {"type": "text", "file": {"filePath": path, "content": "\n".join(lines),
                                    "numLines": len(lines), "startLine": 301, "totalLines": 3001}}}))' "$demand")"
silent "demand: the ranged Read past the page passes through"
export FILE="$demand"; run_hook read-guard.py pretooluse.read.plain.json
silent "demand: a later whole Read of that file is not paged"
export FILE="$big"; run_hook read-guard.py pretooluse.read.plain.json
assert "demand: another file is still paged" 'o["hookSpecificOutput"]["updatedInput"]["limit"] == 300'

[ "$fails" -eq 0 ]
