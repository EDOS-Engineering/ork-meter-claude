#!/usr/bin/env bash
# Prepares ci.yml's `wsl` job (#30) inside a WSL Ubuntu distribution, as the
# unprivileged user the job runs its steps as:
#
#   bash wsl-setup.sh <checkout> <claude version or dist-tag>
#
# It clones the Windows-side checkout into the distribution's own filesystem
# (a working tree under /mnt has Windows permissions and is slow, and WSL users
# keep their projects in Linux), installs Node 22 from nodejs.org (checked
# against its SHASUMS256.txt), Claude Code from npm with the same retries as
# .github/actions/install-claude, and the pinned tokenizer in a venv (Ubuntu's
# Python refuses a global pip install). It writes ~/ci-env.sh, which every
# later step sources: the wsl-bash wrapper starts a bash with no profile, and
# Windows environment variables do not cross into WSL.
set -euo pipefail
checkout=$1
spec=$2

git config --global --add safe.directory '*'
git clone --quiet --no-hardlinks "$checkout" "$HOME/plugin"
git -C "$HOME/plugin" log -1 --format='Testing %H'

arch=$(uname -m)
case "$arch" in
  x86_64) node_arch=x64 ;;
  aarch64) node_arch=arm64 ;;
  *) echo "::error::unsupported WSL architecture $arch"; exit 1 ;;
esac
dist=https://nodejs.org/dist/latest-v22.x
tmp=$(mktemp -d)
curl -fsSL --retry 3 "$dist/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
tarball=$(grep -oE "node-v22\.[0-9]+\.[0-9]+-linux-$node_arch\.tar\.xz$" "$tmp/SHASUMS256.txt")
curl -fsSL --retry 3 "$dist/$tarball" -o "$tmp/$tarball"
(cd "$tmp" && grep " $tarball\$" SHASUMS256.txt | sha256sum -c -)
mkdir -p "$HOME/.local/node"
tar -xJf "$tmp/$tarball" -C "$HOME/.local/node" --strip-components=1
rm -rf "$tmp"

cat > "$HOME/ci-env.sh" <<'EOF'
export PATH="$HOME/venv/bin:$HOME/.npm-global/bin:$HOME/.local/node/bin:$PATH"
export DISABLE_AUTOUPDATER=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CI=true
cd "$HOME/plugin"
EOF
# shellcheck source=/dev/null
. "$HOME/ci-env.sh"
npm config set prefix "$HOME/.npm-global"

python3 -m venv "$HOME/venv"
hash -r # python3 now resolves to the venv's, which later steps use too
python -m pip install --quiet --require-hashes --only-binary=:all: -r requirements-hooks.txt
echo "$(python3 --version) at $(command -v python3); Node $(node --version)"

for attempt in 1 2 3; do
  if version=$(npm view "@anthropic-ai/claude-code@$spec" version) && [ -n "$version" ]; then break; fi
  [ "$attempt" -lt 3 ] || { echo "::error::cannot resolve @anthropic-ai/claude-code@$spec"; exit 1; }
  sleep $((attempt * 10))
done
case "$version" in *[[:space:]]*) echo "::error::$spec resolves to more than one version"; exit 1 ;; esac
for attempt in 1 2 3; do
  if npm install --global --no-fund --no-audit "@anthropic-ai/claude-code@$version" \
      && reported=$(claude --version) && printf '%s\n' "$reported" | grep -qF "$version"; then
    echo "$reported in WSL ($(uname -r))"
    exit 0
  fi
  echo "::warning::Claude Code $version install attempt $attempt failed"
  npm uninstall --global --no-fund --no-audit @anthropic-ai/claude-code || true
  [ "$attempt" -lt 3 ] && sleep $((attempt * 10))
done
echo "::error::Claude Code $version did not install after 3 attempts"
exit 1
