#!/usr/bin/env bash
# Tests for hooks/python-check.sh: the session-start notice when python3 is
# missing or below the 3.10 floor that hooks/py.sh needs (ork-meter #245).
set -euo pipefail
cd "$(dirname "$0")/.."
hook="$PWD/hooks/python-check.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# A PATH with only the tools the hook needs, plus a python3 shim per case.
base="$tmp/base"
mkdir -p "$base"
for tool in bash cat; do ln -s "$(command -v "$tool")" "$base/$tool"; done
shim() { # <name> <version-or-"broken">
  mkdir -p "$tmp/$1"
  if [ "$2" = broken ]; then
    printf '#!/bin/sh\nexit 127\n' > "$tmp/$1/python3"
  else
    # Runs tokenizer_check.py as an interpreter of that version would: a new
    # one passes (no tokenizer message here), an old one prints its version
    # and exits 3 (hooks/lib/tokenizer_check.py FLOOR_STATUS).
    # "crash" passes the floor but its tokenizer_check.py run is killed (137);
    # the floor probe that follows answers by version, as before.
    printf '#!/bin/sh\ncase "$*" in *version_info\\ \\>=*) [ "%s" != old ]; exit ;; *version_info*) echo "%s"; exit 0 ;; esac\n[ "%s" = new ] && exit 0\n[ "%s" = crash ] && exit 137\necho "%s"\nexit 3\n' "$3" "$2" "$3" "$3" "$2" > "$tmp/$1/python3"
  fi
  chmod +x "$tmp/$1/python3"
}
shim old 3.9.6 old
shim new 3.12.4 new
shim broken broken
shim crash 3.12.4 crash
shim odd 'evil"}' old

run() { # <path-dir-or-empty>
  out=$(env -i HOME="$tmp" PATH="${1:+$tmp/$1:}$base" bash "$hook" < tests/fixtures/session-start.startup.json)
}
one_message() {
  if command -v node >/dev/null 2>&1; then
    printf '%s' "$out" | node -e 'const v=JSON.parse(require("fs").readFileSync(0,"utf8"));if(Object.keys(v).length!==1||typeof v.systemMessage!=="string")process.exit(1)'
  fi
}

run old
[[ "$out" == *'python3 on PATH is 3.9.6; they need Python 3.10 or later'* ]]
one_message
echo '  ok   python 3.9: names the version and the floor'

run ""
[[ "$out" == *'python3 is not on PATH; they need Python 3.10 or later'* ]]
one_message
echo '  ok   no python3: says so'

run broken
[[ "$out" == *'python3 on PATH could not be run'* ]]
one_message
echo '  ok   python3 that fails: says so'

run odd
[[ "$out" == *'could not be run'* && "$out" != *evil* ]]
one_message
echo '  ok   an unexpected version string is not echoed into the JSON'

run new
[ -z "$out" ]
echo '  ok   python 3.10+: silent'

run crash
[ -z "$out" ]
echo '  ok   python 3.10+ whose check is killed: silent, as before'

# A real interpreter below the floor still parses tokenizer_check.py far
# enough to say so (macOS ships 3.9 as /usr/bin/python3).
for old in /usr/bin/python3 python3.9 python3.8; do
  old=$(command -v "$old" 2>/dev/null) || continue
  "$old" -c 'import sys; sys.exit(0 if sys.version_info < (3, 10) else 1)' 2>/dev/null || continue
  mkdir -p "$tmp/real-old"; ln -sf "$old" "$tmp/real-old/python3"
  run real-old
  [[ "$out" == *"python3 on PATH is $("$old" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"* ]]
  one_message
  echo "  ok   a real $("$old" -V 2>&1) below the floor: names its version"
  break
done

if command -v python3 >/dev/null 2>&1; then
  out=$(bash "$hook" < /dev/null)
  if python3 -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then [ -z "$out" ]; else [ -n "$out" ]; fi
  echo '  ok   the real python3 on this runner agrees with the hook'
fi

python3 - <<'PY'
import json
groups = json.load(open("hooks/hooks.json"))["hooks"]["SessionStart"]
hits = [(g.get("matcher"), h) for g in groups for h in g["hooks"] if "python-check.sh" in h["command"]]
assert len(hits) == 1 and hits[0][0] == "startup|resume", hits
# The check starts the hooks' Python and loads the whole tokenizer, like a py.sh
# hook, so it gets the same 5 s: Claude Code cancelled it at 2 s on cold Windows
# runners. Every SessionStart hook shares one cap, so none is cut short sooner
# and none makes a session wait longer than trim-session.py already may.
timeouts = {h["timeout"] for g in groups for h in g["hooks"]}
assert timeouts == {5}, timeouts
PY
echo '  ok   wired once, at startup and resume only, with the shared 5 s SessionStart cap'
