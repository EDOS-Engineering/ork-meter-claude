#!/usr/bin/env bash
# scripts/package_runtime.py builds the release layout scripts/runtime.sh accepts.
# Needs a Go toolchain; skips (exit 0) with a message when `go` is not on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v go >/dev/null 2>&1; then
  echo '  skip package_runtime: go is not on PATH'; exit 0
fi
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fails=0
ok() { echo "  ok   $1"; }
fail() { echo "  FAIL $1"; fails=$((fails + 1)); }

# A fake platform source: the real runtime's layout, a stand-in program.
source="$tmp/platform source"
mkdir -p "$source/cmd/claude-plugin-runtime"
printf 'module example.invalid/fake-platform\n\ngo 1.21\n' > "$source/go.mod"
cat > "$source/cmd/claude-plugin-runtime/main.go" <<'GO'
package main

import (
	"fmt"
	"os"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Println("fake-plugin-runtime 0.0.0")
		return
	}
	if len(os.Args) == 4 && os.Args[1] == "status" && os.Args[2] == "--data-dir" && os.Args[3] == os.Getenv("CLAUDE_PLUGIN_DATA") {
		fmt.Println(`{"state":"synthetic_status"}`)
		return
	}
	os.Exit(9)
}
GO

case "$(uname -s)" in Darwin) os=darwin;; Linux) os=linux;; *) os=windows;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64;; *) arch=amd64;; esac
name=ork-meter-plugin-runtime
[ "$os" != windows ] || name="$name.exe"
digest() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d ' ' -f 1; else shasum -a 256 "$1" | cut -d ' ' -f 1; fi; }

# A release-shaped plugin tree: no .git, so runtime.sh must use runtime/, not .local/.
plugin="$tmp/plugin with spaces"
mkdir -p "$plugin/scripts" "$plugin/.claude-plugin"
cp scripts/runtime.sh "$plugin/scripts/runtime.sh"
cp .claude-plugin/plugin.json "$plugin/.claude-plugin/plugin.json"

# Refusals come before any build.
if python3 scripts/package_runtime.py --platform-source "$tmp" --targets "$os-$arch" --output "$plugin/runtime" >/dev/null 2>&1; then
  fail 'source without cmd/claude-plugin-runtime/main.go refused'
else ok 'source without cmd/claude-plugin-runtime/main.go refused'; fi
if python3 scripts/package_runtime.py --platform-source "$source" --targets "$os-$arch" --output "$plugin/runtime" --plugin-version 1.2 >/dev/null 2>&1; then
  fail 'non-semver --plugin-version refused'
else ok 'non-semver --plugin-version refused'; fi
if python3 scripts/package_runtime.py --platform-source "$source" --targets "freebsd-amd64" --output "$plugin/runtime" >/dev/null 2>&1; then
  fail 'unsupported target refused'
else ok 'unsupported target refused'; fi
[ ! -e "$plugin/runtime" ] && ok 'refusals wrote nothing' || fail 'refusals wrote nothing'

# Build the host target plus windows-amd64 (cross-compiled, checks the .exe name).
out=$(python3 scripts/package_runtime.py --platform-source "$source" --targets "$os-$arch" windows-amd64 \
        --output "$plugin/runtime" --plugin-version 9.9.9 --manifest "$plugin/.claude-plugin/plugin.json")
package="$plugin/runtime/$os-$arch"
[ -f "$package/$name" ] && ok "binary at runtime/$os-$arch/$name" || fail "binary at runtime/$os-$arch/$name"
[ -x "$package/$name" ] && ok 'binary is executable' || fail 'binary is executable'
[ -f "$plugin/runtime/windows-amd64/ork-meter-plugin-runtime.exe" ] && ok 'windows binary carries .exe' || fail 'windows binary carries .exe'
[ "$(ls -A "$package" | sort | tr '\n' ' ')" = "SHA256 $name " ] && ok 'target directory holds only the binary and SHA256' || fail "target directory holds only the binary and SHA256: $(ls -A "$package" | tr '\n' ' ')"
grep -q '^Go standard library: LICENSE$' "$plugin/runtime/THIRD_PARTY_NOTICES.txt" && grep -q "^  $os-$arch: " "$plugin/runtime/THIRD_PARTY_NOTICES.txt" \
  && ok 'THIRD_PARTY_NOTICES.txt names the Go licence and each target' || fail 'THIRD_PARTY_NOTICES.txt names the Go licence and each target'
