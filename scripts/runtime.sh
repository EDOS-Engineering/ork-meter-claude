#!/usr/bin/env bash
# Verify/install this plugin's internal runtime. Never invoke legacy ork-meter.
#
# Usage: runtime.sh [--plugin-data <dir>] [--if-installed | --install] <command> [args...]
#   (default)       install the verified runtime if needed, then exec <command>.
#   --if-installed  exit 75 instead of installing when the installed copy is
#                   missing or damaged (after checking the package, so a
#                   broken package still fails with 1); hooks/session-start.sh
#                   then starts --install detached rather than wait for it.
#   --install       install (or repair) only, under a lock so that concurrent
#                   sessions install once, then run the new copy once with
#                   --version, so that a first-exec scan (Windows Defender)
#                   happens here and not in the next session's hook.
set -euo pipefail
data=${CLAUDE_PLUGIN_DATA:-}
mode=run
while [ "$#" -gt 0 ]; do
  case "$1" in
    --plugin-data) [ "$#" -ge 2 ] || exit 1; data=$2; shift 2 ;;
    --if-installed) mode=if-installed; shift ;;
    --install) mode=install; shift ;;
    *) break ;;
  esac
done
# Detached by hooks/session-start.sh, which waits until this closes stdout.
[ "$mode" != install ] || exec >/dev/null 2>&1
root=$(cd "$(dirname "$0")/.." && pwd)
case "$(uname -s)" in
  Darwin) os=darwin ;;
  Linux) os=linux ;;
  MINGW*|MSYS*|CYGWIN*) os=windows ;;
  *) os=$(uname -s | tr '[:upper:]' '[:lower:]') ;;
esac
case "$(uname -m)" in
  x86_64|amd64) arch=amd64 ;;
  arm64|aarch64) arch=arm64 ;;
  *) arch=$(uname -m | tr '[:upper:]' '[:lower:]') ;;
esac
# The targets scripts/package_runtime.py builds and a release carries.
case "$os-$arch" in
  darwin-amd64|darwin-arm64|linux-amd64|linux-arm64|windows-amd64) ;;
  *) echo "Ork Meter: unsupported platform $os-$arch." >&2; exit 1 ;;
esac
name=ork-meter-plugin-runtime
[ "$os" != windows ] || name="$name.exe"
package="$root/runtime/$os-$arch"
# main is the release, so a source checkout also carries the released
# runtime/. A developer opts in to their own build in .local/runtime (from
# scripts/build_runtime.py) with ORK_DEV_RUNTIME=1, and is told on stderr
# whenever it runs; a broken or missing development build fails below rather
# than falling back. An installed plugin has no .git and always uses runtime/.
if [ -e "$root/.git" ]; then
  if [ "${ORK_DEV_RUNTIME:-}" = 1 ]; then
    package="$root/.local/runtime/$os-$arch"
    echo "Ork Meter: using the development runtime in $package (ORK_DEV_RUNTIME=1)." >&2
  elif [ -d "$root/.local/runtime/$os-$arch" ]; then
    echo 'Ork Meter: ignoring the .local/runtime development build; set ORK_DEV_RUNTIME=1 to use it.' >&2
  fi
fi
if [ ! -f "$package/$name" ] || [ ! -f "$package/SHA256" ]; then
  echo 'Ork Meter: this plugin build is missing its bundled runtime. Install a complete plugin package.' >&2; exit 1
fi
expected=$(< "$package/SHA256")
[[ "$expected" =~ ^[a-f0-9]{64}$ ]] || { echo 'Ork Meter: invalid runtime manifest.' >&2; exit 1; }
digest() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum < "$1" | cut -d ' ' -f 1
  else shasum -a 256 < "$1" | cut -d ' ' -f 1; fi
}
package_ok() {
  [ "$(digest "$package/$name")" = "$expected" ] || { echo 'Ork Meter: runtime integrity check failed.' >&2; return 1; }
}
[ -n "$data" ] || { echo 'Ork Meter: persistent plugin data directory is unavailable.' >&2; exit 1; }
destination="$data/runtime/$expected"
binary="$destination/$name"
stamp="$destination/.verified"
staging=
locked=
lock="$data/runtime/.install-lock"
cleanup() {
  [ -z "$staging" ] || rm -f "$staging"
  [ -z "$locked" ] || rmdir "$lock" 2>/dev/null || true
}
trap cleanup EXIT

