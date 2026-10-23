"""Build the plugin's bundled runtime for release packaging.

Compiles ork-meter's cmd/claude-plugin-runtime for each target into
<output>/<os>-<arch>/ork-meter-plugin-runtime (.exe on Windows) with a sibling
SHA256 file holding the binary's hex digest and a newline, the layout that
scripts/runtime.sh verifies and executes, and <output>/THIRD_PARTY_NOTICES.txt:
the licence, notice and patent files of every non-main Go module compiled into
any target (`go list -deps` with the same toolchain and GOOS/GOARCH, read from
the module cache) after the Go standard library's licence. A module without a
licence file stops the build, so no binary ships without its notices.
Optionally rewrites the plugin manifest's version. The package-runtime workflow calls this; it also runs
locally. It builds files only: it never commits, tags or pushes anything.
Publication is a reviewed release operation (docs/release.md).

The development builder, scripts/build_runtime.py, calls build() below for
one target into .local/runtime. The targets, binary name and semver form are
in .github/scripts/release_targets.py. Python 3.10+, standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / ".github" / "scripts"))
from release_targets import BINARY, SEMVER, TARGETS, binary_name  # noqa: E402,F401
RUNTIME_PACKAGE = "./cmd/claude-plugin-runtime"
RUNTIME_MAIN = "cmd/claude-plugin-runtime/main.go"
NOTICES = "THIRD_PARTY_NOTICES.txt"
LICENCE_FILE = re.compile(r"^(licen[cs]e|copying|notice|patents)([._-].*)?$", re.IGNORECASE)
MODULE_FORMAT = ("{{with .Module}}{{if not .Main}}{{.Path}}\t{{.Version}}\t"
                 "{{with .Replace}}{{.Dir}}{{else}}{{.Dir}}{{end}}{{end}}{{end}}")
# The Go distribution's LICENSE (https://go.dev/LICENSE); some toolchain
# packages omit it from GOROOT, so it is reproduced here.
GO_LICENSE = """Copyright 2009 The Go Authors.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

   * Redistributions of source code must retain the above copyright
notice, this list of conditions and the following disclaimer.
   * Redistributions in binary form must reproduce the above
copyright notice, this list of conditions and the following disclaimer
in the documentation and/or other materials provided with the
distribution.
   * Neither the name of Google LLC nor the names of its
