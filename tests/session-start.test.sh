#!/usr/bin/env bash
# Tests for hooks/session-start.sh: it starts silently (routine startup notices
# from the runtime, "setting up", "/ork-meter:connect", are never printed) and
# keeps only the failure warning, one JSON document, never two. A missing
# runtime installs detached (#140). First-run connect, tested at the end, adds
# at most one line of its own until the plugin is connected.
set -euo pipefail
cd "$(dirname "$0")/.."
source_hook="$PWD/hooks/session-start.sh"
tmp=$(mktemp -d)
# A detached installer may still be writing when the script ends: ignore rm's complaint.
trap 'rm -rf "$tmp" 2>/dev/null || true' EXIT
fails=0
# bash 3.2 does not exit on a failing [ ] or [[ ]] everywhere set -e might suggest, so
# every check reports and fails explicitly.
expect() { # <name> <got> <want>: exact string equality
  if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '${2:0:200}', want '${3:0:200}'"; fails=$((fails + 1)); fi
}
check() { # <name> <command...>: the command's success
  local name=$1; shift
  if "$@"; then echo "  ok   $name"; else echo "  FAIL $name"; fails=$((fails + 1)); fi
}
unavailable='{"systemMessage":"Ork Meter: automatic capture is unavailable."}'
# First-run connect is off until its own section at the end: these sections
# assert the silent start of a session that has it turned off, and no test
# may start a real connect (it would open a browser).
export ORK_METER_AUTO_CONNECT=off

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
# The runtime's own startup notices never reach stdout (session-start.sh sends
# them to /dev/null); only a failing runtime is reported, and as one document.
for state in connected pending failed partial; do
  output=$(TEST_RUNTIME_STATE="$state" bash "$fixture/hooks/session-start.sh" < tests/fixtures/session-start.startup.json)
  case "$state" in
    connected) expect "$state: silent" "$output" "" ;;
    pending) expect "$state: the runtime's notice is not printed" "$output" "" ;;
    failed) expect "$state: the failure warning" "$output" "$unavailable" ;;
    partial) expect "$state: the runtime's notice is dropped, the warning alone" "$output" "$unavailable" ;;
  esac
  if [ -n "$output" ] && command -v node >/dev/null 2>&1; then
    check "$state: one JSON object with systemMessage only" bash -c 'printf "%s" "$1" | node -e "const v=JSON.parse(require(\"fs\").readFileSync(0,\"utf8\"));if(Object.keys(v).length!==1||typeof v.systemMessage!==\"string\")process.exit(1)"' _ "$output"
  fi
done
rm "$fixture/scripts/runtime.sh"
output=$(bash "$fixture/hooks/session-start.sh" < /dev/null)
expect "missing runtime stays non-blocking: the warning, no command hint" "$output" "$unavailable"

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
# The synthetic runtime logs each session-start run, since its stdout is not shown.
cat > "$real/runtime/$os-$arch/$name" <<SCRIPT
#!/usr/bin/env bash
case "\$1" in
  --version) echo synthetic ;;
  session-start) [ "\$2" = --data-dir ] || exit 8
    printf '%s\n' "\$3" >> "$tmp/run.log"
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
digest() { if command -v sha256sum >/dev/null 2>&1; then sha256sum < "$1"; else shasum -a 256 < "$1"; fi | cut -d ' ' -f 1; }
wait_reinstalled() { # <data dir>: a damaged copy (which `installed` already accepts) replaced by a verified one
  local i
  for i in $(seq 1 300); do
    [ "$(copies)" -ge 1 ] && installed "$1" && [ "$(digest "$1/runtime/$sha/$name")" = "$sha" ] && return 0
    sleep 0.1
  done
  echo "  FAIL the background reinstall did not finish in $1"; ls -laR "$1" || true; return 1
}
runs() { [ -f "$tmp/run.log" ] && wc -l < "$tmp/run.log" | tr -d ' ' || echo 0; }