# "<uid> <mode> <inode> <size> <mtime> <ctime>" per path, mtime and ctime to
# the nanosecond (GNU stat, Git Bash's too; macOS's own /usr/bin/stat, never a
# GNU stat earlier on PATH, whose -f means something else).
stats() {
  case "$os" in
    darwin) /usr/bin/stat -f '%u %Lp %i %z %Fm %Fc' -- "$@" ;;
    *) stat -c '%u %a %i %s %y %z' -- "$@" ;;
  esac 2>/dev/null
}
# The stamp records the verified digest and the installed file's inode, size,
# mtime and ctime. Any write, chmod or replacement moves the ctime to the time
# of the change, and only root can set a ctime, so a changed copy no longer
# matches and is hashed again. The stamp is trusted only if this user owns,
# and nobody else may write, the data directory, runtime/, the digest
# directory, the stamp and the binary: then only this user and root could
# rewrite the stamp, and either can already change the plugin and its hooks.
# Anything else, or any doubt, means hashing as before.
write_stamp() { # <identity>
  local temporary
  [ "$(cat "$stamp" 2>/dev/null)" != "v1 $expected $1" ] || return 0
  temporary=$(mktemp "$destination/.stamp-XXXXXX" 2>/dev/null) || return 0
  { printf 'v1 %s %s\n' "$expected" "$1" > "$temporary" && mv -f "$temporary" "$stamp"; } 2>/dev/null \
    || rm -f "$temporary"
}
stamped() {
  local recorded lines uid mode rest identity= count=0
  [ -f "$stamp" ] && [ ! -L "$stamp" ] && [ ! -L "$binary" ] || return 1
  recorded=$(< "$stamp") || return 1
  lines=$(stats "$data/." "$data/runtime/." "$destination/." "$stamp" "$binary") || return 1
  while read -r uid mode rest; do
    [ "$uid" = "$UID" ] && [[ "$mode" =~ ^[0-7]+$ ]] && (( (8#$mode & 8#022) == 0 )) || return 1
    identity=$rest count=$((count + 1))
  done <<< "$lines"
  [ "$count" -eq 5 ] && [ "$recorded" = "v1 $expected $identity" ]
}
# 0 when the installed copy is the verified runtime: by its stamp, else by
# hashing it (and stamping what was hashed, unchanged meanwhile).
verified() {
  local before after
  [ -f "$binary" ] || return 1
  ! stamped || return 0
  before=$(stats "$binary") || before=
  [ "$(digest "$binary")" = "$expected" ] || return 1
  after=$(stats "$binary") || after=
  if [ -n "$before" ] && [ "$before" = "$after" ]; then write_stamp "${before#* * }"; fi
  return 0
}
# A staging file outlives an install killed between cp and mv; one older than
# 5 minutes is no live install's. The glob costs nothing when there is none.
clean_staging() {
  local leftover
  for leftover in "$destination"/.runtime-* "$destination"/.stamp-*; do
    [ -e "$leftover" ] || continue
    find "$destination" -maxdepth 1 -type f \( -name '.runtime-*' -o -name '.stamp-*' \) -mmin +5 \
      -exec rm -f {} + 2>/dev/null || true
    return 0
  done
}
# (Re)install from the verified package: a copy damaged after install
# (quarantine, partial restore) is replaced. Staging plus mv keeps concurrent
# sessions from seeing a half-written file.
install() {
  local identity
  package_ok || exit 1
  (umask 077 && mkdir -p "$destination")
  clean_staging
  staging=$(mktemp "$destination/.runtime-XXXXXX")
  cp "$package/$name" "$staging"
  chmod 700 "$staging"
  [ "$(digest "$staging")" = "$expected" ] || { echo 'Ork Meter: installed runtime integrity check failed.' >&2; exit 1; }
  mv -f "$staging" "$binary"
  staging=
  if identity=$(stats "$binary"); then write_stamp "${identity#* * }"; fi
}

[ ! -d "$destination" ] || clean_staging
case "$mode" in
  if-installed)
    verified || { package_ok || exit 1; exit 75; } ;;
  install)
    (umask 077 && mkdir -p "$data/runtime")
    # One installer at a time; a lock older than 10 minutes is a killed one's.
    if ! mkdir "$lock" 2>/dev/null; then
      [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2>/dev/null)" ] && rmdir "$lock" 2>/dev/null \
        && mkdir "$lock" 2>/dev/null || exit 0
    fi
    locked=1
    verified || install
    "$binary" --version >/dev/null 2>&1 || true
    exit 0 ;;
  *)
    verified || install ;;
esac
trap - EXIT
exec "$binary" "$@" --data-dir "$data"
