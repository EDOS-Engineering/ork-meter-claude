"""The release targets, the runtime binary's name and the semver form, once.

scripts/package_runtime.py, build-release-assets.py and
verify-runtime-attestations.py import these. check-release-commit.py stays
standalone, and scripts/runtime.sh and the workflows are shell, so
tests/release_targets_regressions.py checks that they agree. It is on
check-version-bump.py's executable surface. Python 3.10+, standard library
only.
"""
import re

TARGETS = ("darwin-arm64", "darwin-amd64", "linux-amd64", "linux-arm64", "windows-amd64")
BINARY = "ork-meter-plugin-runtime"
# Semantic version 2.0.0 (https://semver.org), the form plugin.json "version" takes.
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def binary_name(target):
    """The runtime binary's file name for ``target`` (``<os>-<arch>``)."""
    return BINARY + (".exe" if target.startswith("windows") else "")
