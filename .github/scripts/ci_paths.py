"""Which CI areas a change can affect (#260), and whether a gate may accept a skip.

ci.yml and benchmarks.yml run a small ``changes`` job that calls

    python3 .github/scripts/ci_paths.py changes --base <sha>

It lists the files changed between <sha> and HEAD and writes ``plugin``,
``host`` and ``bench`` (``true``/``false``) to $GITHUB_OUTPUT (stdout without
it). An empty or unknown base, a failed diff, or an empty diff reports
everything changed: when unsure, run.

ci-gate and benchmark-gate call

    python3 .github/scripts/ci_paths.py accept <result> <changes result> <affected> [--reused <reuse>]

which passes for a job that succeeded, and for a skipped job only when the
``changes`` job succeeded and reported its area unaffected, or (for the jobs
ci-gate passes ``--reused`` for) reported ``reuse=true``: the merge group
reuses its PR's passing run (#289, .github/scripts/ci_reuse.py). A failed or
cancelled ``changes`` job fails the gate.

No workflow-level ``paths:`` filters: a required check that never reports
stalls the PR and the merge queue.
"""
import argparse
import os
import subprocess
import sys

# Files that cannot affect the plugin jobs: the benchmark harness and root
# documentation. docs/ stays plugin-relevant (tests read docs/*.md and
# docs/optional-agents/).
ROOT_DOCS = ("LICENSE",)

# Plugin files neither an installed host loads nor the host suite
# (check-native-host.py) reads: documentation, except the optional agents the
# skill suite reads, and the connected suite, which only its own Linux job runs
# (the runtime pin PRs change nothing else). The Windows native-host leg skips
# when nothing else changed. Anything not named runs.
HOST_DOCS_KEPT = "docs/optional-agents/"
CONNECTED_SUITE = "tests/native_failure_connected_qualification.py"

# What the benchmark harness reads from the plugin tree: its own directory,
# the plugin manifest, and everything snapshot_plugin copies that loads into a
# session (check-version-bump.py's executable surface: hooks, scripts, skills,
# agents ...). .gitattributes changes the checked-out bytes SOURCE.json hashes.
BENCH_PREFIXES = (
    "benchmarks/", ".claude-plugin/", ".github/",
    "hooks/", "scripts/", "skills/", "agents/", "bin/", "commands/",
    "output-styles/", "monitors/", "runtime/",
)
BENCH_FILES = (".mcp.json", ".lsp.json", "settings.json", ".gitattributes")

# A workflow or CI script change runs everything.
EVERYTHING_PREFIXES = (".github/",)


def plugin_irrelevant(path):
    """True if ``path`` cannot affect the plugin jobs."""
    if path.startswith("benchmarks/"):
        return True
    if "/" in path:
        return False
    return path.endswith(".md") or path in ROOT_DOCS


def host_irrelevant(path):
    """True if ``path`` cannot change what an installed host qualifies."""
    if plugin_irrelevant(path) or path == CONNECTED_SUITE:
        return True
    return path.startswith("docs/") and not path.startswith(HOST_DOCS_KEPT)


def bench_relevant(path):
    """True if ``path`` can affect the benchmark jobs."""
    if path.startswith(BENCH_PREFIXES) or path in BENCH_FILES:
        return True
    return "/" not in path and path.startswith("requirements")


def classify(paths):
    """{"plugin": bool, "host": bool, "bench": bool} for the changed ``paths``.

    ``None`` (unknown) or an empty list runs everything."""
    paths = [p for p in (paths or ()) if p]
    if not paths or any(p.startswith(EVERYTHING_PREFIXES) for p in paths):
        return {"plugin": True, "host": True, "bench": True}
    return {"plugin": not all(plugin_irrelevant(p) for p in paths),
            "host": not all(host_irrelevant(p) for p in paths),
            "bench": any(bench_relevant(p) for p in paths)}


def accept(result, changes_result, affected, reused="false"):
    """Whether a gate accepts a job's ``result``.

    Success passes. Skipped passes only if the ``changes`` job succeeded and
    said the area is unaffected (``affected`` "false") or the merge group
    reuses its PR's run (``reused`` "true"). Anything else fails."""
    if result == "success":
        return True
    return (result == "skipped" and changes_result == "success"
            and (affected == "false" or reused == "true"))


def changed_files(base):
    """Paths changed between ``base`` and HEAD, or None if unknown."""
    if not base:
        return None
    try:
        subprocess.run(["git", "cat-file", "-e", base + "^{commit}"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out = subprocess.run(["git", "diff", "--no-renames", "--name-only", "-z", base, "HEAD"],
                             check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0") if p]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    changes = sub.add_parser("changes")
    changes.add_argument("--base", default="")
    gate = sub.add_parser("accept")
    gate.add_argument("result")
    gate.add_argument("changes_result")
    gate.add_argument("affected")
    gate.add_argument("--reused", default="false")
    args = parser.parse_args(argv)

    if args.command == "accept":
        ok = accept(args.result, args.changes_result, args.affected, args.reused)
        print("%s (changes: %s, affected: %s, reused: %s): %s" % (
            args.result, args.changes_result, args.affected, args.reused, "accepted" if ok else "REJECTED"))
        return 0 if ok else 1

    paths = changed_files(args.base)
    if paths is None:
        print("base %r unknown: everything counts as changed" % args.base)
    else:
        print("%d file(s) changed since %s" % (len(paths), args.base))
        for path in paths:
            print("  " + path)
    areas = classify(paths)
    lines = "".join("%s=%s\n" % (k, "true" if v else "false") for k, v in sorted(areas.items()))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(lines)
    sys.stdout.write(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
