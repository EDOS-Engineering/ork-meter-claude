#!/usr/bin/env bash
# Opted-in automatic receiver startup; failures never block inference.
# Until the plugin is connected it also starts /ork-meter:connect once by
# itself (first_run_connect below), so a new install needs no typed command.
# As hooks/py.sh: no external dirname, and the plugin root resolved physically.
case $0 in */* | *\\*) here=${0%[/\\]*} ;; *) here=. ;; esac
root=$(cd -P "$here/.." && pwd -P) || exit 0
if [ -n "${1:-}" ]; then export CLAUDE_PLUGIN_DATA="$1"; fi
cat >/dev/null 2>&1 || true

# The first session after an install or update has no installed runtime for
# this plugin's SHA. Installing it (a 12 MB copy, two digests and, on Windows,
# a first exec that Defender scans) can outlast the hook's 5 s timeout, so it
# runs detached and this session goes on without it (#140):
# - a double fork, so the installer is no child of the hook: a tree kill that
#   follows parent ids (taskkill /T) and the hook's exit leave it alone;
# - its own session where setsid exists (Linux) or macOS's perl can make one,
#   so a signal to the hook's process group does not reach it, and HUP, INT
#   and TERM ignored on every platform;
# - stdin, stdout and stderr off the hook's pipes, which Claude Code waits on
#   (stdout only briefly on a pipe of this hook's own, below);
# - the root directory as its cwd, and an empty environment but PATH, the temp and Windows system variables and
#   ORK_DEV_RUNTIME: it copies and checks files and runs `--version`, and
#   needs nothing of the session.
# scripts/runtime.sh --install takes a lock (concurrent sessions install once)
# and verifies the package before the copy is used.
# Starts `scripts/runtime.sh <args>` detached, as described above: a double
# fork, its own session, HUP, INT and TERM ignored, stdio off the hook's pipes,
# cwd /, and an environment of PATH plus the variables named in $keep_names.
keep_names=(PATH TMPDIR TEMP TMP SYSTEMROOT WINDIR ORK_DEV_RUNTIME)
run_detached() { # <runtime.sh args...>
  local runner=() keep=() name
  case "${OSTYPE:-}" in
    msys* | cygwin*) ;;
    *)
      if command -v setsid >/dev/null 2>&1; then runner=(setsid)
      elif [ -x /usr/bin/perl ]; then runner=(/usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV or exit 127')
      fi ;;
  esac
  for name in "${keep_names[@]}"; do
    [ -z "${!name+set}" ] || keep+=("$name=${!name}")
  done
  spawn() {
    (
      trap '' HUP INT TERM
      # Not the project's directory (Windows keeps a process's cwd in use);
      # $2 is the --plugin-data value, kept relative to this one if relative.
      case "$2" in /* | [A-Za-z]:[\\/]*) cd / 2>/dev/null || true ;; esac
      # bash -c closes stdout first thing, as runtime.sh --install does: the
      # signal, read below, that setsid has already run.
      env -i "${keep[@]}" "${runner[@]}" bash -c 'exec >/dev/null 2>&1 </dev/null; exec bash "$@"' bash \
        "$root/scripts/runtime.sh" "$@" </dev/null 2>/dev/null &
    ) </dev/null 2>/dev/null
  }
  # With setsid, the child's stdout is a pipe read here until it closes it,
  # which it does first thing: by then setsid has run, so the hook never
  # exits (and its group is never killed) before the child has left that
  # group. Bounded at 2 s.
  if [ "${#runner[@]}" -gt 0 ]; then
    spawn "$@" | { read -r -t 2 _ || true; }
  else
    spawn "$@" >/dev/null
  fi
}
install_in_background() { run_detached --plugin-data "$1" --install; }

# First-run connect. Until this Claude configuration is connected, the first
# session that can show a browser starts what /ork-meter:connect starts: the
# runtime's local setup page (plan, telemetry consent, sign-in, approval), and
# says so in one line. Opening the page is not consent: the runtime sends
# nothing but the platform's anonymous readiness check (GET /auth/config)
# before the person approves there, and Claude's settings change only on
# approval. The attempt is recorded in $data/first-run-connect so it happens
# once. After that, or where no browser can open (CI, `claude -p`, SSH without
# a display, Linux without a display), a one-line nudge at most once a day,
# and nothing at all once the plugin has been connected (the record then
# says "connected", so a later /ork-meter:disconnect is not undone).
# ORK_METER_AUTO_CONNECT=off turns all of it off.
nudge_interval=86400
attempt_marker() { printf '%s/first-run-connect' "$CLAUDE_PLUGIN_DATA"; }
nudge_marker() { printf '%s/first-run-connect-nudge' "$CLAUDE_PLUGIN_DATA"; }
auto_connect_off() {
  case "$(printf '%s' "${ORK_METER_AUTO_CONNECT:-}" | tr '[:upper:]' '[:lower:]')" in
    off | 0 | false | no) return 0 ;;
  esac
  return 1
}
# 0 when this session can show a browser to a person at this computer.
can_open_browser() {
  [ -z "${CI:-}" ] || return 1
  # Claude Code: 0 for -p/--print and SDK sessions, sdk-* entry points too.
  [ "${CLAUDE_CODE_SESSION_ATTENDED:-}" != 0 ] || return 1
  case "${CLAUDE_CODE_ENTRYPOINT:-}" in sdk-*) return 1 ;; esac
  if [ -n "${SSH_CONNECTION:-}${SSH_CLIENT:-}${SSH_TTY:-}" ] && [ -z "${DISPLAY:-}" ]; then return 1; fi
  case "$(uname -s 2>/dev/null)" in
    Darwin | MINGW* | MSYS* | CYGWIN*) return 0 ;;
    Linux)
      # WSL opens the Windows browser; elsewhere a display is needed.
      case "$(uname -r 2>/dev/null)" in *[Mm]icrosoft*) return 0 ;; esac
      [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] ;;
    *) return 1 ;;
  esac
}
start_connect() {
  local name
  # connect needs the person's desktop and credential store, and Claude's
  # config directory; never the session's API keys, tokens or settings.
  keep_names=(PATH HOME USER LOGNAME LANG TMPDIR TEMP TMP SYSTEMROOT WINDIR USERPROFILE APPDATA LOCALAPPDATA
    PROGRAMDATA COMSPEC PATHEXT DISPLAY WAYLAND_DISPLAY XAUTHORITY BROWSER DBUS_SESSION_BUS_ADDRESS
    XDG_RUNTIME_DIR XDG_CONFIG_HOME XDG_DATA_HOME XDG_DATA_DIRS XDG_STATE_HOME XDG_CURRENT_DESKTOP
    WSL_DISTRO_NAME WSL_INTEROP CLAUDE_CONFIG_DIR ORK_DEV_RUNTIME
    HTTPS_PROXY HTTP_PROXY NO_PROXY https_proxy http_proxy no_proxy)
  for name in $(compgen -e); do
    case "$name" in LC_*) keep_names+=("$name") ;; esac
  done
  run_detached --plugin-data "$CLAUDE_PLUGIN_DATA" connect --environment production
}
now_epoch() { date +%s 2>/dev/null; }
# Prints the session's one line, or nothing. $1: 1 when this Claude
# configuration is connected.
first_run_connect() {
  local connected=$1 marker nudge now last=
  ! auto_connect_off || return 0
  case "${CLAUDE_PLUGIN_DATA:-}" in /* | [A-Za-z]:[\\/]*) ;; *) return 0 ;; esac
  marker=$(attempt_marker) nudge=$(nudge_marker)
  now=$(now_epoch)
  [[ "$now" =~ ^[0-9]+$ ]] || return 0
  if [ "$connected" = 1 ]; then
    [ "$(head -c 9 "$marker" 2>/dev/null)" = connected ] && return 0
    (umask 077 && mkdir -p "$CLAUDE_PLUGIN_DATA" && printf 'connected %s\n' "$now" > "$marker") 2>/dev/null
    return 0
  fi
  [ "$(head -c 9 "$marker" 2>/dev/null)" != connected ] || return 0
  if [ ! -e "$marker" ] && can_open_browser; then
    # noclobber creates the record exclusively: of concurrent first
    # sessions, one opens the page.
    if (umask 077 && mkdir -p "$CLAUDE_PLUGIN_DATA" && set -C && printf 'opened %s\n' "$now" > "$marker") 2>/dev/null; then
      if start_connect >/dev/null 2>&1; then
        (umask 077 && printf '%s\n' "$now" > "$nudge") 2>/dev/null
        printf '%s\n' "Ork Meter: opening its setup page in your browser to connect (first run only; nothing is sent until you approve there). Not now? Close it; /ork-meter:connect works any time."
        return 0
      fi
    else
      return 0 # another session is opening it
    fi
  fi
  last=$(head -c 20 "$nudge" 2>/dev/null | tr -d '[:space:]')
  if [[ "$last" =~ ^[0-9]+$ ]] && [ $((now - last)) -lt "$nudge_interval" ] && [ "$last" -le "$now" ]; then
    return 0
  fi
  (umask 077 && mkdir -p "$CLAUDE_PLUGIN_DATA" && printf '%s\n' "$now" > "$nudge") 2>/dev/null || return 0
  printf '%s\n' "Ork Meter: not connected yet. Run /ork-meter:connect to enable automatic token capture (ORK_METER_AUTO_CONNECT=off hides this)."
}
json_message() { # <text without quotes or backslashes>
  printf '{"systemMessage":"%s"}\n' "$1"
}

# Start silently; retain the failure warning, without routine startup notices.
# The runtime's own lines are not shown, but its not-connected one tells
# first_run_connect that this configuration is not connected.
status=0
notice=$(bash "$root/scripts/runtime.sh" --if-installed session-start 2>/dev/null) || status=$?
connected=0
if [ -f "${CLAUDE_PLUGIN_DATA:-}/connection.json" ]; then
  connected=1 # written at connect, removed at disconnect
elif [ "$status" -eq 0 ] && [[ "$notice" != *"run /ork-meter:connect once"* ]]; then
  connected=1 # an enabled connection from a runtime that predates that file
fi
if [ "$status" -eq 0 ]; then
  :
elif [ "$status" -eq 75 ] && install_in_background "$CLAUDE_PLUGIN_DATA" >/dev/null 2>&1; then
  :
else
  json_message "Ork Meter: automatic capture is unavailable."
  exit 0
fi
line=$(first_run_connect "$connected" 2>/dev/null) || line=
[ -z "$line" ] || json_message "$line"
exit 0
