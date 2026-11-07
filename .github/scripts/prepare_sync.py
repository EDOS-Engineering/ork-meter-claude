#!/usr/bin/env python3
"""Finish a synced tree so the release that follows can pass CI and be published.

Usage: prepare_sync.py --private <checkout> --public <checkout> --version <version>

Run by public-sync after sync_public.py. Two things the mirror cannot take as
copied from the private tag:

* plugin.json becomes <version>-dev. package-runtime's release commit must be
  the commit that sets <version>; check-release-commit.py refuses one that does
  not change plugin.json.
* tests/native_failure_connected_qualification.py is owned here and needs a
  runtime pin per version. <version> takes the private tag's pin, because the
  public build of the same ork-meter commit is byte-identical. <version>-dev
  takes the pin of the release already on main, whose runtime/ it still carries.

Python 3.10+, standard library and git only.
"""
import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys

MANIFEST = ".claude-plugin/plugin.json"
PINS = "tests/native_failure_connected_qualification.py"
ANCHOR = "_PLUGIN_VERSION = json.loads("


def candidates(source):
    """RUNTIME_CANDIDATES as a dict, without running the test module."""
    pins = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "RUNTIME_CANDIDATES":
                pins.update(ast.literal_eval(node.value))
            elif isinstance(target, ast.Subscript) and getattr(target.value, "id", None) == "RUNTIME_CANDIDATES":
                value = node.value
                if isinstance(value, ast.Subscript):
                    pins[ast.literal_eval(target.slice)] = pins[ast.literal_eval(value.slice)]
                else:
                    pins[ast.literal_eval(target.slice)] = ast.literal_eval(value)
    return pins


def add_pins(public_source, private_source, version, previous):
    released = candidates(private_source).get(version)
    if released is None:
        raise ValueError(f"the private tag v{version} has no runtime pin for {version}")
    known = candidates(public_source)
    if previous not in known:
        raise ValueError(f"this repository has no runtime pin for {previous}, the release on main")
    if version in known:
        raise ValueError(f"this repository already has a runtime pin for {version}")
    if public_source.count(ANCHOR) != 1:
        raise ValueError(f"{PINS} does not have exactly one {ANCHOR}")
    lines = (f"RUNTIME_CANDIDATES[{version!r}] = {released!r}\n"
             f"RUNTIME_CANDIDATES[{version + '-dev'!r}] = RUNTIME_CANDIDATES[{previous!r}]\n").replace("'", '"')
    return public_source.replace(ANCHOR, lines + ANCHOR, 1)


def set_dev_version(manifest_text, version):
    pattern = re.compile(r'("version"\s*:\s*")' + re.escape(version) + '"')
    if len(pattern.findall(manifest_text)) != 1:
        raise ValueError(f"{MANIFEST} does not say version {version}")
    return pattern.sub(lambda match: f'{match.group(1)}{version}-dev"', manifest_text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--public", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        shown = subprocess.run(["git", "-C", str(args.public), "show", f"HEAD:{MANIFEST}"], check=True, capture_output=True, text=True).stdout
        previous = json.loads(shown)["version"].split("-")[0]
        manifest = args.public / MANIFEST
        pins = args.public / PINS
        pins_text = add_pins(pins.read_text(encoding="utf-8"), (args.private / PINS).read_text(encoding="utf-8"), args.version, previous)
        manifest_text = set_dev_version(manifest.read_text(encoding="utf-8"), args.version)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"::error::prepare_sync: {error}")
        return 1
    pins.write_text(pins_text, encoding="utf-8")
    manifest.write_text(manifest_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
