#!/usr/bin/env bash
# Runs one of the plugin's Python hooks, or does nothing; or runs one of the
# plugin's scripts for a skill.
#
# Every Ork Meter hook must leave Claude Code alone when it can't run: no
# python3 on PATH, a crash, a timeout. This wrapper needs python3 (3.10 or
# later, the plugin's one floor) and runs the hook through hooks/lib/run.py, the
# runtime that parses the payload, applies the opt-outs and prints the reply
# (see hooks/lib/hook.py), with -I so neither the environment nor the current
# directory can change what it imports. Whatever happens, it exits 0 and
# prints nothing but the hook's own stdout.
#
# A script (scripts/savings.py, scripts/audit.py; the skills call them this
# way) is the opposite case: the person asked for its output, so it runs
# with the same isolation (python3 -I) but without the hook runtime, its
# stderr stays visible and its exit status is passed through. A missing
# python3 is reported on stderr with exit 1.
#
# Compiled bytecode goes to $CLAUDE_PLUGIN_DATA/pycache when that is an
# absolute path (python3 -X pycache_prefix=...), so hooks/lib isn't compiled
# again on every call; the directory is owner-only (0700), and a link or a
# directory someone else owns there is refused, as is a data directory that
# isn't this user's or that others may write. Otherwise, or when it can't be
# made, nothing is cached (python3 -B), as before. Every run passes
# --check-hash-based-pycs always, so an unchecked-hash .pyc is never trusted
# over its source.
#
# Usage: py.sh <script-name-under-hooks/>                 (hook payload on stdin)
#        py.sh --script <path-under-the-plugin-root> [args...]

# When the host launched this hook, as early as the script can tell: bash 5
# (Git Bash on Windows too) has $EPOCHREALTIME; bash 3.2 (macOS) has not, and
# then nothing is passed and the hook counts from its own start. A value
# inherited from a parent shell is replaced here, and hooks/lib/run.py
# ignores an implausible one (hook.launch_age).
_ork_launched=
if [ "${BASH_VERSINFO[0]:-0}" -ge 5 ]; then _ork_launched=${EPOCHREALTIME:-}; fi
unset ORK_HOOK_LAUNCHED_AT

# The Write guard (hooks/write-guard.py, PreToolUse on Write and PostToolUse
# on Read, Bash and Grep) has nothing to do while no compacted Read has left a
# record. Without one, skip the interpreter (about 50 ms on every such call):
# drain the payload and exit. A record in any place Python's state directory
# could resolve to (hooks/lib/features.py state_dir: $XDG_STATE_HOME, else
# $HOME/.local/state, or %USERPROFILE% on Windows) runs the hook; with none of
# those set, it runs too.
_ork_guard_records() {
  local base found=1 any=
  for base in "${XDG_STATE_HOME:-}" "${HOME:+$HOME/.local/state}" "${USERPROFILE:+$USERPROFILE/.local/state}"; do
    [ -n "$base" ] || continue
    any=1
    base=${base//\\//}
    if compgen -G "$base/ork-meter/read-guard/*.json" >/dev/null 2>&1; then found=0; break; fi
  done
  [ -n "$any" ] || return 0
  return $found
}
if [ "${1:-}" = "write-guard.py" ] && ! _ork_guard_records; then
  cat >/dev/null 2>&1
  exit 0
fi

# Keep hook dependencies separate from the project's Python environment: the
# private interpreter under $CLAUDE_PLUGIN_DATA/hook-python if there is one,
# else python3 from PATH (hooks/hook-python.sh).
hook_python=python3
hook_data_dir=
# The directory of $0 without the external dirname (an empty PATH must still
# report a missing python3): up to the last / or, on Windows, \.
case $0 in */* | *\\*) _ork_here=${0%[/\\]*} ;; *) _ork_here=. ;; esac
. "$_ork_here/hook-python.sh" 2>/dev/null || { hook_python=python3; hook_data_dir=; }

# Sets bytecode to the interpreter options for compiled bytecode: a private
# cache under the plugin's data directory, else -B (none). The cache
# directory is checked like hooks/lib/session.py's private_dir: created 0700,
# not a link, owned by this user, then set to 0700 if it isn't. The data
# directory must be this user's and neither group- nor other-writable. In the
# shell, so that no extra interpreter starts for it. On Windows (Git Bash, Cygwin) only a drive path is passed on, since
# a native Python can't read /c/... .
bytecode=(-B)
set_bytecode() {
  local data=${hook_data_dir:-} cache loose
  [ -n "$data" ] || return 0
  case "${OSTYPE:-}" in
    msys* | cygwin*) case "$data" in [A-Za-z]:[\\/]*) ;; *) return 0 ;; esac ;;
  esac
  [ -d "$data" ] && [ -O "$data" ] || return 0
  cache="${data%[/\\]}/pycache"
  if [ ! -e "$cache" ] && [ ! -L "$cache" ]; then
    mkdir -m 700 "$cache" 2>/dev/null   # another hook may win the race; checked below
  fi
  [ -d "$cache" ] && [ ! -L "$cache" ] && [ -O "$cache" ] || return 0
  # Prints the data directory if its group or others may write it; find
  # missing or failing means no cache.
  loose=$(find "$data/." -prune \( -perm -020 -o -perm -002 \) -print 2>/dev/null) || return 0
  [ -z "$loose" ] || return 0
  chmod 700 "$cache" 2>/dev/null || return 0
  bytecode=(-X "pycache_prefix=$cache")
}

run_script() {
  local here=$1 script=$2
  shift 2
  if ! command -v "$hook_python" >/dev/null 2>&1; then
    echo "ork-meter: python3 (3.10 or later) is needed on PATH to run $script" >&2
    exit 1
  fi
  local root
  root=$(cd -P "$here/.." && pwd -P) || exit 1
  set_bytecode
  exec "$hook_python" -I --check-hash-based-pycs always "${bytecode[@]}" "$root/$script" "$@"
}

main() {
  local here script
  here=$(cd -P "${_ork_here:-/}" && pwd -P) || exit 0
  script="$here/${1:?script}"
  [ -f "$script" ] || { cat >/dev/null 2>&1; exit 0; }

  if ! command -v "$hook_python" >/dev/null 2>&1; then
    cat >/dev/null 2>&1
    exit 0
  fi

  set_bytecode 2>/dev/null
  # The launch time goes to this one process only (run.py removes it from
  # its environment at once), never to scripts or anything else.
  ORK_HOOK_LAUNCHED_AT=$_ork_launched \
    "$hook_python" -I --check-hash-based-pycs always "${bytecode[@]}" "$here/lib/run.py" "$script" 2>/dev/null
  exit 0
}

# Directories are resolved physically (cd -P, pwd -P): a logical pwd would
# take the plugin root from how the caller spelled a symlinked directory, so
# `cd <dir linking to hooks> && bash py.sh --script ...` would run the
# scripts under that link's parent instead of the plugin's.
if [ "${1:-}" = "--script" ]; then
  [ -n "${2:-}" ] || { echo "usage: py.sh --script <path-under-the-plugin-root> [args...]" >&2; exit 2; }
  here=$(cd -P "${_ork_here:-/}" && pwd -P) || exit 1
  shift
  run_script "$here" "$@"
fi

main "$@" 2>/dev/null
exit 0
