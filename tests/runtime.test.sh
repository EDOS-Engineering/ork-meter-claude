#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
unset ORK_DEV_RUNTIME # each case below opts in explicitly
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fixture="$tmp/plugin with spaces"
mkdir -p "$fixture/scripts" "$fixture/.git"
cp scripts/runtime.sh "$fixture/scripts/runtime.sh"
case "$(uname -s)" in Darwin) os=darwin;; Linux) os=linux;; *) os=windows;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64;; *) arch=amd64;; esac
name=ork-meter-plugin-runtime
[ "$os" != windows ] || name="$name.exe"
package="$fixture/.local/runtime/$os-$arch"
data="$tmp/data with spaces"
mkdir -p "$package"
cat > "$package/$name" <<'SCRIPT'
#!/usr/bin/env bash
[ "$1" = status ] || exit 9
[ "$2" = --data-dir ] && [ "$3" = "$CLAUDE_PLUGIN_DATA" ] || exit 8
printf '%s\n' '{"state":"synthetic_status"}'
SCRIPT
chmod 700 "$package/$name"
digest() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d ' ' -f 1; else shasum -a 256 "$1" | cut -d ' ' -f 1; fi; }
digest "$package/$name" > "$package/SHA256"
export ORK_DEV_RUNTIME=1 # this fixture is a checkout running its .local build
output=$(CLAUDE_PLUGIN_DATA="$data" bash "$fixture/scripts/runtime.sh" status 2>/dev/null)
[ "$output" = '{"state":"synthetic_status"}' ]
echo '  ok   verified runtime installs and forwards exact arguments with spaces'
printf '\n# changed\n' >> "$package/$name"
if CLAUDE_PLUGIN_DATA="$data-fresh" bash "$fixture/scripts/runtime.sh" status >/dev/null 2>&1; then
  echo '  FAIL tampered bundled runtime accepted'; exit 1
fi
[ ! -e "$data-fresh/runtime/$(cat "$package/SHA256")/$name" ]
echo '  ok   tampered bundled runtime refused'
# The package is read only to install: a verified installed copy still runs,
# since it, not the package, is what runs and what matches SHA256.
output=$(CLAUDE_PLUGIN_DATA="$data" bash "$fixture/scripts/runtime.sh" status 2>/dev/null)
[ "$output" = '{"state":"synthetic_status"}' ]
echo '  ok   a tampered package does not stop the verified installed copy'
digest "$package/$name" > "$package/SHA256"
output=$(CLAUDE_PLUGIN_DATA="$data" bash "$fixture/scripts/runtime.sh" status 2>/dev/null)
[ "$output" = '{"state":"synthetic_status"}' ]
installed="$data/runtime/$(cat "$package/SHA256")/$name"
printf '\n# changed again\n' >> "$installed"
output=$(CLAUDE_PLUGIN_DATA="$data" bash "$fixture/scripts/runtime.sh" status 2>/dev/null) \
  || { echo '  FAIL damaged installed runtime was not repaired'; exit 1; }
[ "$output" = '{"state":"synthetic_status"}' ] && [ "$(digest "$installed")" = "$(cat "$package/SHA256")" ]
[ -z "$(find "$data/runtime" -name '.runtime-*')" ]
echo '  ok   damaged installed runtime is replaced from the verified package'
rm -f "$installed"
output=$(CLAUDE_PLUGIN_DATA="$data" bash "$fixture/scripts/runtime.sh" status 2>/dev/null)
[ "$output" = '{"state":"synthetic_status"}' ] && [ -f "$installed" ]
echo '  ok   removed installed runtime is reinstalled'
unset ORK_DEV_RUNTIME

