"""Whether a merge group may reuse its PR's passing ``ci`` run (#289).

ci.yml's ``changes`` job calls, on merge_group only,

    python3 .github/scripts/ci_reuse.py

with GH_TOKEN, GITHUB_REPOSITORY, EVENT, HEAD_REF, HEAD_SHA and BASE_SHA (the
merge_group event's head_ref, head_sha and base_sha) in the environment. It
writes ``reuse`` (``true``/``false``) and ``run`` (the reused run's URL, or
empty) to $GITHUB_OUTPUT, and the decision to $GITHUB_STEP_SUMMARY.

It answers ``true`` only when every one of these holds:

1. The event is merge_group, and head_ref names one PR
   (``gh-readonly-queue/<base>/pr-<N>-<sha>``). The PR is open, targets the
   queue's base branch, and its head is in this repository.
2. The merge-group commit's tree equals the PR head's tree. ci.yml, the CI
   scripts and the tests are then byte-for-byte the ones the PR run used.
3. The queue's base commit is an ancestor of the PR head, and the merge group
   adds exactly the PR's commits on top of it: the group holds this PR only,
   and the merge commit the PR run tested (the head merged with ``main`` as
   it was then, an ancestor of today's base) had this same tree.
4. Exactly one ``ci`` run exists for the PR head SHA on pull_request. It is
   completed with conclusion success and comes from this repository, and
   every job of its latest attempt succeeded: ci-gate and at least one leg of
   each heavy job, none skipped (a docs-only or fork run skipped some).

Anything else, including an API error, missing or unexpected data, more than
one run, or a run that is queued, in progress (a re-run) or did not succeed,
answers ``false``: the merge group runs in full. Standard library only; the
API calls go through ``gh api``.
"""
import json
import os
import re
import subprocess
import sys

