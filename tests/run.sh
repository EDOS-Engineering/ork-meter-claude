#!/usr/bin/env bash
# Runs every tests/*.test.sh and every tests/*regressions.py; each exits
# non-zero on failure. Hook tests feed fixture payloads from tests/fixtures/
# to the hook on stdin.
#
# Each regressions file runs in its own Python process: they set sys.path and
# the environment at import. A regressions file that a *.test.sh runs itself
# (a non-comment `python3 ... /<file>` line, with setup of its own) is left to
# that script. Every regressions file runs exactly once: a file two such lines
# run fails here.
# CI runs this once per OS in ci.yml's `unit` job; on Windows that job runs
# the same files with .github/scripts/check-native-python.py instead.
# ORK_SLOW_TESTS=1 adds the full pure_bpe code-point sweep; in ci.yml only the
# unit (ubuntu-latest) job sets it, and the release workflows' verify jobs do.
set -uo pipefail
cd "$(dirname "$0")/.."
# Tests must not inherit the developer's Ork Meter settings (#133): an
# ORK_METER_CONTEXT_HYGIENE=on in the shell turned "off by default" tests red.
for name in $(compgen -e); do
  case "$name" in ORK_METER_*) unset "$name" ;; esac
done

pass=0
fail=0
result() { # <exit status> <file>
  if [ "$1" -eq 0 ]; then
    echo "pass  $2"; pass=$((pass + 1))
  else
    echo "FAIL  $2"; fail=$((fail + 1))
  fi
}

for t in tests/*.test.sh; do
  [ -e "$t" ] || continue
  bash "$t"
  result $? "$t"
done

for f in tests/*regressions.py; do
  [ -e "$f" ] || continue
  name=$(basename "$f")
  if ! grep -q "unittest.main(" "$f"; then   # run as a script, it would run no test
    echo "  FAIL $f has no unittest.main()"; result 1 "$f"; continue
  fi
  runners=$(grep -hE "^[^#]*python3 [^#]*/${name//./\\.}([\"' ]|\$)" tests/*.test.sh | wc -l | tr -d ' ')
  case "$runners" in
    0) python3 -I -B "$f"; result $? "$f" ;;
    1) ;;   # its *.test.sh ran it above
    *) echo "  FAIL $f is run by $runners lines in tests/*.test.sh; run it once"; result 1 "$f" ;;
  esac
done

echo "$pass passed, $fail failed."
[ "$fail" -eq 0 ]
