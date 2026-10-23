"""Assemble the GitHub Release assets for a tagged plugin release.

Given a plugin checkout at the release commit (the commit tag v<version>
points at; `main` is the release, docs/release.md), this writes into --output:

  ork-meter-claude-plugin-<version>.zip         the plugin tree as installed
                                                 (git archive of the release
                                                 commit: tracked files only)
  ork-meter-plugin-runtime-<os>-<arch>[.exe]    each bundled runtime binary
  <asset>.sha256                                 `<hex>  <asset>` for sha256sum -c
  ork-meter-claude-plugin-<version>.spdx.json   SPDX 2.3 SBOM of the assets above
                                                 (build-release-sbom.py)
  manifest.json                                  plugin version, ork-meter ref,
                                                 per-asset sha256 (the SBOM's
                                                 included)

It refuses to run when the checkout's plugin.json version differs from
--version, when a runtime target is missing, or when a binary's digest differs
from its sibling SHA256 file (the file scripts/runtime.sh verifies), so a
release never ships assets that disagree with the reviewed commit. It writes
files only: the publish-release workflow uploads them. Python 3.10+, standard
library and git only.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_targets import BINARY, SEMVER, TARGETS, binary_name  # noqa: E402

# Its file name has dashes, like this script's, so it is loaded by path.
_spec = importlib.util.spec_from_file_location("build_release_sbom", Path(__file__).resolve().parent / "build-release-sbom.py")
sbom = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sbom)

PLUGIN = "ork-meter"


def semver(value):
    if not SEMVER.match(value):
        raise argparse.ArgumentTypeError(f"version must be semver (for example 0.3.0), got {value!r}")
    return value


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_sum(output, name, value):
    (output / (name + ".sha256")).write_text(f"{value}  {name}\n", encoding="utf-8", newline="\n")


def runtime_assets(root, output):
    """Copy each bundled binary under a per-target name; verify its SHA256 file first."""
    assets = {}
    for target in TARGETS:
        name = binary_name(target)
        package = root / "runtime" / target
        binary, manifest = package / name, package / "SHA256"
        if not binary.is_file() or not manifest.is_file():
            raise SystemExit(f"runtime/{target}: missing {name} or SHA256; this commit is not a complete release")
        expected = manifest.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise SystemExit(f"runtime/{target}/SHA256 is not a 64-character lowercase hex digest")
        actual = digest(binary)
        if actual != expected:
            raise SystemExit(f"runtime/{target}/{name}: digest {actual} differs from SHA256 file {expected}")
        asset = f"{BINARY}-{target}" + name[len(BINARY):]   # + .exe on Windows
        (output / asset).write_bytes(binary.read_bytes())
        write_sum(output, asset, actual)
        assets[asset] = {"sha256": actual, "target": target, "path": f"runtime/{target}/{name}", "file": output / asset}
    return assets


def archive(root, output, version, ref):
    """Zip the release commit's tracked tree, the plugin as a marketplace install copies it."""
    name = f"ork-meter-claude-plugin-{version}.zip"
    subprocess.run(["git", "archive", "--format=zip", f"--prefix={PLUGIN}/", "-o", str(output / name), ref],
                   cwd=root, check=True)
    value = digest(output / name)
    write_sum(output, name, value)
    return name, value


def build(root, output, version, ork_meter_ref, archive_ref, ork_meter_commit=None, pip_report=None):
    root, output = Path(root).resolve(), Path(output).resolve()
    manifest = json.loads((root / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    if manifest.get("name") != PLUGIN or manifest.get("version") != version:
        raise SystemExit(f"plugin.json is {manifest.get('name')} {manifest.get('version')}, not {PLUGIN} {version}")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"{output} is not empty; assets are assembled into a fresh directory")
    output.mkdir(parents=True, exist_ok=True)
    plugin_commit = subprocess.check_output(["git", "rev-parse", f"{archive_ref}^{{commit}}"], cwd=root, text=True).strip()
    runtime = runtime_assets(root, output)
    zip_name, zip_digest = archive(root, output, version, archive_ref)
    assets = {zip_name: zip_digest}
    assets.update({name: entry["sha256"] for name, entry in runtime.items()})
    # The SBOM describes the assets above, so it comes after them; the
    # manifest names its digest, so it comes after the SBOM.
    document = sbom.build_sbom(root, version, plugin_commit, ork_meter_ref, ork_meter_commit,
                               (zip_name, zip_digest), runtime, pip_report)
    sbom_name = f"ork-meter-claude-plugin-{version}.spdx.json"
    sbom_digest = sbom.write_sbom(output / sbom_name, document)
    write_sum(output, sbom_name, sbom_digest)
    assets[sbom_name] = sbom_digest
    record = {
        "plugin": PLUGIN,
        "plugin_version": version,
        "tag": f"v{version}",
        "plugin_commit": plugin_commit,
        "ork_meter_repository": "EDOS-Engineering/ork-meter",
        "ork_meter_ref": ork_meter_ref,
        "ork_meter_commit": ork_meter_commit,
        "runtime": {entry["target"]: {"asset": name, "sha256": entry["sha256"], "path": entry["path"]}
                    for name, entry in runtime.items()},
        "sbom": {"asset": sbom_name, "format": "SPDX-2.3", "sha256": sbom_digest},
        "assets": dict(sorted(assets.items())),
    }
    (output / "manifest.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    for name, value in sorted(assets.items()):
        print(f"{value}  {name}")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--version", type=semver, required=True, help="the plugin version being released")
    parser.add_argument("--ork-meter-ref", required=True, help="the ork-meter ref the runtime was built from")
    parser.add_argument("--ork-meter-commit", help="the ork-meter commit that ref resolved to, if known")
    parser.add_argument("--archive-ref", default="HEAD", help="the git ref to archive (the tagged release commit)")
    parser.add_argument("--output", type=Path, required=True, help="an empty or new directory for the assets")
    parser.add_argument("--pip-report", type=Path,
                        help="pip install --dry-run --report of requirements-hooks.txt, for the SBOM's Python packages")
    args = parser.parse_args(argv)
    report = json.loads(args.pip_report.read_text(encoding="utf-8")) if args.pip_report else None
    build(args.plugin_root, args.output, args.version, args.ork_meter_ref, args.archive_ref, args.ork_meter_commit, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