data="$tmp/data one"
started=$(now)
output=$(hook "$data")
elapsed=$(python3 -c "import sys; print(round($(now) - $started, 2))")
# No "setting up in the background" notice any more: a cold session is silent.
expect "cold: silent, at once (${elapsed} s)" "$output" ""
check "cold: the hook returned before the install (under 1.5 s)" python3 -c "import sys; sys.exit(0 if $elapsed < 1.5 else 1)"
check "cold: the install not done yet" test ! -e "$data/runtime/$sha/$name"
wait_installed "$data"
expect "cold: the detached install completes after the hook returned (one copy)" "$(copies)" 1
expect "cold: the runtime did not run in the cold session" "$(runs)" 0
output=$(hook "$data")
expect "warm: the next session runs the installed runtime (its connect notice not printed)" "$output" ""
expect "warm: the installed runtime ran once with the data dir" "$(tail -1 "$tmp/run.log")" "$data"
expect "warm: no copy" "$(copies)" 1

: > "$tmp/cp.log"
data="$tmp/data concurrent"
pids=()
for i in 1 2 3 4; do hook "$data" > "$tmp/out-$i" & pids+=($!); done
for pid in "${pids[@]}"; do wait "$pid"; done
for i in 1 2 3 4; do expect "concurrent cold session $i: silent" "$(cat "$tmp/out-$i")" ""; done
wait_installed "$data"
sleep 1
expect "concurrent cold sessions install once" "$(copies)" 1

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
    expect "killed group: the hook was silent" "$(cat "$tmp/out-killed")" ""
    wait_installed "$data"
    expect "the install survives its hook process group being killed" "$(copies)" 1 ;;
esac

data="$tmp/data damaged"
mkdir -p "$data"
hook "$data" >/dev/null; wait_installed "$data"
printf '\n# damaged\n' >> "$data/runtime/$sha/$name"
: > "$tmp/cp.log"; : > "$tmp/run.log"
expect "a damaged install: the session is silent while it is replaced" "$(hook "$data")" ""
wait_reinstalled "$data"
expect "a damaged install is replaced in the background (one copy)" "$(copies)" 1
expect "the damaged install did not run" "$(runs)" 0
expect "the replaced runtime then runs, silently" "$(hook "$data")" ""
expect "the replaced runtime ran with the data dir" "$(tail -1 "$tmp/run.log")" "$data"

# Nothing of the session reaches the installer: env -i keeps PATH, the temp
# and Windows system variables and ORK_DEV_RUNTIME only.
real_bash=$(command -v bash)
mkdir -p "$tmp/env-shims"
printf '#!%s\ncase " $* " in *" --install "*) env > "%s" ;; esac\nexec "%s" "$@"\n' \
  "$real_bash" "$tmp/installer.env" "$real_bash" > "$tmp/env-shims/bash"
chmod 755 "$tmp/env-shims/bash"
data="$tmp/data env"
expect "env: the cold session is silent" "$(PATH="$tmp/env-shims:$PATH" ANTHROPIC_API_KEY=sk-session-secret CLAUDE_CODE_OAUTH_TOKEN=sk-session-secret \
     ORK_METER_TOKEN=sk-session-secret hook "$data")" ""
wait_installed "$data"
check "the detached installer gets no session secrets (only PATH, temp and system variables), cwd /" bash -c '
  [ -s "$1" ] && ! grep -q "sk-session-secret" "$1" && ! grep -qE "^(ANTHROPIC_|CLAUDE_|ORK_METER_|HOME=)" "$1" && grep -qx "PWD=/" "$1"' _ "$tmp/installer.env"

cp "$real/runtime/$os-$arch/$name" "$tmp/good-runtime"
printf '\n# changed\n' >> "$real/runtime/$os-$arch/$name"
data="$tmp/data broken package"
: > "$tmp/cp.log"
output=$(hook "$data")
expect "a broken package is unavailable" "$output" "$unavailable"
sleep 0.5
check "a broken package starts no background install" test ! -e "$data/runtime/.install-lock"
expect "a broken package is never copied" "$(copies)" 0

