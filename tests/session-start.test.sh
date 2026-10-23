#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source_hook="$PWD/hooks/session-start.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fixture="$tmp/plugin with spaces"
mkdir -p "$fixture/hooks" "$fixture/scripts"
cp "$source_hook" "$fixture/hooks/session-start.sh"

cat > "$fixture/scripts/runtime.sh" <<'SCRIPT'
#!/usr/bin/env bash
[ "$1" = --if-installed ] && [ "$2" = session-start ] || exit 9
case "${TEST_RUNTIME_STATE:-}" in
  connected) exit 0 ;;
  pending) printf '%s\n' '{"systemMessage":"Ork Meter: finish browser setup."}' ;;
  partial) printf '%s\n' '{"systemMessage":"Ork Meter: half done."}'; exit 1 ;;
  *) exit 1 ;;
esac
SCRIPT
for state in connected pending failed partial; do
  output=$(TEST_RUNTIME_STATE="$state" bash "$fixture/hooks/session-start.sh" < tests/fixtures/session-start.startup.json)
  case "$state" in
    connected) [ -z "$output" ] ;;
    pending) [[ "$output" == *'finish browser setup'* ]] ;;
    failed) [[ "$output" == *'automatic capture is unavailable'* ]] ;;
    # Output from a runtime that then fails is dropped: one JSON document, never two.
    partial) [ "$output" = '{"systemMessage":"Ork Meter: automatic capture is unavailable."}' ] ;;
  esac
  if [ -n "$output" ] && command -v node >/dev/null 2>&1; then
    printf '%s' "$output" | node -e 'const v=JSON.parse(require("fs").readFileSync(0,"utf8"));if(Object.keys(v).length!==1||typeof v.systemMessage!=="string")process.exit(1)'
  fi
  echo "  ok   $state: fail-open session hook"
done
rm "$fixture/scripts/runtime.sh"
output=$(bash "$fixture/hooks/session-start.sh" < /dev/null)
[[ "$output" == *'automatic capture is unavailable'* ]]
[[ "$output" != *'/ork-meter:'* ]]
echo '  ok   missing runtime stays non-blocking'

# The real scripts/runtime.sh and a synthetic runtime (#140). A cold session
# starts the install detached and returns at once; the shims make the copy
# slow (a foreground install would show) and log every copy of the runtime.
case "$(uname -s)" in Darwin) os=darwin;; Linux) os=linux;; *) os=windows;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64;; *) arch=amd64;; esac
name=ork-meter-plugin-runtime
[ "$os" != windows ] || name="$name.exe"
real="$tmp/installed plugin"
mkdir -p "$real/hooks" "$real/scripts" "$real/runtime/$os-$arch" "$tmp/shims"
cp "$source_hook" "$real/hooks/session-start.sh"
cp scripts/runtime.sh "$real/scripts/runtime.sh"
cat > "$real/runtime/$os-$arch/$name" <<'SCRIPT'
#!/usr/bin/env bash
case "$1" in
  --version) echo synthetic ;;
  session-start) [ "$2" = --data-dir ] || exit 8
    printf '%s\n' '{"systemMessage":"Ork Meter: run /ork-meter:connect once to enable automatic token capture."}' ;;
  *) exit 9 ;;
esac
SCRIPT
chmod 700 "$real/runtime/$os-$arch/$name"
if command -v sha256sum >/dev/null 2>&1; then sha=$(sha256sum < "$real/runtime/$os-$arch/$name" | cut -d ' ' -f 1)
else sha=$(shasum -a 256 < "$real/runtime/$os-$arch/$name" | cut -d ' ' -f 1); fi
printf '%s\n' "$sha" > "$real/runtime/$os-$arch/SHA256"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "%s"\nsleep 2\nexec "%s" "$@"\n' "$tmp/cp.log" "$(command -v cp)" > "$tmp/shims/cp"
chmod 755 "$tmp/shims/cp"
export PATH="$tmp/shims:$PATH"
setting_up='{"systemMessage":"Ork Meter: setting up its runtime in the background; it is ready from your next session."}'
now() { python3 -c 'import time; print(time.time())'; }
hook() { # <data dir>: one session's hook output
  bash "$real/hooks/session-start.sh" "$1" < tests/fixtures/session-start.startup.json
}
installed() { # <data dir>: the install finished (binary, stamp, lock released)
  [ -x "$1/runtime/$sha/$name" ] && [ -f "$1/runtime/$sha/.verified" ] && [ ! -e "$1/runtime/.install-lock" ]
}
wait_installed() { # <data dir>
  local i
  for i in $(seq 1 300); do installed "$1" && return 0; sleep 0.1; done
  echo "  FAIL the background install did not finish in $1"; ls -laR "$1" || true; return 1
}
copies() { [ -f "$tmp/cp.log" ] && wc -l < "$tmp/cp.log" | tr -d ' ' || echo 0; }

