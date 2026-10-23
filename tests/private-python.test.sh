#!/usr/bin/env bash
# Hook dependencies must not require replacing the model's project Python PATH.
set -euo pipefail
# macOS bash 3.2 doesn't stop on a failed [[ ]] under set -e, so every check
# below exits explicitly.
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/plugin data/hook-python/bin"
printf '#!/bin/sh\nprintf "fallback:%%s\\n" "$*"\n' > "$tmp/bin/python3"
printf '#!/bin/sh\nprintf "private:%%s\\n" "$*"\n' > "$tmp/plugin data/hook-python/bin/python3"
chmod +x "$tmp/bin/python3" "$tmp/plugin data/hook-python/bin/python3"
original_path=$PATH
export PATH="$tmp/bin:$PATH"

out=$(CLAUDE_PLUGIN_DATA="$tmp/plugin data" bash hooks/py.sh --script scripts/savings.py --help)
[[ "$out" == private:"-I --check-hash-based-pycs always -X pycache_prefix=$tmp/plugin data/pycache "*scripts/savings.py' --help' ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
out=$(CLAUDE_PLUGIN_DATA="$tmp/plugin data" bash hooks/py.sh loop-guard.py </dev/null)
[[ "$out" == private:"-I --check-hash-based-pycs always -X pycache_prefix=$tmp/plugin data/pycache "*hooks/lib/run.py*loop-guard.py ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
out=$(CLAUDE_PLUGIN_DATA=relative bash hooks/py.sh --script scripts/savings.py)
[[ "$out" == fallback:'-I --check-hash-based-pycs always -B '* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
out=$(CLAUDE_PLUGIN_DATA="$tmp/missing" bash hooks/py.sh --script scripts/savings.py)
[[ "$out" == fallback:'-I --check-hash-based-pycs always -B '* && ! -e "$tmp/missing" ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
[[ "$PATH" == "$tmp/bin:$original_path" ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
# The startup floor check must use the same private interpreter.
printf '#!/bin/sh\nexit 0\n' > "$tmp/plugin data/hook-python/bin/python3"
printf '#!/bin/sh\nexit 127\n' > "$tmp/bin/python3"
out=$(CLAUDE_PLUGIN_DATA="$tmp/plugin data" bash hooks/python-check.sh </dev/null)
[[ -z "$out" ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
echo '  ok   private hook Python, script path, hook path, fallback and startup check'

# Windows (#131 item 4): a venv keeps its interpreter at Scripts\python.exe,
# and CLAUDE_PLUGIN_DATA may be a drive path. Drive paths are exercised here as
# directories literally named C:\... and C:/... under a scratch working
# directory, which is how this POSIX runner sees them; only the path checks
# are tested, not a real Windows Git Bash.
plugin=$PWD
mkdir -p "$tmp/win data/hook-python/Scripts" "$tmp/cwd/C:\\data/hook-python/Scripts" "$tmp/cwd/D:/data/hook-python/Scripts"
for exe in "$tmp/win data/hook-python/Scripts/python.exe" "$tmp/cwd/C:\\data/hook-python/Scripts/python.exe" \
           "$tmp/cwd/D:/data/hook-python/Scripts/python.exe"; do
  printf '#!/bin/sh\nprintf "windows:%%s\\n" "$*"\n' > "$exe"
  chmod +x "$exe"
done
printf '#!/bin/sh\nprintf "fallback:%%s\\n" "$*"\n' > "$tmp/bin/python3"
out=$(CLAUDE_PLUGIN_DATA="$tmp/win data" bash hooks/py.sh --script scripts/savings.py)
[[ "$out" == windows:"-I --check-hash-based-pycs always -X pycache_prefix=$tmp/win data/pycache "*scripts/savings.py ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
out=$(cd "$tmp/cwd" && OSTYPE=msys CLAUDE_PLUGIN_DATA='C:\data' bash "$plugin/hooks/py.sh" --script scripts/savings.py)
[[ "$out" == windows:'-I --check-hash-based-pycs always -X pycache_prefix=C:\data/pycache '* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
out=$(cd "$tmp/cwd" && OSTYPE=cygwin CLAUDE_PLUGIN_DATA='D:/data' bash "$plugin/hooks/py.sh" --script scripts/savings.py)
[[ "$out" == windows:* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
# Outside Windows a drive-like value is a relative name: the cwd must not choose.
out=$(cd "$tmp/cwd" && OSTYPE=darwin CLAUDE_PLUGIN_DATA='C:\data' bash "$plugin/hooks/py.sh" --script scripts/savings.py)
[[ "$out" == fallback:* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
# A drive-relative or plain relative path is not trusted.
mkdir -p "$tmp/cwd/D:data/hook-python/Scripts"
cp "$tmp/win data/hook-python/Scripts/python.exe" "$tmp/cwd/D:data/hook-python/Scripts/"
out=$(cd "$tmp/cwd" && CLAUDE_PLUGIN_DATA='D:data' bash "$plugin/hooks/py.sh" --script scripts/savings.py)
[[ "$out" == fallback:* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
# A directory named python.exe is not an interpreter.
mkdir -p "$tmp/dir exe/hook-python/Scripts/python.exe"
out=$(CLAUDE_PLUGIN_DATA="$tmp/dir exe" bash hooks/py.sh --script scripts/savings.py)
[[ "$out" == fallback:* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
# The startup check uses the same resolution.
printf '#!/bin/sh\nexit 0\n' > "$tmp/cwd/C:\\data/hook-python/Scripts/python.exe"
printf '#!/bin/sh\nexit 127\n' > "$tmp/bin/python3"
out=$(cd "$tmp/cwd" && OSTYPE=msys CLAUDE_PLUGIN_DATA='C:\data' bash "$plugin/hooks/python-check.sh" </dev/null)
[[ -z "$out" ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
echo '  ok   Windows venv layout and drive-letter plugin data (path checks only)'

# Compiled bytecode (#189): cached under $CLAUDE_PLUGIN_DATA/pycache, owner-only,
# when that is an absolute path; otherwise, or when the cache directory is
# unsafe, nothing is cached (-B), as before.
mode() { PATH=$original_path python3 -I -c 'import os, stat, sys; print(format(stat.S_IMODE(os.lstat(sys.argv[1]).st_mode), "o"))' "$1"; }
printf '#!/bin/sh\nprintf "fallback:%%s\\n" "$*"\n' > "$tmp/bin/python3"
data="$tmp/cache data"
mkdir -p "$data"
out=$(CLAUDE_PLUGIN_DATA="$data" bash hooks/py.sh loop-guard.py </dev/null)
[[ "$out" == fallback:"-I --check-hash-based-pycs always -X pycache_prefix=$data/pycache "*hooks/lib/run.py* && -d "$data/pycache" ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
[[ "$(mode "$data/pycache")" == 700 ]] || { echo "  FAIL line $LINENO: $(mode "$data/pycache")"; exit 1; }
chmod 755 "$data/pycache"
out=$(CLAUDE_PLUGIN_DATA="$data/" bash hooks/py.sh --script scripts/savings.py)
[[ "$out" == fallback:"-I --check-hash-based-pycs always -X pycache_prefix=$data/pycache "* && "$(mode "$data/pycache")" == 700 ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
# Git Bash's /c/... form is not passed to a native Windows Python.
out=$(OSTYPE=msys CLAUDE_PLUGIN_DATA="$data" bash hooks/py.sh --script scripts/savings.py)
[[ "$out" == fallback:'-I --check-hash-based-pycs always -B '* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
# Not a directory there: refused, nothing cached.
mkdir -p "$tmp/file data" && : > "$tmp/file data/pycache"
out=$(CLAUDE_PLUGIN_DATA="$tmp/file data" bash hooks/py.sh loop-guard.py </dev/null)
[[ "$out" == fallback:'-I --check-hash-based-pycs always -B '* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
# The data directory itself must be this user's and not writable by others.
for loose in 770 707; do
  chmod "$loose" "$data"
  out=$(CLAUDE_PLUGIN_DATA="$data" bash hooks/py.sh loop-guard.py </dev/null)
  [[ "$out" == fallback:'-I --check-hash-based-pycs always -B '* ]] || { echo "  FAIL line $LINENO ($loose): $out"; exit 1; }
done
chmod 755 "$data"
out=$(CLAUDE_PLUGIN_DATA="$data" bash hooks/py.sh loop-guard.py </dev/null)
[[ "$out" == fallback:"-I --check-hash-based-pycs always -X pycache_prefix=$data/pycache "* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
echo '  ok   bytecode cache under the plugin data directory, 0700; -B when relative, missing, not a directory, or the data directory is group- or other-writable'

# A link at the cache path is refused, even to a private directory.
mkdir -p "$tmp/link data" "$tmp/elsewhere"
chmod 700 "$tmp/elsewhere"
if ln -s "$tmp/elsewhere" "$tmp/link data/pycache" 2>/dev/null && [ -L "$tmp/link data/pycache" ]; then
  out=$(CLAUDE_PLUGIN_DATA="$tmp/link data" bash hooks/py.sh loop-guard.py </dev/null)
  [[ "$out" == fallback:'-I --check-hash-based-pycs always -B '* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
  echo '  ok   a symlinked bytecode cache directory is refused (-B)'
else
  echo '  skip symlinked bytecode cache: symlinks unsupported here'
fi

# With a real interpreter, on a copy of the plugin: a hook's lib bytecode
# lands in the cache, never beside the source.
export PATH=$original_path
mkdir -p "$tmp/copy" "$tmp/real data"
cp -R hooks .claude-plugin scripts "$tmp/copy/"
rm -rf "$tmp/copy/hooks/lib/__pycache__"
printf '{"hook_event_name":"PostToolUseFailure","session_id":"cache-1","tool_name":"Bash","tool_input":{"command":"x"},"error":"e","scratchpad_dir":"%s"}' "$tmp/scratch" \
  | HOME="$tmp" XDG_STATE_HOME="$tmp/state" CLAUDE_PLUGIN_DATA="$tmp/real data" bash "$tmp/copy/hooks/py.sh" loop-guard.py >/dev/null
lib=$(cd -P "$tmp/copy/hooks/lib" && pwd -P)
ls "$tmp/real data/pycache$lib"/hook.*.pyc >/dev/null 2>&1 || { echo "  FAIL line $LINENO: no cached hook bytecode"; find "$tmp/real data/pycache" | head; exit 1; }
[ ! -e "$tmp/copy/hooks/lib/__pycache__" ] || { echo "  FAIL line $LINENO: bytecode written beside the source"; exit 1; }
echo '  ok   a real hook caches hooks/lib bytecode under the plugin data directory only'

# A planted unchecked-hash .pyc (whose code differs from its source) is never
# run: py.sh passes --check-hash-based-pycs always, so the source wins.
mkdir -p "$tmp/plant/project" "$tmp/plant/scratch"
plant_data="$tmp/plant/data"
mkdir -m 700 "$plant_data"
python3 -I tests/plus_policy_sign.py "$plant_data"   # a signed Plus envelope, so the hook runs its handler
python3 -I -B - "$tmp/copy/hooks/lib/hook.py" "$plant_data/pycache" "$tmp/plant" <<'EOF' || { echo "  FAIL line $LINENO: could not plant"; exit 1; }
import os, py_compile, subprocess, sys
source, prefix, work = sys.argv[1:]
source = os.path.realpath(source)   # py.sh resolves the plugin root physically
os.makedirs(prefix, mode=0o700, exist_ok=True)
cfile = subprocess.run([sys.executable, "-I", "-X", "pycache_prefix=" + prefix, "-c",
                        "import importlib.util, sys; print(importlib.util.cache_from_source(sys.argv[1]))", source],
                       capture_output=True, text=True, check=True).stdout.strip()
fake = os.path.join(work, "planted.py")
with open(fake, "w") as stream:
    stream.write("import sys\nsys.stdout.write('PLANTED\\n')\nsys.stdout.flush()\n")
py_compile.compile(fake, cfile=cfile, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH, doraise=True)
# The probe is real: without the option, Python runs the planted code.
out = subprocess.run([sys.executable, "-I", "-X", "pycache_prefix=" + prefix, "-c",
                      "import sys; sys.path.insert(0, sys.argv[1]); import hook", os.path.dirname(source)],
                     capture_output=True, text=True).stdout
assert "PLANTED" in out, out
EOF
python3 -I -B -c 'import sys
with open(sys.argv[1], "w") as f:
    for i in range(3000): f.write("line %05d: some text to page past the guard\n" % i)' "$tmp/plant/project/big.txt"
printf '{"hook_event_name":"PreToolUse","session_id":"plant-1","tool_name":"Read","tool_use_id":"t1","tool_input":{"file_path":"%s"},"scratchpad_dir":"%s","cwd":"%s"}' \
  "$tmp/plant/project/big.txt" "$tmp/plant/scratch" "$tmp/plant/project" > "$tmp/plant/payload.json"
out=$(HOME="$tmp/plant" XDG_STATE_HOME="$tmp/plant/state" CLAUDE_PLUGIN_DATA="$plant_data" bash "$tmp/copy/hooks/py.sh" read-guard.py < "$tmp/plant/payload.json")
[[ "$out" != *PLANTED* && "$out" == *updatedInput* ]] || { echo "  FAIL line $LINENO: $out"; exit 1; }
echo '  ok   a planted unchecked-hash .pyc is rejected; the hook runs from its source'