# First-run connect: until connected, the first session that can show a
# browser starts `runtime.sh connect` detached, says so in one line and
# records the attempt; afterwards a nudge at most once a day; never once
# connected; only the nudge where no browser can open; nothing with
# ORK_METER_AUTO_CONNECT=off.
unset ORK_METER_AUTO_CONNECT
first="$tmp/first run"
mkdir -p "$first/hooks" "$first/scripts"
cp "$source_hook" "$first/hooks/session-start.sh"
# Detached children get a scrubbed environment, so the paths are baked in.
cat > "$first/scripts/runtime.sh" <<SCRIPT
#!/usr/bin/env bash
case "\$1" in
  --if-installed)
    case "\${TEST_RUNTIME_STATE:-}" in
      connected) exit 0 ;;
      notconnected) printf '%s\n' '{"systemMessage":"Ork Meter: run /ork-meter:connect once to enable automatic token capture."}'; exit 0 ;;
      cold) exit 75 ;;
      *) exit 1 ;;
    esac ;;
  --plugin-data)
    case "\$3" in
      connect) printf '%s\n' "\$*" >> "$tmp/connect.log"; env > "$tmp/connect.env"; pwd > "$tmp/connect.pwd"; sleep 3 ;;
      --install) printf '%s\n' "\$2" >> "$tmp/install.log" ;;
    esac ;;
esac
exit 0
SCRIPT
opened='{"systemMessage":"Ork Meter: opening its setup page in your browser to connect (first run only; nothing is sent until you approve there). Not now? Close it; /ork-meter:connect works any time."}'
nudge='{"systemMessage":"Ork Meter: not connected yet. Run /ork-meter:connect to enable automatic token capture (ORK_METER_AUTO_CONNECT=off hides this)."}'
# A desktop session: attended, a display, no CI and no SSH.
# Then: [-u NAME | NAME=value]... command...
desktop() {
  (
    unset CI SSH_CONNECTION SSH_CLIENT SSH_TTY ORK_METER_AUTO_CONNECT
    export CLAUDE_CODE_SESSION_ATTENDED=1 CLAUDE_CODE_ENTRYPOINT=cli DISPLAY=:99
    while [ "$#" -gt 0 ]; do
      case "$1" in -u) unset "$2"; shift 2 ;; *=*) export "$1"; shift ;; *) break ;; esac
    done
    "$@"
  )
}
first_hook() { # <data dir> [state]: one session's hook output, with CLAUDE_PLUGIN_DATA set as Claude Code sets it
  CLAUDE_PLUGIN_DATA="$1" TEST_RUNTIME_STATE="${2:-notconnected}" bash "$first/hooks/session-start.sh" "$1" \
    < tests/fixtures/session-start.startup.json
}
launches() { [ -f "$tmp/connect.log" ] && wc -l < "$tmp/connect.log" | tr -d ' ' || echo 0; }
wait_launches() { # <count>
  local i
  for i in $(seq 1 50); do [ "$(launches)" -ge "$1" ] && return 0; sleep 0.1; done
  return 1
}

data="$tmp/first data"
started=$(now)
output=$(desktop ANTHROPIC_API_KEY=sk-session-secret CLAUDE_CONFIG_DIR="$tmp/claude config" HOME="$tmp/home" first_hook "$data")
elapsed=$(python3 -c "print(round($(now) - $started, 2))")
expect "first run: one line saying the setup page is opening" "$output" "$opened"
check "first run: the hook did not wait for connect (${elapsed} s, under 1.5 s)" python3 -c "import sys; sys.exit(0 if $elapsed < 1.5 else 1)"
check "first run: connect started in the background" wait_launches 1
expect "first run: the production connect, with this data dir" "$(head -1 "$tmp/connect.log")" "--plugin-data $data connect --environment production"
expect "first run: the attempt is recorded" "$(cut -d ' ' -f 1 "$data/first-run-connect")" opened
check "connect gets the desktop, home and Claude config but no session secret, cwd /" bash -c '
  ! grep -q sk-session-secret "$1" && ! grep -q "^ANTHROPIC_" "$1" && grep -qx "DISPLAY=:99" "$1" \
    && grep -qx "HOME=$2/home" "$1" && grep -qx "CLAUDE_CONFIG_DIR=$2/claude config" "$1" && [ "$(cat "$3")" = / ]' \
  _ "$tmp/connect.env" "$tmp" "$tmp/connect.pwd"
expect "second session the same day: silent" "$(desktop first_hook "$data")" ""
expect "second session: connect not started again" "$(launches)" 1
printf '%s\n' "$(( $(date +%s) - 90000 ))" > "$data/first-run-connect-nudge"
expect "a day later: the one-line nudge, no browser" "$(desktop first_hook "$data")" "$nudge"
expect "a day later: still silent after the nudge" "$(desktop first_hook "$data")" ""
sleep 0.3
expect "after the first attempt connect never starts again" "$(launches)" 1
printf '{"reference":"r","origin":"https://orkmeter.com"}\n' > "$data/connection.json"
printf '%s\n' "$(( $(date +%s) - 90000 ))" > "$data/first-run-connect-nudge"
expect "connected (connection.json): silent" "$(desktop first_hook "$data")" ""
expect "connected: recorded" "$(cut -d ' ' -f 1 "$data/first-run-connect")" connected
rm "$data/connection.json"
expect "after a disconnect: silent, no nudge, no browser" "$(desktop first_hook "$data")" ""

