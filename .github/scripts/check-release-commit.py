"""Refuse publication unless a commit is the package-runtime workflow's release commit.

Usage: check-release-commit.py <commit>

The commit must be authored by github-actions[bot] (the identity the
package-runtime workflow commits as), have exactly one parent, and change
only runtime/<os>-<arch>/ binaries, their SHA256 files,
runtime/THIRD_PARTY_NOTICES.txt and .claude-plugin/plugin.json relative to that parent, including plugin.json
(every release bumps the version). A git author is self-declared, so this is
a consistency check; the build provenance attestation each binary must carry
(verify-runtime-attestations.py) is what ties the bytes to the workflow run.
Python 3.10+, standard library and git only.
"""
import re
import subprocess
import sys

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MANIFEST = ".claude-plugin/plugin.json"
ALLOWED = re.compile(
    r"^(runtime/(darwin|linux|windows)-(amd64|arm64)/(ork-meter-plugin-runtime(\.exe)?|SHA256)"
    r"|runtime/THIRD_PARTY_NOTICES\.txt|\.claude-plugin/plugin\.json)$"
)


def validate(author_name, author_email, parents, paths):
    if (author_name, author_email) != (BOT_NAME, BOT_EMAIL):
        raise ValueError(f"author is {author_name} <{author_email}>, not {BOT_NAME} <{BOT_EMAIL}>")
    if len(parents) != 1:
        raise ValueError(f"has {len(parents)} parents; a release commit has exactly one")
    unexpected = [p for p in paths if not ALLOWED.match(p)]
    if unexpected:
        raise ValueError("changes files outside runtime/ and plugin.json: " + ", ".join(unexpected))
    if MANIFEST not in paths:
        raise ValueError(f"does not change {MANIFEST}; every release bumps the version")


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def main(argv):
    if len(argv) != 2:
        print("usage: check-release-commit.py <commit>", file=sys.stderr)
        return 2
    commit = argv[1]
    try:
        name, email, parents = git("log", "-1", "--format=%an%x00%ae%x00%P", commit).rstrip("\n").split("\x00")
        parents = parents.split()
        paths = []
        if len(parents) == 1:
            paths = git("diff", "--no-renames", "--name-only", "-z", parents[0], commit).split("\x00")
            paths = [p for p in paths if p]
        validate(name, email, parents, paths)
    except (subprocess.CalledProcessError, ValueError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else exc
        print(f"Release commit {commit} refused: {detail}", file=sys.stderr)
        return 1
    print(f"Release commit {commit}: authored by {BOT_NAME}, changes only runtime/ and plugin.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
