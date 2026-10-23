#!/usr/bin/env bash
# A regressions file run on its own (python3 -I -B tests/x_regressions.py)
# must not inherit the developer's Ork Meter settings (#133, #193): importing
# tests/support.py removes ORK_METER_* from os.environ. run.sh scrubs its own
# shell, so this sets them again for the child processes. An installed-host
# suite gets its whole environment from tests/native_host.py instead.
set -uo pipefail
cd "$(dirname "$0")/.."
fails=0

for f in tests/*regressions.py; do
  if ! grep -qE "^(import support|from support import)" "$f"; then
    echo "  FAIL $f does not import tests/support.py"; fails=$((fails + 1))
  fi
done
[ "$fails" -eq 0 ] && echo "  ok   every regressions file imports tests/support.py"

# No installed-host suite builds the CLI's environment, server or plugin copy
# itself: Host (tests/native_host.py) has the one strict filter, the refusing
# proxy and the private HOME.
before=$fails
for f in tests/native_*qualification.py; do
  if grep -nE "os\.environ\.items\(\)|dict\(os\.environ|ThreadingHTTPServer\(|copytree\(" "$f"; then
    echo "  FAIL $f has its own harness; use tests/native_host.py"; fails=$((fails + 1))
  fi
done
[ "$fails" -eq "$before" ] && echo "  ok   every installed-host suite uses tests/native_host.py"

# The files that went red with a developer's settings exported.
for name in filtered_over_budget policy shared_contract tokenizer_check; do
  if ORK_METER_MODE=off ORK_METER_TRIM=off ORK_METER_CONTEXT_HYGIENE=on ORK_METER_DELTA=on \
      python3 -I -B "tests/${name}_regressions.py" >"${TMPDIR:-/tmp}/ork-scrub-$$.log" 2>&1; then
    echo "  ok   tests/${name}_regressions.py run on its own with ORK_METER_* set"
  else
    echo "  FAIL tests/${name}_regressions.py run on its own with ORK_METER_* set"
    tail -20 "${TMPDIR:-/tmp}/ork-scrub-$$.log" | sed 's/^/       /'; fails=$((fails + 1))
  fi
  rm -f "${TMPDIR:-/tmp}/ork-scrub-$$.log"
done
[ "$fails" -eq 0 ]