# The stamp (#140): a warm run hashes nothing; a changed copy is hashed again.
stamped="$tmp/stamped"
mkdir -p "$stamped/scripts" "$tmp/shims"
cp scripts/runtime.sh "$stamped/scripts/runtime.sh"
mkdir -p "$stamped/runtime/$os-$arch"
cat > "$stamped/runtime/$os-$arch/$name" <<'SCRIPT'
#!/usr/bin/env bash
case "$1" in
  --version) : > "${0%/*}/.version-ran"; echo synthetic ;;
  status) printf '%s\n' '{"state":"stamped"}' ;;
  *) exit 9 ;;
esac
SCRIPT
chmod 700 "$stamped/runtime/$os-$arch/$name"
digest "$stamped/runtime/$os-$arch/$name" > "$stamped/runtime/$os-$arch/SHA256"
sha=$(cat "$stamped/runtime/$os-$arch/SHA256")
# Shims log every digest and copy runtime.sh makes, then run the real tool.
hasher=sha256sum
command -v sha256sum >/dev/null 2>&1 || hasher=shasum
for tool in "$hasher" cp; do
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "%s"\nexec "%s" "$@"\n' "$tmp/$tool.log" "$(command -v "$tool")" > "$tmp/shims/$tool"
  chmod 755 "$tmp/shims/$tool"
done
sdata="$tmp/stamped data"
mkdir "$sdata"; chmod 700 "$sdata" 2>/dev/null || true
srun() { # [runtime.sh options...]: status through the shims; logs reset first
  : > "$tmp/$hasher.log"; : > "$tmp/cp.log"
  PATH="$tmp/shims:$PATH" bash "$stamped/scripts/runtime.sh" --plugin-data "$sdata" "$@" status 2>"$tmp/stderr"
}
hashes() { wc -l < "$tmp/$hasher.log" | tr -d ' '; }
copies() { wc -l < "$tmp/cp.log" | tr -d ' '; }
sbin="$sdata/runtime/$sha/$name"
[ "$(srun)" = '{"state":"stamped"}' ] && [ "$(copies)" = 1 ] && [ -f "$sdata/runtime/$sha/.verified" ]
grep -q "^v1 $sha " "$sdata/runtime/$sha/.verified"
echo '  ok   an install writes a stamp of the verified copy'
[ "$(srun)" = '{"state":"stamped"}' ] && [ "$(hashes)" = 0 ] && [ "$(copies)" = 0 ]
echo '  ok   a warm run with a matching stamp hashes neither the package nor the copy'
sleep 1; touch "$sbin"
[ "$(srun)" = '{"state":"stamped"}' ] && [ "$(hashes)" = 1 ] && [ "$(copies)" = 0 ]
[ "$(srun)" = '{"state":"stamped"}' ] && [ "$(hashes)" = 0 ]
echo '  ok   a stamp mismatch (new mtime) re-hashes the copy once, then restamps it'
# Same size, same mtime, different bytes: only the ctime (and the digest) tell.
cp -p "$sbin" "$tmp/reference"
sed 's/synthetic/synthetiX/' "$tmp/reference" > "$tmp/tampered"
[ "$(wc -c < "$tmp/tampered")" = "$(wc -c < "$tmp/reference")" ]
sleep 1; cat "$tmp/tampered" > "$sbin"; touch -r "$tmp/reference" "$sbin"
[ "$(srun)" = '{"state":"stamped"}' ] && [ "$(copies)" = 1 ] && [ "$(digest "$sbin")" = "$sha" ]
echo '  ok   a same-size copy with its mtime put back is detected (ctime), re-hashed and replaced'
printf 'v1 %s forged\n' "$sha" > "$sdata/runtime/$sha/.verified"
[ "$(srun)" = '{"state":"stamped"}' ] && [ "$(hashes)" = 1 ]
echo '  ok   a stamp that does not describe the copy is not trusted'
case "${OSTYPE:-}" in
  msys* | cygwin*) echo '  skip a group-writable data dir (Git Bash maps no group write bits)' ;;
  *)
    srun >/dev/null && chmod g+w "$sdata"
    [ "$(srun)" = '{"state":"stamped"}' ] && [ "$(hashes)" = 1 ] && [ "$(srun)" = '{"state":"stamped"}' ] && [ "$(hashes)" = 1 ]
    chmod g-w "$sdata"
    echo '  ok   a data dir others may write is hashed every run, stamp or not' ;;
