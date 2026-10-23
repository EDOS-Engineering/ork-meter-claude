#!/usr/bin/env bash
# #115: when the hooks' Python can run them but has no pinned tokenizer, the
# built-in pure-Python encoder (hooks/lib/pure_bpe.py) counts exactly: the
# session start says nothing (hooks/python-check.sh ->
# hooks/lib/tokenizer_check.py) and trims are delivered and credited. The
# byte-count gate, used only when no exact encoder loads, is covered by
# tests/token_gate_regressions.py and tests/tokenizer_check_regressions.py.
#
# Uses a real interpreter without tiktoken: a venv made from this runner's
# python3, without pip and without system site-packages, installed as the
# plugin's private hook Python.
set -uo pipefail
cd "$(dirname "$0")/.."
python3 -I -B tests/tokenizer_check_regressions.py || exit 1
. tests/lib.sh

json_message() { # prints the one systemMessage in $out, or fails
  OUT="$out" python3 -c 'import json, os; v = json.loads(os.environ["OUT"]); assert list(v) == ["systemMessage"]; print(v["systemMessage"])'
}

if ! python3 -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo '  skip python3 here is older than 3.10'; exit "$fails"
fi
if python3 -I -B -c 'import sys; sys.path.insert(0, "hooks/lib"); import token_gate; token_gate.count("ork")' 2>/dev/null; then
  out=$(bash hooks/python-check.sh </dev/null)
  [ -z "$out" ] && echo '  ok   python3 with the tokenizer, entitled: silent' \
    || { echo "  FAIL python3 with the tokenizer: $out"; fails=$((fails + 1)); }
fi

if ! python3 -m venv --without-pip "$CLAUDE_PLUGIN_DATA/hook-python" >/dev/null 2>&1; then
  echo '  skip python3 -m venv is unavailable here'; exit "$fails"
fi
private="$CLAUDE_PLUGIN_DATA/hook-python/bin/python3"
[ -x "$private" ] || private="$CLAUDE_PLUGIN_DATA/hook-python/Scripts/python.exe"

# The built-in pure-Python encoder counts exactly without tiktoken: no notice.
out=$(bash hooks/python-check.sh </dev/null)
[ -z "$out" ] && echo '  ok   private Python without tiktoken, entitled: silent (pure_bpe counts exactly)' \
  || { echo "  FAIL private Python without tiktoken: $out"; fails=$((fails + 1)); }

# The same interpreter, as the trim hooks run it. 22 KB over a 12 KiB budget
# can't prove 512 tokens by bytes alone; the exact count does: trimmed, credited.
ledger="$SCRATCH/ork-meter/savings.jsonl"
run_hook trim-tool-output.py posttooluse.bash.large.json
assert "private Python without tiktoken: Bash output over budget is trimmed on pure_bpe's exact count" \
  'len(o["hookSpecificOutput"]["updatedToolOutput"]["stdout"]) < 20000'
if python3 -c 'import json, sys; rows = [json.loads(l) for l in open(sys.argv[1])]
assert len(rows) == 1 and rows[0]["kind"] == "trim"' "$ledger" 2>/dev/null
then echo '  ok   private Python without tiktoken: one ledger row'
else echo "  FAIL ledger after the pure_bpe trim: $(head -c 400 "$ledger" 2>/dev/null)"; fails=$((fails + 1)); fi

write_entitlement false free null
out=$(bash hooks/python-check.sh </dev/null)
[ -z "$out" ] && echo '  ok   without Plus: silent (the levers are off anyway)' \
  || { echo "  FAIL without Plus: $out"; fails=$((fails + 1)); }
write_entitlement true active

# The private venv broken by a Python upgrade: the hooks fall back to python3 on
# PATH, and the notice offers to recreate the venv.
rm -f "$private"
out=$(bash hooks/python-check.sh </dev/null)
if [ -z "$out" ] || { text=$(json_message) && [[ "$text" == *"-m venv --clear"* ]]; }; then
  echo '  ok   missing private Python: falls back to python3 on PATH'
else
  echo "  FAIL missing private Python: $out"; fails=$((fails + 1))
fi

exit "$fails"
