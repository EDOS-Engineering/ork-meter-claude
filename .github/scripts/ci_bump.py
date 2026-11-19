"""Whether a change is a release's version bump and nothing else.

A release pull request changes one line: the ``version`` in
``.claude-plugin/plugin.json``. Everything else in its tree is main's tree,
which the merge queue has already tested. The ``changes`` job of a workflow
calls

    python3 .github/scripts/ci_bump.py detect --base <sha>

and writes ``bump`` (``true``/``false``) to $GITHUB_OUTPUT (stdout without
it). The unit and native-host matrices skip when it is ``true``. The jobs that
depend on the version itself still run: the release channel, runtime
provenance and the connected failure suite, which pins a runtime per version.

It answers ``true`` only when every one of these holds:

1. The base is a known commit, and the only path changed between it and HEAD
   is ``.claude-plugin/plugin.json``.
2. Both versions of that file are JSON objects that are equal once ``version``
   is removed, and the text differs in exactly one line.
3. The new ``version`` is a plain ``MAJOR.MINOR.PATCH`` that differs from the
   old one. A pre-release or malformed version runs everything.

Anything else, including a git error or unreadable JSON, answers ``false``:
the run is a full one. A new runtime binary, a manifest change beside the
version, or any second file is not a bump.

A gate then calls

    python3 .github/scripts/ci_bump.py accept <result> <changes result> <bump>

which passes only for a job that was ``skipped`` while the ``changes`` job
succeeded and reported ``bump=true``. Standard library only.
"""
import argparse
import json
import os
import re
import subprocess
import sys

MANIFEST = ".claude-plugin/plugin.json"
VERSION_RE = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")


def _manifest(text):
    """The manifest object in ``text``, or None."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def version_bump_only(paths, old_text, new_text):
    """True if ``paths`` is the manifest alone and its only change is a new
    plain version. ``old_text`` and ``new_text`` are the manifest at the base
    and at HEAD; None for either answers False."""
    if list(paths or ()) != [MANIFEST] or not isinstance(old_text, str) or not isinstance(new_text, str):
        return False
    old, new = _manifest(old_text), _manifest(new_text)
    if old is None or new is None:
        return False
    before, after = old.get("version"), new.get("version")
    if not isinstance(before, str) or not isinstance(after, str) or before == after:
        return False
    if not VERSION_RE.match(after):
        return False
    if {k: v for k, v in old.items() if k != "version"} != {k: v for k, v in new.items() if k != "version"}:
        return False
    old_lines, new_lines = old_text.split("\n"), new_text.split("\n")
    if len(old_lines) != len(new_lines):
        return False
    return sum(a != b for a, b in zip(old_lines, new_lines)) == 1


def accept(result, changes_result, bump):
    """Whether a gate may count a skipped job as passed: only when the
    ``changes`` job succeeded and said the change is a version bump alone."""
    return result == "skipped" and changes_result == "success" and bump == "true"


def _git(*args):
    return subprocess.run(("git",) + args, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout


def detect(base):
    """(bump, reason) for the change between ``base`` and HEAD. Never raises."""
    if not base:
        return False, "no base commit"
    try:
        _git("cat-file", "-e", base + "^{commit}")
        names = _git("diff", "--no-renames", "--name-only", "-z", base, "HEAD")
        paths = [p for p in names.decode("utf-8", "surrogateescape").split("\0") if p]
        if paths != [MANIFEST]:
            return False, "%d path(s) changed, not %s alone" % (len(paths), MANIFEST)
        old_text = _git("show", "%s:%s" % (base, MANIFEST)).decode("utf-8")
        new_text = _git("show", "HEAD:%s" % MANIFEST).decode("utf-8")
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
        return False, "error: %s" % type(error).__name__
    if version_bump_only(paths, old_text, new_text):
        return True, "only the version in %s changed" % MANIFEST
    return False, "%s changed beyond a plain version bump" % MANIFEST


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    found = sub.add_parser("detect")
    found.add_argument("--base", default="")
    gate = sub.add_parser("accept")
    gate.add_argument("result")
    gate.add_argument("changes_result")
    gate.add_argument("bump")
    args = parser.parse_args(argv)

    if args.command == "accept":
        ok = accept(args.result, args.changes_result, args.bump)
        print("%s (changes: %s, bump: %s): %s" % (
            args.result, args.changes_result, args.bump, "accepted as a version bump" if ok else "not accepted"))
        return 0 if ok else 1

    bump, reason = detect(args.base)
    line = "bump=%s\n" % ("true" if bump else "false")
    print(reason)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(line)
    sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