esac
dest="$sdata/runtime/$sha"
: > "$dest/.runtime-old"; : > "$dest/.stamp-old"; : > "$dest/.runtime-live"
touch -t 202001010000 "$dest/.runtime-old" "$dest/.stamp-old"
[ "$(srun)" = '{"state":"stamped"}' ]
[ ! -e "$dest/.runtime-old" ] && [ ! -e "$dest/.stamp-old" ] && [ -e "$dest/.runtime-live" ]
rm -f "$dest/.runtime-live"
echo '  ok   staging files older than 5 minutes are removed, a live one is kept'
rm -rf "$sdata/runtime"
status=0; out=$(srun --if-installed) || status=$?
[ "$status" = 75 ] && [ -z "$out" ] && [ ! -e "$sbin" ] && [ "$(copies)" = 0 ]
echo '  ok   --if-installed exits 75 without installing when nothing is installed'
printf '\n# changed\n' >> "$stamped/runtime/$os-$arch/$name"
status=0; srun --if-installed >/dev/null || status=$?
[ "$status" = 1 ] && grep -q 'runtime integrity check failed' "$tmp/stderr"
echo '  ok   --if-installed with a broken package fails (1), not 75'
digest "$stamped/runtime/$os-$arch/$name" > "$stamped/runtime/$os-$arch/SHA256"
sha=$(cat "$stamped/runtime/$os-$arch/SHA256"); sbin="$sdata/runtime/$sha/$name"; dest="$sdata/runtime/$sha"
out=$(srun --install)
[ -z "$out" ] && [ -x "$sbin" ] && [ -f "$dest/.version-ran" ] && [ ! -e "$sdata/runtime/.install-lock" ]
echo '  ok   --install installs, runs --version once, prints nothing and releases its lock'
rm -rf "$dest"; mkdir "$sdata/runtime/.install-lock"
srun --install >/dev/null && [ ! -e "$sbin" ] && [ "$(copies)" = 0 ]
echo '  ok   --install leaves it to the installer holding a live lock'
touch -t 202001010000 "$sdata/runtime/.install-lock"
srun --install >/dev/null && [ -x "$sbin" ] && [ ! -e "$sdata/runtime/.install-lock" ]
echo '  ok   a lock older than 10 minutes (a killed installer) is taken over'
cp "$sbin" "$tmp/good"; printf '\n# damaged\n' >> "$sbin"
srun --install >/dev/null && [ "$(digest "$sbin")" = "$sha" ] && [ "$(copies)" = 1 ]
echo '  ok   --install replaces a damaged copy'