data="$tmp/data one"
started=$(now)
output=$(hook "$data")
elapsed=$(python3 -c "import sys; print(round($(now) - $started, 2))")
[ "$output" = "$setting_up" ] && [ "$(printf '%s\n' "$output" | wc -l | tr -d ' ')" = 1 ]
python3 -c "import sys; sys.exit(0 if $elapsed < 1.5 else 1)" || { echo "  FAIL cold hook took $elapsed s"; exit 1; }
[ ! -e "$data/runtime/$sha/$name" ]
echo "  ok   cold: one line at once (${elapsed} s), the install not done yet"
wait_installed "$data"
[ "$(copies)" = 1 ]
echo '  ok   cold: the detached install completes after the hook returned'
output=$(hook "$data")
[[ "$output" == *'/ork-meter:connect'* ]] && [ "$(copies)" = 1 ]
echo '  ok   warm: the next session runs the installed runtime, no copy'

: > "$tmp/cp.log"
data="$tmp/data concurrent"
pids=()
for i in 1 2 3 4; do hook "$data" > "$tmp/out-$i" & pids+=($!); done
for pid in "${pids[@]}"; do wait "$pid"; done
for i in 1 2 3 4; do [ "$(cat "$tmp/out-$i")" = "$setting_up" ]; done
wait_installed "$data"
sleep 1
[ "$(copies)" = 1 ] || { echo "  FAIL concurrent sessions copied $(copies) times"; exit 1; }
echo '  ok   concurrent cold sessions install once'

case "${OSTYPE:-}" in
  msys* | cygwin*) echo '  skip killing the hook process group (no process groups on Windows)' ;;
  *)
    : > "$tmp/cp.log"
    data="$tmp/data killed"
    # The hook in a process group of its own, which is then killed whole, as
    # a host might on timeout: the installer is in another session.
    python3 -c 'import os, sys; os.setpgrp(); os.execvp(sys.argv[1], sys.argv[1:])' \
      bash "$real/hooks/session-start.sh" "$data" < tests/fixtures/session-start.startup.json > "$tmp/out-killed" &
    group=$!
    wait "$group"
    kill -KILL -- "-$group" 2>/dev/null || true
    [ "$(cat "$tmp/out-killed")" = "$setting_up" ]
    wait_installed "$data"
    [ "$(copies)" = 1 ]
    echo '  ok   the install survives its hook process group being killed' ;;
esac

data="$tmp/data damaged"
mkdir -p "$data"
hook "$data" >/dev/null; wait_installed "$data"
printf '\n# damaged\n' >> "$data/runtime/$sha/$name"
: > "$tmp/cp.log"
[ "$(hook "$data")" = "$setting_up" ]
wait_installed "$data"
[ "$(copies)" = 1 ] && [[ "$(hook "$data")" == *'/ork-meter:connect'* ]]
echo '  ok   a damaged install is replaced in the background, then runs'

# Nothing of the session reaches the installer: env -i keeps PATH, the temp
# and Windows system variables and ORK_DEV_RUNTIME only.
real_bash=$(command -v bash)
mkdir -p "$tmp/env-shims"
printf '#!%s\ncase " $* " in *" --install "*) env > "%s" ;; esac\nexec "%s" "$@"\n' \
  "$real_bash" "$tmp/installer.env" "$real_bash" > "$tmp/env-shims/bash"
chmod 755 "$tmp/env-shims/bash"
data="$tmp/data env"
[ "$(PATH="$tmp/env-shims:$PATH" ANTHROPIC_API_KEY=sk-session-secret CLAUDE_CODE_OAUTH_TOKEN=sk-session-secret \
     ORK_METER_TOKEN=sk-session-secret hook "$data")" = "$setting_up" ]
wait_installed "$data"
[ -s "$tmp/installer.env" ] && ! grep -q 'sk-session-secret' "$tmp/installer.env" \
  && ! grep -qE '^(ANTHROPIC_|CLAUDE_|ORK_METER_|HOME=)' "$tmp/installer.env" && grep -qx 'PWD=/' "$tmp/installer.env"
echo '  ok   the detached installer gets no session secrets (only PATH, temp and system variables), cwd /'

cp "$real/runtime/$os-$arch/$name" "$tmp/good-runtime"
printf '\n# changed\n' >> "$real/runtime/$os-$arch/$name"
data="$tmp/data broken package"
: > "$tmp/cp.log"
output=$(hook "$data")
[ "$output" = '{"systemMessage":"Ork Meter: automatic capture is unavailable."}' ]
sleep 0.5
[ ! -e "$data/runtime/.install-lock" ] && [ "$(copies)" = 0 ]
echo '  ok   a broken package is unavailable, with no background install'
