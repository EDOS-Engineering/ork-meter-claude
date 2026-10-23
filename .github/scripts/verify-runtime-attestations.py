"""Refuse runtime binaries that the package-runtime workflow did not build.

Usage: verify-runtime-attestations.py <runtime-dir>

<runtime-dir> must hold exactly runtime/<os>-<arch>/{binary, SHA256} for
every release target and THIRD_PARTY_NOTICES.txt (the licence texts of the
Go modules linked into the binaries), nothing else. Each binary must carry a build provenance
attestation on this repository signed by package-runtime.yml running on
main, checked with `gh attestation verify` (needs `gh` and GH_TOKEN with
attestations read access). Any failure, including a missing `gh`, refuses.

The v0.3.0 binaries were built before package-runtime attested anything, so
they have no attestation. Exactly those five digests, each at its own target,
are reported as unattested legacy binaries instead of being verified; any
other bytes must verify. Python 3.10+, standard library and `gh` only.
"""
import hashlib
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_targets import BINARY, TARGETS, binary_name  # noqa: E402,F401

REPOSITORY = "EDOS-Engineering/ork-meter-claude"
SIGNER_WORKFLOW = REPOSITORY + "/.github/workflows/package-runtime.yml"
SOURCE_REF = "refs/heads/main"
NOTICES = "THIRD_PARTY_NOTICES.txt"
# v0.3.0 (fc742d7), built before build-time attestation (#124). Never extend
# this: every later release is attested by package-runtime.
LEGACY = {
    ("darwin-amd64", "20f8c8797f1b9f3ccd60089daad6a28e876bb8526a807a677304d207d9d84c3b"),
    ("darwin-arm64", "3b11f99a3d70d646894c240fc3cd3d3b1ba5f687cfd7b76a3bcc20d9bca6f6fb"),
    ("linux-amd64", "ef6ab9ab5dd7d878c7d68f262d7192ead7987b25f4a31c735b641f1039075210"),
    ("linux-arm64", "6d0b1e808ab5dbd62e9001e04c8a41ce97ce3107e730999c7aefb29ec3bdb1a5"),
    ("windows-amd64", "27737e4a1cc02cdb8c70c8555b190acb13b4c38c3e1a8b8fd8c666cfc082628b"),
}


def expected_files():
    return {f"{t}/{n}" for t in TARGETS for n in (binary_name(t), "SHA256")}


def gh_verify(path):
    command = ["gh", "attestation", "verify", str(path), "--repo", REPOSITORY,
               "--signer-workflow", SIGNER_WORKFLOW, "--source-ref", SOURCE_REF,
               "--deny-self-hosted-runners"]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        return False, f"cannot run gh: {exc}"
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def verify(root, verifier=gh_verify):
    """Return a list of failures; print one line per binary."""
    root = Path(root)
    if not root.is_dir():
        return [f"{root} is not a directory"]
    found = {p.relative_to(root).as_posix() for p in root.rglob("*") if not p.is_dir()}
    failures = [f"unexpected file {root.name}/{p}" for p in sorted(found - expected_files() - {NOTICES})]
    failures += [f"missing {root.name}/{p}" for p in sorted(expected_files() - found)]
    for target in TARGETS:
        path = root / target / binary_name(target)
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if (target, digest) in LEGACY:
            print(f"::warning::{target}: v0.3.0 binary {digest} predates build attestation; not verified (docs/release.md)")
            continue
        ok, detail = verifier(path)
        if ok:
            print(f"{target}: {digest} attested by package-runtime on main")
        else:
            failures.append(f"{target}: {digest} has no valid package-runtime attestation: {detail}")
    return failures


def main(argv):
    if len(argv) != 2:
        print("usage: verify-runtime-attestations.py <runtime-dir>", file=sys.stderr)
        return 2
    failures = verify(argv[1])
    for failure in failures:
        print(f"::error::{failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
