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

# The configured origin (ORK_METER_PLUGIN_ORIGIN in managed or user settings;
# tests/project_env_regressions.py covers where it is read from) goes to the
# runtime, and the runtime's line saying nothing is sent because of it is the
# one runtime line that is shown.
origin_fixture="$tmp/origin plugin"
mkdir -p "$origin_fixture/hooks" "$origin_fixture/scripts"
cp "$source_hook" "$origin_fixture/hooks/session-start.sh"
cat > "$origin_fixture/hooks/py.sh" <<'SCRIPT'
#!/usr/bin/env bash
[ "$1" = --script ] && [ "$2" = scripts/pairing.py ] && [ "$3" = configured-origin ] || exit 9
[ -z "${TEST_PY_FAILS:-}" ] || exit 1
[ -z "${TEST_CONFIGURED_ORIGIN:-}" ] || printf '%s\n' "$TEST_CONFIGURED_ORIGIN"
SCRIPT
cat > "$origin_fixture/scripts/runtime.sh" <<'SCRIPT'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$TEST_RUNTIME_ARGS"
case "${TEST_RUNTIME_STATE:-}" in
  mismatch) printf '%s\n' '{"systemMessage":"Ork Meter: this computer is connected to https://orkmeter.com but is configured for https://ome.example-customer.com. Nothing is being sent. Run /ork-meter:disconnect, then /ork-meter:connect to connect to https://ome.example-customer.com."}' ;;
  unusable) printf '%s\n' '{"systemMessage":"Ork Meter: the configured destination is not an exact https origin, or could not be read, so nothing is being sent. Ask your administrator to check ORK_METER_PLUGIN_ORIGIN."}' ;;
  lapsed) printf '%s\n' '{"systemMessage":"Ork Meter: there is no current deployment attestation for https://ome.example-customer.com, so nothing is being sent. It is fetched again automatically; if this stays, ask your administrator whether that deployment is still in service."}' ;;
  expiring) printf '%s\n' '{"systemMessage":"Ork Meter: this connection expires in 3 days."}' ;;
esac
exit 0
SCRIPT
export TEST_RUNTIME_ARGS="$tmp/runtime-args"
origin_data="$tmp/origin data"
mkdir -p "$origin_data"
start_with_origin() { # [VAR=value ...]: runs the hook; sets $output and $args
  rm -f "$TEST_RUNTIME_ARGS"
  output=$(env "$@" bash "$origin_fixture/hooks/session-start.sh" "$origin_data" < tests/fixtures/session-start.startup.json)
  args=$(cat "$TEST_RUNTIME_ARGS" 2>/dev/null)
}
start_with_origin
expect "nothing configured: the runtime is told nothing" "$args" "--if-installed session-start"
start_with_origin TEST_CONFIGURED_ORIGIN=https://ome.example-customer.com
expect "a configured origin goes to the runtime" "$args" "--if-installed session-start --configured-origin https://ome.example-customer.com"
expect "and a session with no conflict stays silent" "$output" ""
start_with_origin TEST_PY_FAILS=1
expect "the setting could not be determined and none was in force: nothing is said" "$args" "--if-installed session-start"
: > "$origin_data/configured-origin"
start_with_origin TEST_PY_FAILS=1
expect "the setting could not be determined and one was in force: the runtime is told so, not told there is none" "$args" "--if-installed session-start --configured-origin unreadable"
rm -f "$origin_data/configured-origin"
for state in mismatch unusable lapsed; do
  start_with_origin TEST_RUNTIME_STATE="$state" TEST_CONFIGURED_ORIGIN=https://ome.example-customer.com
  case "$output" in
    '{"systemMessage":"Ork Meter: '*'othing is being sent'*'"}') echo "  ok   $state: the runtime's line is shown" ;;
    *) echo "  FAIL $state: the runtime's line is not shown: '${output:0:160}'"; fails=$((fails + 1)) ;;
  esac
  if command -v node >/dev/null 2>&1; then
    check "$state: one JSON object with systemMessage only" bash -c 'printf "%s" "$1" | node -e "const v=JSON.parse(require(\"fs\").readFileSync(0,\"utf8\"));if(Object.keys(v).length!==1||typeof v.systemMessage!==\"string\")process.exit(1)"' _ "$output"
  fi