contributors may be used to endorse or promote products derived from
this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE."""


def parse_target(value):
    try:
        target_os, target_arch = value.split("-", 1)
    except ValueError:
        raise argparse.ArgumentTypeError(f"target must look like linux-amd64, got {value!r}")
    if value not in TARGETS:
        raise argparse.ArgumentTypeError(f"unsupported target {value!r}; choose from {', '.join(TARGETS)}")
    return target_os, target_arch


def semver(value):
    if not SEMVER.match(value):
        raise argparse.ArgumentTypeError(f"plugin version must be semver (for example 0.3.0), got {value!r}")
    return value


def build(go, source, output, target_os, target_arch):
    """Compile one target and write its SHA256. Returns the digest."""
    destination = output / f"{target_os}-{target_arch}"
    if destination.exists():
        shutil.rmtree(destination)  # no stale binary or digest from an earlier build
    destination.mkdir(parents=True)
    binary = destination / binary_name(f"{target_os}-{target_arch}")
    environment = dict(os.environ, GOOS=target_os, GOARCH=target_arch, CGO_ENABLED="0")
    command = [go, "build", "-trimpath", "-ldflags", "-s -w -buildid=", "-o", str(binary), RUNTIME_PACKAGE]
    subprocess.run(command, cwd=source, env=environment, check=True)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    (destination / "SHA256").write_text(digest + "\n", encoding="utf-8", newline="\n")
    return digest


def runtime_modules(go, source, target_os, target_arch):
    """{path: (version, dir)} for every non-main module compiled into the target."""
    environment = dict(os.environ, GOOS=target_os, GOARCH=target_arch, CGO_ENABLED="0")
    listing = subprocess.run([go, "list", "-mod=readonly", "-deps", "-f", MODULE_FORMAT, RUNTIME_PACKAGE],
                             cwd=source, env=environment, check=True, capture_output=True, text=True).stdout
    modules = {}
    for line in listing.splitlines():
        if not line.strip():
            continue
        path, version, directory = line.split("\t")
        if not directory:
            raise SystemExit(f"Go module {path} {version} is not in the module cache; cannot read its licence")
        modules[path] = (version, directory)
    return modules


def write_notices(go, source, output, targets, ref=""):
    """Write <output>/THIRD_PARTY_NOTICES.txt for the Go modules compiled into the targets."""
    modules = {}
    per_target = {}
    for target_os, target_arch in targets:
        found = runtime_modules(go, source, target_os, target_arch)
        per_target[f"{target_os}-{target_arch}"] = sorted(found)
        modules.update(found)
    go_version = subprocess.run([go, "version"], check=True, capture_output=True, text=True).stdout.split()
    toolchain = go_version[2] if len(go_version) > 2 else "go"
    sections = [f"{'=' * 78}\nGo standard library: LICENSE\n{'=' * 78}\n\n{GO_LICENSE}\n"]
    for path, (version, directory) in sorted(modules.items()):
        files = sorted(p for p in Path(directory).iterdir() if p.is_file() and LICENCE_FILE.match(p.name))
        if not files:
            raise SystemExit(f"Go module {path} {version} has no licence file in {directory}; refusing to ship it without notices")
        for file in files:
            text = file.read_text(encoding="utf-8", errors="replace").rstrip()
            sections.append(f"{'=' * 78}\n{path} {version or '(local)'}: {file.name}\n{'=' * 78}\n\n{text}\n")
    header = ("Third-party notices for the Ork Meter plugin runtime\n"
              "(runtime/<os>-<arch>/ork-meter-plugin-runtime, .exe on Windows).\n\n"
              f"The runtime is cmd/claude-plugin-runtime of EDOS-Engineering/ork-meter at\n{ref or '(local source)'},\n"
              f"built with CGO_ENABLED=0 and {toolchain}. It statically links the\n"
              f"Go standard library and the {len(modules)} Go module(s) below, listed by\n"
              "`go list -deps` for each target with that toolchain. Each is licensed under\n"
              "the terms reproduced here.\n\nModules per target:\n")
    for target, names in per_target.items():
        header += f"  {target}: {', '.join(names) or '(none)'}\n"
    (output / NOTICES).write_text(header + "".join("\n" + s for s in sections), encoding="utf-8", newline="\n")
    return sorted(modules)


def set_manifest_version(manifest, version):
    """Rewrite the top-level "version" in plugin.json in place, keeping its formatting."""
    text = manifest.read_text(encoding="utf-8")
    current = json.loads(text)
    if not isinstance(current, dict) or "version" not in current:
        raise SystemExit(f"{manifest}: no top-level \"version\" to rewrite")
    pattern = re.compile(r'^(\s*"version"\s*:\s*")([^"]*)(")', re.MULTILINE)
    text, count = pattern.subn(lambda m: m.group(1) + version + m.group(3), text, count=1)
    if count != 1:
        raise SystemExit(f"{manifest}: could not locate the \"version\" line to rewrite")
    if json.loads(text).get("version") != version:
        raise SystemExit(f"{manifest}: version rewrite did not take")
    manifest.write_text(text, encoding="utf-8", newline="\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform-source", required=True, type=Path, help="ork-meter checkout to build from")
    parser.add_argument("--targets", nargs="+", type=parse_target, default=[parse_target(t) for t in TARGETS],
                        metavar="OS-ARCH", help=f"targets to build (default: {' '.join(TARGETS)})")
    parser.add_argument("--output", type=Path, default=PLUGIN_ROOT / "runtime",
                        help="directory that receives <os>-<arch>/ (default: the plugin's runtime/)")
    parser.add_argument("--plugin-version", type=semver, help="rewrite the manifest's version to this semver")
    parser.add_argument("--manifest", type=Path, default=PLUGIN_ROOT / ".claude-plugin/plugin.json",
                        help="plugin manifest to rewrite with --plugin-version")
    parser.add_argument("--go", default="go", help="Go toolchain to use (default: go on PATH)")
    parser.add_argument("--source-ref", default="", help="the ork-meter ref being built, named in the notices")
    args = parser.parse_args(argv)

    source = args.platform_source.resolve()
    if not (source / RUNTIME_MAIN).is_file():
        parser.error(f"platform source {source} does not contain {RUNTIME_MAIN}")
    output = args.output.resolve()
    seen = []
    for target in args.targets:
        if target not in seen:
            seen.append(target)

    digests = {}
    for target_os, target_arch in seen:
        digest = build(args.go, source, output, target_os, target_arch)
        digests[f"{target_os}-{target_arch}"] = digest
        print(f"{target_os}-{target_arch}  {digest}")
    modules = write_notices(args.go, source, output, seen, args.source_ref)
    print(f"{output / NOTICES}: {len(modules)} Go module(s) with licence texts")
    if args.plugin_version:
        set_manifest_version(args.manifest, args.plugin_version)
        print(f"{args.manifest}: version set to {args.plugin_version}")
    print(f"Built {len(digests)} runtime target(s) under {output}. Nothing was committed or published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
