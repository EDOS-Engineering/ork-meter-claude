#!/usr/bin/env bash
# Shared helpers for the hook tests. Source after `cd` to the repo root.
#
#   run_hook <script> <fixture> [VAR=value ...]   runs hooks/<script> through py.sh with the
#                                                  fixture on stdin; placeholders replaced from
#                                                  $SCRATCH, $FILE, $TRANSCRIPT. Sets $out, $status.
#   pyexpr <name> <python code> [lines]           runs the code with hooks/lib on sys.path.
#   pyexprs <name> <code> [<name> <code> ...]     the same for several, in one Python process.
#   check <name> <python expression over `o` (parsed stdout JSON) or `out` (raw)>
#   ok/fail bookkeeping in $fails.

# Never inherit the developer's Ork Meter settings (#133); tests set what they need.
# Nor the billing signals hooks/lib/billing.py reads by name (#110): an exported
# API key would turn the context nudge on by default. Nor how Claude Code was
# started (CLAUDE_CODE_ENTRYPOINT): run from an agent's -p session it would make
# every prompt unattended (features.unattended).
for name in $(compgen -e); do
  case "$name" in
    ORK_METER_*|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|CLAUDE_CODE_USE_*|CLAUDE_CODE_PROMPT_CACHE_TTL|ENABLE_PROMPT_CACHING_1H|FORCE_PROMPT_CACHING_5M|CLAUDE_PROJECT_DIR|CLAUDE_CODE_ENTRYPOINT) unset "$name" ;;
  esac
done

fails=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
export SCRATCH="$tmp/scratchpad"
# The tempdir-fallback store (hooks/lib/session.py) and anything else that
# asks tempfile for a directory stay under this run's root too, so a hook
# under test (retention, #143) never reaches the developer's real temp stores.
ORK_TEST_REAL_TMPDIR=${TMPDIR:-}
export TMPDIR="$tmp/tmpdir" TEMP="$tmp/tmpdir" TMP="$tmp/tmpdir"
mkdir -p "$TMPDIR"
export XDG_STATE_HOME="$tmp/state"
export HOME="$tmp/home"
mkdir -p "$SCRATCH" "$HOME"
# Savings levers need Ork Meter Plus (hooks/lib/entitlement.py). Hook tests run
# entitled by default, from a policy envelope signed with the contract's test
# key (tests/plus_policy_sign.py) beside its connection.json and the legacy
# entitlement.json; tests/entitlement.test.sh and tests/plus-policy.test.sh
# cover the gate itself.
export CLAUDE_PLUGIN_DATA="$HOME/.plugin-data"
write_entitlement() { # <optimize true|false> <state> [until RFC3339 JSON string|null] [policy JSON]
  python3 -I tests/plus_policy_sign.py "$CLAUDE_PLUGIN_DATA" "$1" "$2" "${3:-}" "${4:-}"
}
remove_entitlement() { # the runtime's disconnect: no envelope, connection or legacy answer
  rm -f "$CLAUDE_PLUGIN_DATA/plus-policy.json" "$CLAUDE_PLUGIN_DATA/connection.json" "$CLAUDE_PLUGIN_DATA/entitlement.json"
}
write_entitlement true active
FILE=${FILE:-}
TRANSCRIPT="$PWD/tests/fixtures/transcript.sample.jsonl"

# Avoid mixing GNU stat's filesystem report into the BSD-stat fallback.
file_mode() {
  python3 -c 'import os, stat, sys; print(format(stat.S_IMODE(os.stat(sys.argv[1]).st_mode), "o"))' "$1"
}

run_hook() {
  local script=$1 fixture=$2
  shift 2
  out=$(sed -e "s#__SCRATCH__#$SCRATCH#g" -e "s#__FILE__#${FILE}#g" -e "s#__TRANSCRIPT__#$TRANSCRIPT#g" \
          "tests/fixtures/$fixture" | env "$@" bash hooks/py.sh "$script")
  status=$?
}

run_raw() { # <script> <stdin-text> [VAR=value ...]
  local script=$1 text=$2
  shift 2
  out=$(printf '%s' "$text" | env "$@" bash hooks/py.sh "$script")
  status=$?
}

