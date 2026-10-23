#!/usr/bin/env bash
# Says at session start when the local token-saving hooks can't do their job:
#
# - python3 is missing or older than 3.10, the plugin's one floor (hooks/py.sh).
#   Those hooks fail open silently by design, so without this line a person on
#   an old Python sees nothing at all. This part doesn't
#   consult the mode: only hooks/lib/features.py reads ORK_METER_*, and the
#   floor applies whatever the mode is.
# - the hooks' Python can run them but neither the pinned tokenizer
#   (requirements-hooks.txt) nor the built-in pure-Python encoder loads, so
#   the offline token gate falls back to a conservative byte-count gate and
#   admits only very large trims.
#   hooks/lib/tokenizer_check.py decides that, and
#   says nothing without Ork Meter Plus, when every lever is off anyway.
#
# Both use the interpreter the hooks use (hooks/hook-python.sh), started once:
# tokenizer_check.py exits 3 with the version below the floor. Only when it
# fails otherwise is the interpreter probed again, as before. Silent when
# all is well. Prints at most one JSON systemMessage, installs nothing and
# always exits 0.
cat >/dev/null 2>&1 || true
# As hooks/py.sh: no external dirname, and the directory resolved physically.
case $0 in */* | *\\*) here=${0%[/\\]*} ;; *) here=. ;; esac
here=$(cd -P "$here" && pwd -P) || exit 0
hook_python=python3
. "$here/hook-python.sh" 2>/dev/null || hook_python=python3
if [ "$hook_python" = python3 ]; then which="python3 on PATH"; else which="the plugin's private hook Python"; fi
if ! command -v "$hook_python" >/dev/null 2>&1; then
  reason="python3 is not on PATH"
else
  out=$("$hook_python" -I -B "$here/lib/tokenizer_check.py" 2>/dev/null)
  status=$?
  if [ "$status" -eq 0 ]; then
    [[ "$out" == '{"systemMessage": '* ]] && printf '%s\n' "$out"
    exit 0
  fi
  if [ "$status" -eq 3 ]; then
    version=$out
  elif "$hook_python" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    exit 0   # the check itself failed (killed, out of memory) on a Python that can run the hooks
  else
    version=$("$hook_python" -I -c 'import sys; print("%d.%d.%d" % tuple(sys.version_info[:3]))' 2>/dev/null)
  fi
  if [[ "$version" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}$ ]]; then
    reason="$which is $version"
  else
    reason="$which could not be run"
  fi
fi
printf '{"systemMessage":"Ork Meter: the local token-saving hooks cannot run because %s; they need Python 3.10 or later. Install a newer Python 3, then restart Claude Code."}\n' "$reason"
exit 0