done
start_with_origin TEST_RUNTIME_STATE=expiring
expect "every other runtime notice is still not printed" "$output" ""

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
# browser starts `runtime.sh connect` detached, says so in one line once
# connect answers that it opened the page, and records the attempt; afterwards a nudge at most once a day; never once
# connected; only the nudge where no browser can open; nothing with
# ORK_METER_AUTO_CONNECT=off.
unset ORK_METER_AUTO_CONNECT
first="$tmp/first run"
mkdir -p "$first/hooks" "$first/scripts"
cp "$source_hook" "$first/hooks/session-start.sh"
# The configured origin, as scripts/pairing.py configured-origin prints it:
# the contents of $tmp/configured-origin, none when it is absent; a failure
# (settings unreadable) while $tmp/configured-origin-fails exists.
cat > "$first/hooks/py.sh" <<SCRIPT
#!/usr/bin/env bash
[ ! -e "$tmp/configured-origin-fails" ] || exit 1
[ ! -e "$tmp/configured-origin" ] || cat "$tmp/configured-origin"
SCRIPT
# How the stub connect answers ($tmp/connect.mode): opened (default), refused,
# slow (opened, after 4 s, longer than the hook waits).
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
      connect)
        printf '%s\n' "\$*" >> "$tmp/connect.log"; env > "$tmp/connect.env"; pwd > "$tmp/connect.pwd"
        case "\$(cat "$tmp/connect.mode" 2>/dev/null)" in
          refused) printf '%s\n' '{"error":"origin_conflict","message":"refused"}'; exit 1 ;;
          slow) sleep 4 ;;
        esac
        printf '%s\n' '{"destination":"https://orkmeter.com","message":"m","state":"setup_opened"}' ;;
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
output=$(desktop ANTHROPIC_API_KEY=sk-session-secret CLAUDE_CONFIG_DIR="$tmp/claude config" HOME="$tmp/home" \
  SSL_CERT_FILE="$tmp/ca.pem" SSL_CERT_DIR="$tmp/certs" ORK_METER_VAULT_DIR="$tmp/vault" first_hook "$data")
elapsed=$(python3 -c "print(round($(now) - $started, 2))")
expect "first run: one line saying the setup page is opening" "$output" "$opened"
# The stub answers at once, so the hook must not sit out its 2.5 s wait
# (Windows runners take about 1.8 s for the whole hook).
check "first run: the hook waited only for connect's answer (${elapsed} s, under 2.5 s)" python3 -c "import sys; sys.exit(0 if $elapsed < 2.5 else 1)"
check "first run: the answer is read and removed" test ! -e "$data/first-run-connect-answer"
check "first run: connect started in the background" wait_launches 1
expect "first run: the production connect, with this data dir" "$(head -1 "$tmp/connect.log")" "--plugin-data $data connect --environment production"
expect "first run: the attempt is recorded" "$(cut -d ' ' -f 1 "$data/first-run-connect")" opened
check "connect gets the desktop, home and Claude config but no session secret, cwd /" bash -c '
  ! grep -q sk-session-secret "$1" && ! grep -q "^ANTHROPIC_" "$1" && grep -qx "DISPLAY=:99" "$1" \
    && grep -qx "HOME=$2/home" "$1" && grep -qx "CLAUDE_CONFIG_DIR=$2/claude config" "$1" && [ "$(cat "$3")" = / ]' \
  _ "$tmp/connect.env" "$tmp" "$tmp/connect.pwd"
check "connect keeps the TLS trust and vault settings" bash -c '
  grep -qx "SSL_CERT_FILE=$2/ca.pem" "$1" && grep -qx "SSL_CERT_DIR=$2/certs" "$1" && grep -qx "ORK_METER_VAULT_DIR=$2/vault" "$1"' \
  _ "$tmp/connect.env" "$tmp"
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

# A configured origin: connect starts there, never at production.
: > "$tmp/connect.log"
printf '%s\n' https://ome.example-customer.com > "$tmp/configured-origin"
data="$tmp/first configured"
expect "configured origin: opening once connect opened the page" "$(desktop first_hook "$data")" "$opened"
check "configured origin: connect started" wait_launches 1
expect "configured origin: connect goes to that origin" "$(head -1 "$tmp/connect.log")" "--plugin-data $data connect --origin https://ome.example-customer.com"

# Refused (no current attestation, say): no page, so no line saying one opens.
: > "$tmp/connect.log"
printf refused > "$tmp/connect.mode"
data="$tmp/first refused"
expect "connect refused: no opening line" "$(desktop first_hook "$data")" ""
check "connect refused: it was started" wait_launches 1
expect "connect refused: the attempt is recorded" "$(cut -d ' ' -f 1 "$data/first-run-connect")" opened
check "connect refused: the answer is removed" test ! -e "$data/first-run-connect-answer"
rm -f "$tmp/configured-origin"

# Slower than the hook waits: the page still opens, without the line, and the
# hook stays well inside its 5 s limit.
: > "$tmp/connect.log"
printf slow > "$tmp/connect.mode"
data="$tmp/first slow"
started=$(now)
output=$(desktop first_hook "$data")
elapsed=$(python3 -c "print(round($(now) - $started, 2))")
expect "slow connect: no opening line" "$output" ""
# Its 2.5 s wait plus the hook's own work (about 1.1 s on Windows runners),
# inside Claude Code's 5 s hook limit.
check "slow connect: the hook gave up waiting (${elapsed} s, under 4.5 s)" python3 -c "import sys; sys.exit(0 if $elapsed < 4.5 else 1)"
check "slow connect: started" wait_launches 1
rm -f "$tmp/connect.mode"

# An unreadable configured origin: connect would be refused, so it is not started.
: > "$tmp/connect.log"
data="$tmp/first unreadable"
mkdir -p "$data"; : > "$data/configured-origin"
: > "$tmp/configured-origin-fails"
expect "unreadable configured origin (runtime cold): silent" "$(desktop first_hook "$data" cold)" ""
sleep 0.3
expect "unreadable configured origin: no connect" "$(launches)" 0
rm -f "$tmp/configured-origin-fails"

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