WORKFLOW = "ci.yml"
WORKFLOW_PATH = ".github/workflows/" + WORKFLOW
GATE_JOB = "ci-gate"
# The job names ci.yml gives the heavy jobs a reuse skips or narrows; the PR
# run must have run (and passed) at least one job with each prefix.
HEAVY_JOB_PREFIXES = ("unit (", "native-host (", "wsl (", "connected failure modes")
HEAD_REF_RE = re.compile(r"^(?:refs/heads/)?gh-readonly-queue/(?P<base>.+)/pr-(?P<number>[1-9][0-9]*)-[0-9a-f]{40}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class Refuse(Exception):
    """A reason to run in full."""


def pr_number(head_ref):
    """(base branch, PR number) named by a merge-group head_ref, or None."""
    match = HEAD_REF_RE.match(head_ref or "")
    return (match.group("base"), int(match.group("number"))) if match else None


def _get(data, *keys):
    """data[k1][k2]..., refusing on anything missing or of the wrong shape."""
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            raise Refuse("missing %s in API response" % ".".join(keys))
        data = data[key]
    return data


def _sha(value, what):
    if not isinstance(value, str) or not SHA_RE.match(value):
        raise Refuse("%s is not a commit SHA: %r" % (what, value))
    return value


def _tree(api, repo, sha):
    return _sha(_get(api("repos/%s/commits/%s" % (repo, sha)), "commit", "tree", "sha"), "tree of " + sha)


def check(api, repo, event, head_ref, head_sha, base_sha):
    """The reused run's URL, or raise Refuse. ``api(path)`` returns parsed JSON
    for a GitHub REST path and raises on any error."""
    if event != "merge_group":
        raise Refuse("event is %r, not merge_group" % event)
    if not isinstance(repo, str) or not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise Refuse("repository %r is not owner/name" % repo)
    named = pr_number(head_ref)
    if not named:
        raise Refuse("head_ref %r does not name exactly one PR" % head_ref)
    base_branch, number = named
    group_sha = _sha(head_sha, "merge group head_sha")
    base_sha = _sha(base_sha, "merge group base_sha")

    pr = api("repos/%s/pulls/%d" % (repo, number))
    if _get(pr, "state") != "open":
        raise Refuse("PR #%d is not open" % number)
    if _get(pr, "base", "ref") != base_branch:
        raise Refuse("PR #%d targets %r, the queue %r" % (number, _get(pr, "base", "ref"), base_branch))
    if _get(pr, "head", "repo", "full_name") != repo:
        raise Refuse("PR #%d's head is not in %s" % (number, repo))
    pr_head = _sha(_get(pr, "head", "sha"), "PR head SHA")
    pr_commits = _get(pr, "commits")
    if not isinstance(pr_commits, int) or isinstance(pr_commits, bool) or pr_commits < 1:
        raise Refuse("PR #%d's commit count is %r" % (number, pr_commits))

    group_tree, head_tree = _tree(api, repo, group_sha), _tree(api, repo, pr_head)
    if group_tree != head_tree:
        raise Refuse("merge group tree %s differs from PR head %s's tree %s" % (group_tree, pr_head, head_tree))

    for target, what in ((pr_head, "PR head"), (group_sha, "merge group")):
        compare = api("repos/%s/compare/%s...%s" % (repo, base_sha, target))
        status, ahead, behind = (_get(compare, "status"), _get(compare, "ahead_by"),
                                 _get(compare, "behind_by"))
        if status != "ahead" or behind != 0:
            raise Refuse("%s %s is %s of base %s, not strictly ahead" % (what, target, status, base_sha))
        if ahead != pr_commits:
            raise Refuse("%s is %r commit(s) ahead of base, PR #%d has %d: not this PR alone"
                         % (what, ahead, number, pr_commits))

    runs = api("repos/%s/actions/workflows/%s/runs?head_sha=%s&event=pull_request&per_page=100"
               % (repo, WORKFLOW, pr_head))
    listed = _get(runs, "workflow_runs")
    if _get(runs, "total_count") != 1 or not isinstance(listed, list) or len(listed) != 1:
        raise Refuse("%r pull_request ci runs for %s, need exactly 1" % (_get(runs, "total_count"), pr_head))
    run = listed[0]
    url = _get(run, "html_url")
    if not isinstance(url, str) or not re.match(r"^https://github\.com/%s/actions/runs/[0-9]+$" % re.escape(repo), url):
        raise Refuse("run URL %r is not a run of %s" % (url, repo))
    expected = {"event": "pull_request", "head_sha": pr_head, "status": "completed",
                "conclusion": "success", "path": WORKFLOW_PATH}
    for key, value in expected.items():
        if _get(run, key) != value:
            raise Refuse("run %s has %s %r, need %r" % (url, key, _get(run, key), value))
    if _get(run, "head_repository", "full_name") != repo:
        raise Refuse("run %s is not from %s" % (url, repo))

    run_id = _get(run, "id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or not url.endswith("/%d" % run_id):
        raise Refuse("run %s has id %r" % (url, run_id))
    jobs = api("repos/%s/actions/runs/%d/jobs?filter=latest&per_page=100" % (repo, run_id))
    job_list = _get(jobs, "jobs")
    if not isinstance(job_list, list) or not job_list or _get(jobs, "total_count") != len(job_list):
        raise Refuse("run %s's job list is empty or incomplete" % url)
    names = []
    for job in job_list:
        name = _get(job, "name")
        if _get(job, "status") != "completed" or _get(job, "conclusion") != "success":
            raise Refuse("run %s job %r is %s/%s" % (url, name, _get(job, "status"), _get(job, "conclusion")))
        names.append(name)
    if GATE_JOB not in names:
        raise Refuse("run %s has no %s job" % (url, GATE_JOB))
    for prefix in HEAVY_JOB_PREFIXES:
        if not any(isinstance(name, str) and name.startswith(prefix) for name in names):
            raise Refuse("run %s ran no %r job" % (url, prefix))
    return url


def decide(api, repo, event, head_ref, head_sha, base_sha):
    """(reuse, run URL or "", reason). Never raises: any doubt runs in full."""
    try:
        url = check(api, repo, event, head_ref, head_sha, base_sha)
    except Refuse as refusal:
        return False, "", str(refusal)
    except Exception as error:  # noqa: BLE001 - fail open to full testing
        return False, "", "error: %s: %s" % (type(error).__name__, error)
    return True, url, "merge group tree equals the PR head's, whose ci run passed"


def gh_api(path):
    """GET a GitHub REST path with ``gh api``; raise on any failure."""
    completed = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
    if completed.returncode != 0:
        raise RuntimeError("gh api %s exited %d: %s" % (path, completed.returncode,
                                                      completed.stderr.decode("utf-8", "replace").strip()))
    return json.loads(completed.stdout.decode("utf-8"))


def main(environ=None, api=gh_api):
    environ = os.environ if environ is None else environ
    reuse, url, reason = decide(api, environ.get("GITHUB_REPOSITORY", ""), environ.get("EVENT", ""),
                                environ.get("HEAD_REF", ""), environ.get("HEAD_SHA", ""),
                                environ.get("BASE_SHA", ""))
    verdict = "reuse=%s: %s" % ("true" if reuse else "false", reason)
    print(verdict + ("\nreused run: " + url if url else ""))
    summary = environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            if reuse:
                handle.write("### ci: reusing the PR's passing run\n\n%s\n\nReused run: %s\n" % (reason, url))
            else:
                handle.write("### ci: full run\n\nNo reuse: %s\n" % reason)
    lines = "reuse=%s\nrun=%s\n" % ("true" if reuse else "false", url)
    output = environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