data="$tmp/first connected"
expect "connected before connection.json (runtime silent): silent" "$(desktop first_hook "$data" connected)" ""
expect "connected before connection.json: recorded, never opened" "$(cut -d ' ' -f 1 "$data/first-run-connect")" connected

data="$tmp/first off"
for value in off OFF 0 false no; do
  expect "ORK_METER_AUTO_CONNECT=$value: silent" "$(desktop ORK_METER_AUTO_CONNECT=$value first_hook "$data")" ""
done
check "ORK_METER_AUTO_CONNECT=off: nothing recorded" test ! -e "$data/first-run-connect" -a ! -e "$data/first-run-connect-nudge"

data="$tmp/first failed"
expect "an unavailable runtime: the warning only" "$(desktop first_hook "$data" failed)" "$unavailable"

: > "$tmp/connect.log"; : > "$tmp/install.log"
data="$tmp/first cold"
expect "first session after install (runtime not yet installed): opening" "$(desktop first_hook "$data" cold)" "$opened"
check "cold: connect started" wait_launches 1
check "cold: the background install started too" bash -c 'for i in $(seq 1 50); do grep -qx "$2" "$1" 2>/dev/null && exit 0; sleep 0.1; done; exit 1' _ "$tmp/install.log" "$data"

# No browser here: the nudge, nothing recorded as an attempt, so the first
# desktop session still opens the page once.
for headless in "CI=true" "CLAUDE_CODE_SESSION_ATTENDED=0" "CLAUDE_CODE_ENTRYPOINT=sdk-cli" \
                "SSH_CONNECTION=10.0.0.1 -u DISPLAY"; do
  : > "$tmp/connect.log"
  data="$tmp/first headless $headless"
  output=$(desktop $headless first_hook "$data")
  expect "$headless: the nudge only" "$output" "$nudge"
  sleep 0.2
  expect "$headless: no connect" "$(launches)" 0
  check "$headless: no attempt recorded" test ! -e "$data/first-run-connect"
  expect "$headless: rate-limited" "$(desktop $headless first_hook "$data")" ""
  expect "$headless, then a desktop session: opening once" "$(desktop first_hook "$data")" "$opened"
  check "$headless, then a desktop session: connect started" wait_launches 1
done

case "$(uname -s)-$(uname -r)" in
  Linux-*[Mm]icrosoft*) ;;
  Linux-*)
    : > "$tmp/connect.log"
    data="$tmp/first linux no display"
    expect "Linux without a display: the nudge only" "$(desktop -u DISPLAY -u WAYLAND_DISPLAY first_hook "$data")" "$nudge"
    sleep 0.2
    expect "Linux without a display: no connect" "$(launches)" 0
    expect "Linux with Wayland: opening" "$(desktop -u DISPLAY WAYLAND_DISPLAY=wayland-0 first_hook "$data")" "$opened" ;;
esac

: > "$tmp/connect.log"
data="$tmp/first concurrent"
pids=()
for i in 1 2 3 4; do desktop first_hook "$data" > "$tmp/first-out-$i" & pids+=($!); done
for pid in "${pids[@]}"; do wait "$pid"; done
expect "concurrent first sessions: one says opening" "$(cat "$tmp"/first-out-* | grep -c 'opening its setup page')" 1
wait_launches 1; sleep 0.5
expect "concurrent first sessions: connect started once" "$(launches)" 1
if command -v node >/dev/null 2>&1; then
  for out in "$opened" "$nudge"; do
    check "one JSON object with systemMessage only: ${out:16:40}" bash -c 'printf "%s" "$1" | node -e "const v=JSON.parse(require(\"fs\").readFileSync(0,\"utf8\"));if(Object.keys(v).length!==1||typeof v.systemMessage!==\"string\")process.exit(1)"' _ "$out"
  done
fi

[ "$fails" -eq 0 ]