# main is the release: a source checkout carries runtime/ and may also have a
# development build in .local/runtime. Which one runs, and that both are verified.
fake() { # <dir> <state>: a verified fake runtime that reports <state>
  mkdir -p "$1"
  printf '#!/usr/bin/env bash\n[ "$1" = status ] || exit 9\nprintf "%%s\\n" "{\\"state\\":\\"%s\\"}"\n' "$2" > "$1/$name"
  chmod 700 "$1/$name"
  digest "$1/$name" > "$1/SHA256"
}
run() { # <data suffix> <plugin root> [env...]: stdout of `status`, stderr in $tmp/stderr
  local suffix=$1 root=$2; shift 2
  env "$@" CLAUDE_PLUGIN_DATA="$tmp/data-$suffix" bash "$root/scripts/runtime.sh" status 2>"$tmp/stderr"
}
dev=ORK_DEV_RUNTIME=1
both="$tmp/checkout"
mkdir -p "$both/scripts" "$both/.git"
cp scripts/runtime.sh "$both/scripts/runtime.sh"
fake "$both/runtime/$os-$arch" released
fake "$both/.local/runtime/$os-$arch" development
[ "$(run a "$both")" = '{"state":"released"}' ]
grep -q 'ignoring the .local/runtime development build; set ORK_DEV_RUNTIME=1' "$tmp/stderr"
echo '  ok   a source checkout runs runtime/ and says why it ignores .local/runtime without opt-in'
[ "$(run a2 "$both" "$dev")" = '{"state":"development"}' ]
grep -q "using the development runtime in .*/.local/runtime/$os-$arch" "$tmp/stderr"
echo '  ok   ORK_DEV_RUNTIME=1 runs the .local/runtime build and says so on stderr'
printf '\n# changed\n' >> "$both/.local/runtime/$os-$arch/$name"
if out=$(run b "$both" "$dev"); then echo "  FAIL tampered development build accepted: $out"; exit 1; fi
grep -q 'runtime integrity check failed' "$tmp/stderr" && [[ "$out" != *released* ]]
echo '  ok   a tampered development build is refused, not swapped for runtime/'
rm "$both/.local/runtime/$os-$arch/$name"
if out=$(run c "$both" "$dev"); then echo "  FAIL empty development build accepted: $out"; exit 1; fi
grep -q 'missing its bundled runtime' "$tmp/stderr"
echo '  ok   an empty development build directory fails explicitly'
rm -rf "$both/.local"
if out=$(run c2 "$both" "$dev"); then echo "  FAIL opted in without a development build: $out"; exit 1; fi
grep -q 'missing its bundled runtime' "$tmp/stderr"
echo '  ok   opting in without a development build fails rather than running runtime/'
[ "$(run d "$both")" = '{"state":"released"}' ] && [ ! -s "$tmp/stderr" ]
echo '  ok   a source checkout with no development build uses runtime/ quietly'
installed="$tmp/installed"
mkdir -p "$installed/scripts"
cp scripts/runtime.sh "$installed/scripts/runtime.sh"
fake "$installed/runtime/$os-$arch" released
fake "$installed/.local/runtime/$os-$arch" development
[ "$(run e "$installed" "$dev")" = '{"state":"released"}' ] && [ ! -s "$tmp/stderr" ]
echo '  ok   an installed plugin (no .git) ignores .local/ and ORK_DEV_RUNTIME, and uses runtime/'
printf '\n# changed\n' >> "$installed/runtime/$os-$arch/$name"
if out=$(run f "$installed"); then echo "  FAIL tampered released runtime accepted: $out"; exit 1; fi
grep -q 'runtime integrity check failed' "$tmp/stderr"
echo '  ok   a tampered released runtime is refused'
rm -rf "$installed/runtime"
if out=$(run g "$installed"); then echo "  FAIL missing runtime accepted: $out"; exit 1; fi
grep -q 'missing its bundled runtime' "$tmp/stderr"
echo '  ok   no matching runtime fails explicitly'

# A platform with no released target is named as unsupported, not as a broken package.
mkdir -p "$tmp/bin"
printf '#!/usr/bin/env bash\ncase "$1" in -s) echo "$FAKE_UNAME_S" ;; -m) echo "$FAKE_UNAME_M" ;; esac\n' > "$tmp/bin/uname"
chmod 755 "$tmp/bin/uname"
fake "$installed/runtime/$os-$arch" released
for platform in 'MINGW64_NT-10.0-26100 aarch64 windows-arm64' 'FreeBSD amd64 freebsd-amd64' 'Linux riscv64 linux-riscv64'; do
  read -r uname_s uname_m expected <<< "$platform"
  if out=$(run "h-$expected" "$installed" PATH="$tmp/bin:$PATH" FAKE_UNAME_S="$uname_s" FAKE_UNAME_M="$uname_m"); then
    echo "  FAIL $expected accepted: $out"; exit 1
  fi
  grep -q "unsupported platform $expected" "$tmp/stderr" && ! grep -q 'missing its bundled runtime' "$tmp/stderr"
  echo "  ok   $expected is reported as an unsupported platform"
done
