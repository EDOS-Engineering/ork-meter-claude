#!/usr/bin/env bash
# Opted-in automatic receiver startup; failures never block inference.
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
install_in_background() {
  local data=$1 runner=() keep=() name
  case "${OSTYPE:-}" in
    msys* | cygwin*) ;;
    *)
      if command -v setsid >/dev/null 2>&1; then runner=(setsid)
      elif [ -x /usr/bin/perl ]; then runner=(/usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV or exit 127')
      fi ;;
  esac
  for name in PATH TMPDIR TEMP TMP SYSTEMROOT WINDIR ORK_DEV_RUNTIME; do
    [ -z "${!name+set}" ] || keep+=("$name=${!name}")
  done
  spawn() {
    (
      trap '' HUP INT TERM
      # Not the project's directory (Windows keeps a process's cwd in use).
      case "$data" in /* | [A-Za-z]:[\\/]*) cd / 2>/dev/null || true ;; esac
      env -i "${keep[@]}" "${runner[@]}" bash "$root/scripts/runtime.sh" --plugin-data "$data" --install \
        </dev/null 2>/dev/null &
    ) </dev/null 2>/dev/null
  }
  # With setsid, the installer's stdout is a pipe read here until
  # runtime.sh --install closes it, which it does first thing: by then setsid
  # has run, so the hook never exits (and its group is never killed) before
  # the installer has left that group. Bounded at 2 s.
  if [ "${#runner[@]}" -gt 0 ]; then
    spawn | { read -r -t 2 _ || true; }
  else
    spawn >/dev/null
  fi
}

# Emit exactly one JSON document: the runtime's output only if it succeeded.
status=0
output=$(bash "$root/scripts/runtime.sh" --if-installed session-start 2>/dev/null) || status=$?
if [ "$status" -eq 0 ]; then
  [ -z "$output" ] || printf '%s\n' "$output"
elif [ "$status" -eq 75 ] && install_in_background "$CLAUDE_PLUGIN_DATA"; then
  printf '%s\n' '{"systemMessage":"Ork Meter: setting up its runtime in the background; it is ready from your next session."}'
else
  printf '%s\n' '{"systemMessage":"Ork Meter: automatic capture is unavailable."}'
fi
exit 0