# assert <name> <python expression>; the expression sees `out` (str) and `o` (parsed JSON or None).
assert() {
  local name=$1 expr=$2
  if [ "$status" -ne 0 ]; then
    echo "  FAIL $name: exit $status"; fails=$((fails + 1)); return 1
  fi
  if OUT="$out" python3 -c '
import json, os, sys
out = os.environ["OUT"]
try:
    o = json.loads(out) if out.strip() else None
except ValueError:
    o = None
sys.exit(0 if eval(sys.argv[1]) else 1)' "$expr"; then
    echo "  ok   $name"
  else
    echo "  FAIL $name: $expr"; echo "       output: ${out:0:400}"; fails=$((fails + 1)); return 1
  fi
}

# pyexpr <name> <python code> [lines]: runs the code with hooks/lib on sys.path
# and prints ok/FAIL; a failure shows the last [lines] (default 5) of its stderr.
pyexpr() {
  local name=$1 code=$2 lines=${3:-5}
  if python3 -I -c "import sys; sys.path.insert(0, 'hooks/lib')
$code" 2>"$tmp/err"; then echo "  ok   $name"; else echo "  FAIL $name"; sed 's/^/       /' "$tmp/err" | tail -"$lines"; fails=$((fails + 1)); fi
}

# pyexprs <name> <code> [<name> <code> ...]: several pyexpr checks in one
# Python process, in order, each reporting its own ok/FAIL (a failure shows
# the last $PYEXPR_LINES lines, default 5, of its traceback). Each block starts
# with a fresh namespace, the hooks/lib modules imported anew, and the
# os.environ, working directory and tempfile.tempdir the batch began with.
# Anything else a block changes in the process, such as an attribute of a
# standard-library module, stays for the blocks after it.
pyexprs() {
  local count
  PYEXPR_LINES=${PYEXPR_LINES:-5} python3 -I - "$@" >"$tmp/pyexprs" <<'PY'
import os, sys, tempfile, traceback
lib = os.path.abspath("hooks/lib")
sys.path.insert(0, "hooks/lib")
lines = int(os.environ.pop("PYEXPR_LINES"))
environ, cwd, tempdir = dict(os.environ), os.getcwd(), tempfile.tempdir
failed = 0
for name, code in zip(sys.argv[1::2], sys.argv[2::2]):
    for module in [key for key, value in sys.modules.items()
                   if os.path.dirname(os.path.abspath(getattr(value, "__file__", None) or "/")) == lib]:
        del sys.modules[module]
    os.environ.clear()
    os.environ.update(environ)
    os.chdir(cwd)
    tempfile.tempdir = tempdir
    try:
        exec(compile(code, "<%s>" % name, "exec"), {"__name__": "__main__"})
        error = None
    except SystemExit as stop:
        error = None if stop.code in (None, 0) else traceback.format_exc()
    except BaseException:
        error = traceback.format_exc()
    if error is None:
        print("  ok   " + name)
    else:
        failed += 1
        print("  FAIL " + name)
        print("\n".join("       " + line for line in error.splitlines()[-lines:]))
print(failed)
PY
  count=$(tail -1 "$tmp/pyexprs")
  sed '$d' "$tmp/pyexprs"
  case "$count" in
    ''|*[!0-9]*) echo "  FAIL pyexprs: the batch did not finish"; fails=$((fails + 1)) ;;
    *) fails=$((fails + count)) ;;
  esac
}

silent() { # <name>: no output and exit 0; the same check as assert 'out == ""', without a Python start
  if [ "$status" -ne 0 ]; then
    echo "  FAIL $1: exit $status"; fails=$((fails + 1)); return 1
  fi
  if [ -z "$out" ]; then
    echo "  ok   $1"
  else
    echo "  FAIL $1: out == \"\""; echo "       output: ${out:0:400}"; fails=$((fails + 1)); return 1
  fi
}

# Configuration fixtures reuse one synthetic ID/store. Independent settings
# cases deliberately start a fresh pin; production reset never deletes it.
reset_fixture_policy() {
  python3 -I -c 'import os, sys
sys.path.insert(0, "hooks/lib")
import session
path = session.Store.for_scratchpad(os.environ["SCRATCH"]).policy_path
try: os.unlink(path)
except FileNotFoundError: pass'
}