[[ "$out" == *"THIRD_PARTY_NOTICES.txt: 0 Go module(s)"* ]] && ok 'builder reports the notices' || fail "builder reports the notices: $out"

expected=$(digest "$package/$name")
manifest=$(cat "$package/SHA256")
[[ "$manifest" =~ ^[a-f0-9]{64}$ ]] && ok 'SHA256 is 64 lowercase hex' || fail "SHA256 is 64 lowercase hex: $manifest"
[ "$(wc -c < "$package/SHA256" | tr -d ' ')" = 65 ] && [ "$(tail -c 1 "$package/SHA256" | od -An -c | tr -d ' ')" = '\n' ] \
  && ok 'SHA256 is digest plus one newline' || fail 'SHA256 is digest plus one newline'
[ "$manifest" = "$expected" ] && ok 'SHA256 matches the binary' || fail 'SHA256 matches the binary'
[[ "$out" == *"$os-$arch  $expected"* ]] && ok 'builder prints each target and digest' || fail "builder prints each target and digest: $out"
[ "$("$package/$name" --version)" = 'fake-plugin-runtime 0.0.0' ] && ok 'built binary runs on the host' || fail 'built binary runs on the host'

version=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$plugin/.claude-plugin/plugin.json")
[ "$version" = 9.9.9 ] && ok 'plugin.json version rewritten' || fail "plugin.json version rewritten: $version"
[ "$(wc -l < "$plugin/.claude-plugin/plugin.json")" = "$(wc -l < .claude-plugin/plugin.json)" ] \
  && ok 'plugin.json formatting kept (same line count)' || fail 'plugin.json formatting kept (same line count)'
[ "$(diff .claude-plugin/plugin.json "$plugin/.claude-plugin/plugin.json" | grep -c '^[<>]')" = 2 ] \
  && ok 'plugin.json differs only on the version line' || fail 'plugin.json differs only on the version line'

# runtime.sh accepts the packaged layout: verifies, installs, execs with the data dir.
data="$tmp/data with spaces"
output=$(CLAUDE_PLUGIN_DATA="$data" bash "$plugin/scripts/runtime.sh" status)
[ "$output" = '{"state":"synthetic_status"}' ] && ok 'runtime.sh runs the packaged runtime' || fail "runtime.sh runs the packaged runtime: $output"
[ -f "$data/runtime/$expected/$name" ] && ok 'runtime.sh installed it under its digest' || fail 'runtime.sh installed it under its digest'
# A source checkout without a development build uses the packaged runtime/.
mkdir -p "$plugin/.git"
output=$(CLAUDE_PLUGIN_DATA="$data" bash "$plugin/scripts/runtime.sh" status)
[ "$output" = '{"state":"synthetic_status"}' ] && ok 'a source checkout with no .local/ build uses runtime/' || fail 'a source checkout with no .local/ build uses runtime/'

# Rebuilding replaces stale files in the target directory.
touch "$package/stale"
python3 scripts/package_runtime.py --platform-source "$source" --targets "$os-$arch" --output "$plugin/runtime" >/dev/null
[ ! -e "$package/stale" ] && ok 'rebuild clears stale files' || fail 'rebuild clears stale files'

# The development builder is package_runtime.build() for the host, into
# .local/runtime, which runtime.sh uses with ORK_DEV_RUNTIME=1.
mkdir -p "$plugin/.github/scripts"
cp scripts/build_runtime.py scripts/package_runtime.py "$plugin/scripts/"
cp .github/scripts/release_targets.py "$plugin/.github/scripts/"
out=$(python3 -I "$plugin/scripts/build_runtime.py" --platform-source "$source")
dev="$plugin/.local/runtime/$os-$arch"
[ "$(cat "$dev/SHA256")" = "$(digest "$dev/$name")" ] && [[ "$out" == *"SHA-256 $(cat "$dev/SHA256")"* ]] \
  && ok 'build_runtime.py builds the host target into .local/runtime' || fail "build_runtime.py builds the host target into .local/runtime: $out"
cmp -s "$dev/$name" "$package/$name" && ok 'a development build is the release build' || fail 'a development build is the release build'
output=$(ORK_DEV_RUNTIME=1 CLAUDE_PLUGIN_DATA="$data" bash "$plugin/scripts/runtime.sh" status 2>/dev/null)
[ "$output" = '{"state":"synthetic_status"}' ] && ok 'runtime.sh runs the development build' || fail "runtime.sh runs the development build: $output"

[ "$fails" -eq 0 ]
