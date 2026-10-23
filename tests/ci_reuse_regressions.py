"""A merge group reuses its PR's passing ci run only when the trees are the
same and every check of .github/scripts/ci_reuse.py holds (#289); any doubt
runs in full. The GitHub API is a fake: nothing here reaches the network."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402

reuse = load(".github/scripts/ci_reuse.py")
SCRIPT = ROOT / ".github/scripts/ci_reuse.py"

REPO = "EDOS-Engineering/ork-meter-claude"
BASE = "b" * 40
HEAD = "a" * 40
GROUP = "c" * 40
TREE = "d" * 40
RUN_ID = 4242
RUN_URL = "https://github.com/%s/actions/runs/%d" % (REPO, RUN_ID)
HEAD_REF = "refs/heads/gh-readonly-queue/main/pr-289-%s" % BASE
JOBS = ["changes", "unit (ubuntu-latest)", "unit (windows-latest)", "native-host (ubuntu-latest, claude latest)",
        "wsl (ubuntu-24.04, claude latest)", "connected failure modes (linux)", "runtime provenance",
        "release channel", "ci-gate"]


def responses():
    """The API answers for a merge group that may reuse PR #289's run."""
    run = {"id": RUN_ID, "html_url": RUN_URL, "event": "pull_request", "head_sha": HEAD, "status": "completed",
           "conclusion": "success", "path": ".github/workflows/ci.yml", "head_repository": {"full_name": REPO}}
    return {
        "repos/%s/pulls/289" % REPO: {"state": "open", "commits": 2, "base": {"ref": "main"},
                                      "head": {"sha": HEAD, "repo": {"full_name": REPO}}},
        "repos/%s/commits/%s" % (REPO, GROUP): {"commit": {"tree": {"sha": TREE}}},
        "repos/%s/commits/%s" % (REPO, HEAD): {"commit": {"tree": {"sha": TREE}}},
        "repos/%s/compare/%s...%s" % (REPO, BASE, HEAD): {"status": "ahead", "ahead_by": 2, "behind_by": 0},
        "repos/%s/compare/%s...%s" % (REPO, BASE, GROUP): {"status": "ahead", "ahead_by": 2, "behind_by": 0},
        "repos/%s/actions/workflows/ci.yml/runs?head_sha=%s&event=pull_request&per_page=100" % (REPO, HEAD):
            {"total_count": 1, "workflow_runs": [run]},
        "repos/%s/actions/runs/%d/jobs?filter=latest&per_page=100" % (REPO, RUN_ID):
            {"total_count": len(JOBS), "jobs": [{"name": name, "status": "completed", "conclusion": "success"}
                                                for name in JOBS]},
    }


class FakeAPI:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path not in self.data:
            raise RuntimeError("HTTP 404: " + path)
        value = self.data[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def decide(data=None, event="merge_group", head_ref=HEAD_REF, head_sha=GROUP, base_sha=BASE):
    api = FakeAPI(responses() if data is None else data)
    return reuse.decide(api, REPO, event, head_ref, head_sha, base_sha)


def runs_key():
    return "repos/%s/actions/workflows/ci.yml/runs?head_sha=%s&event=pull_request&per_page=100" % (REPO, HEAD)


def jobs_key():
    return "repos/%s/actions/runs/%d/jobs?filter=latest&per_page=100" % (REPO, RUN_ID)


def edit(change):
    """responses() after ``change(data)``."""
    data = responses()
    change(data)
    return data


class Decide(unittest.TestCase):
    def assertFull(self, result, reason=None):
        ok, url, why = result
        self.assertFalse(ok, why)
        self.assertEqual(url, "")
        if reason:
            self.assertIn(reason, why)

    def test_same_tree_and_a_passing_run_reuses(self):
        self.assertEqual(decide(), (True, RUN_URL, "merge group tree equals the PR head's, whose ci run passed"))

    def test_bare_head_ref_is_accepted(self):
        self.assertTrue(decide(head_ref=HEAD_REF[len("refs/heads/"):])[0])

    def test_different_tree_runs_in_full(self):
        data = edit(lambda d: d["repos/%s/commits/%s" % (REPO, GROUP)]["commit"]["tree"].update(sha="e" * 40))
        self.assertFull(decide(data), "differs from PR head")

    def test_failed_cancelled_or_unfinished_run_runs_in_full(self):
        for status, conclusion in (("completed", "failure"), ("completed", "cancelled"),
                                   ("completed", "skipped"), ("completed", None),
                                   ("in_progress", None), ("queued", None)):
            with self.subTest(status=status, conclusion=conclusion):
                data = edit(lambda d: d[runs_key()]["workflow_runs"][0].update(status=status, conclusion=conclusion))
                self.assertFull(decide(data))

    def test_missing_run_runs_in_full(self):
        self.assertFull(decide(edit(lambda d: d.update({runs_key(): {"total_count": 0, "workflow_runs": []}}))),
                        "need exactly 1")
        self.assertFull(decide(edit(lambda d: d.pop(runs_key()))), "HTTP 404")

    def test_more_than_one_run_runs_in_full(self):
        def two(d):
            runs = d[runs_key()]
            runs["workflow_runs"].append(dict(runs["workflow_runs"][0], id=RUN_ID + 1))
            runs["total_count"] = 2
        self.assertFull(decide(edit(two)), "need exactly 1")
        # A count above the page's length is still more than one.
        self.assertFull(decide(edit(lambda d: d[runs_key()].update(total_count=2))), "need exactly 1")

    def test_run_of_another_event_workflow_sha_or_repository_runs_in_full(self):
        for key, value in (("event", "merge_group"), ("head_sha", "f" * 40),
                           ("path", ".github/workflows/publish-release.yml"),
                           ("head_repository", {"full_name": "someone/fork"}),
                           ("html_url", "https://example.com/actions/runs/%d" % RUN_ID), ("id", RUN_ID + 1)):
            with self.subTest(key=key):
                self.assertFull(decide(edit(lambda d: d[runs_key()]["workflow_runs"][0].update({key: value}))))

    def test_a_failed_skipped_or_missing_job_runs_in_full(self):
        for name in JOBS:
            for conclusion in ("failure", "cancelled", "skipped", None):
                with self.subTest(job=name, conclusion=conclusion):
                    def change(d, name=name, conclusion=conclusion):
                        for job in d[jobs_key()]["jobs"]:
                            if job["name"] == name:
                                job["conclusion"] = conclusion
                    self.assertFull(decide(edit(change)))
        for name in ("ci-gate", "unit (ubuntu-latest)", "native-host (ubuntu-latest, claude latest)",
                     "wsl (ubuntu-24.04, claude latest)", "connected failure modes (linux)"):
            with self.subTest(dropped=name):
                def drop(d, name=name):
                    jobs = d[jobs_key()]
                    if name == "unit (ubuntu-latest)":  # drop both unit legs
                        jobs["jobs"] = [j for j in jobs["jobs"] if not j["name"].startswith("unit (")]
                    else:
                        jobs["jobs"] = [j for j in jobs["jobs"] if j["name"] != name]
                    jobs["total_count"] = len(jobs["jobs"])
                self.assertFull(decide(edit(drop)))
        self.assertFull(decide(edit(lambda d: d[jobs_key()].update(total_count=len(JOBS) + 1))), "incomplete")
        self.assertFull(decide(edit(lambda d: d[jobs_key()].update(total_count=0, jobs=[]))), "incomplete")

    def test_api_error_runs_in_full(self):
        for key in responses():
            with self.subTest(key=key):
                self.assertFull(decide(edit(lambda d: d.update({key: RuntimeError("HTTP 502")}))), "error")
        self.assertFull(decide(edit(lambda d: d.update({runs_key(): ValueError("bad JSON")}))), "ValueError")

    def test_malformed_data_runs_in_full(self):
        for key in responses():
            for bad in ({}, [], None, "text"):
                with self.subTest(key=key, bad=bad):
                    self.assertFull(decide(edit(lambda d: d.update({key: bad}))))

    def test_more_than_one_pr_in_the_group_runs_in_full(self):
        # The group adds another PR's commits on top of the base.
        compare = "repos/%s/compare/%s...%s" % (REPO, BASE, GROUP)
        self.assertFull(decide(edit(lambda d: d[compare].update(ahead_by=3))), "not this PR alone")
        for head_ref in ("refs/heads/gh-readonly-queue/main/pr-289-pr-290-%s" % BASE,
                         "refs/heads/gh-readonly-queue/main/pr-289", "refs/heads/feature",
                         "gh-readonly-queue/main/pr-0-%s" % BASE, "", None):
            with self.subTest(head_ref=head_ref):
                self.assertFull(decide(head_ref=head_ref), "does not name exactly one PR")

    def test_pr_behind_or_diverged_from_the_base_runs_in_full(self):
        compare = "repos/%s/compare/%s...%s" % (REPO, BASE, HEAD)
        for change in ({"status": "diverged", "behind_by": 1}, {"status": "behind", "ahead_by": 0, "behind_by": 1},
                       {"status": "identical", "ahead_by": 0}, {"ahead_by": 1}):
            with self.subTest(change=change):
                self.assertFull(decide(edit(lambda d: d[compare].update(change))))

    def test_closed_fork_or_retargeted_pr_runs_in_full(self):
        pr = "repos/%s/pulls/289" % REPO
        for change in ({"state": "closed"}, {"head": {"sha": HEAD, "repo": {"full_name": "someone/fork"}}},
                       {"head": {"sha": HEAD, "repo": None}}, {"base": {"ref": "release"}},
                       {"commits": 0}, {"commits": True}, {"head": {"sha": "nope", "repo": {"full_name": REPO}}}):
            with self.subTest(change=change):
                self.assertFull(decide(edit(lambda d: d[pr].update(change))))

    def test_bad_event_shas_run_in_full(self):
        for kwargs in ({"head_sha": ""}, {"base_sha": ""}, {"head_sha": "C" * 40}, {"base_sha": None}):
            with self.subTest(**kwargs):
                self.assertFull(decide(**kwargs))

    def test_other_events_never_reuse_or_call_the_api(self):
        for event in ("pull_request", "schedule", "workflow_dispatch", "push", ""):
            with self.subTest(event=event):
                api = FakeAPI(responses())
                ok, url, why = reuse.decide(api, REPO, event, HEAD_REF, GROUP, BASE)
                self.assertFalse(ok)
                self.assertIn("not merge_group", why)
                self.assertEqual(api.calls, [])


class Main(unittest.TestCase):
    def run_main(self, environ, api):
        with tempfile.TemporaryDirectory() as temp:
            output, summary = Path(temp) / "out", Path(temp) / "summary"
            env = dict(environ, GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary))
            self.assertEqual(reuse.main(env, api), 0)
            return output.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8")

    ENV = {"GITHUB_REPOSITORY": REPO, "EVENT": "merge_group", "HEAD_REF": HEAD_REF, "HEAD_SHA": GROUP,
           "BASE_SHA": BASE}

    def test_reuse_writes_outputs_and_logs_the_run(self):
        output, summary = self.run_main(self.ENV, FakeAPI(responses()))
        self.assertEqual(output, "reuse=true\nrun=%s\n" % RUN_URL)
        self.assertIn("Reused run: " + RUN_URL, summary)

    def test_refusal_writes_false(self):
        output, summary = self.run_main(dict(self.ENV, EVENT="pull_request"), FakeAPI(responses()))
        self.assertEqual(output, "reuse=false\nrun=\n")
        self.assertIn("No reuse: event is 'pull_request'", summary)

    def test_cli_without_gh_or_network_runs_in_full(self):
        """The real entry point fails open when gh cannot answer."""
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            env = {"PATH": temp, "GITHUB_OUTPUT": str(output), **self.ENV}
            if os.name == "nt":
                env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
            completed = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT)], env=env,
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "reuse=false\nrun=\n")
            self.assertIn("reuse=false: error", completed.stdout)

    def test_gh_api_raises_on_a_failed_call(self):
        with tempfile.TemporaryDirectory() as temp:
            if os.name == "nt":
                self.skipTest("the fake gh is a POSIX shell script")
            gh = Path(temp) / "gh"
            gh.write_text("#!/bin/sh\necho 'HTTP 403' >&2\nexit 1\n")
            gh.chmod(0o755)
            old = os.environ["PATH"]
            os.environ["PATH"] = temp + os.pathsep + old
            try:
                with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                    reuse.gh_api("repos/x/y")
                gh.write_text("#!/bin/sh\necho '%s'\n" % json.dumps({"ok": True}))
                self.assertEqual(reuse.gh_api("repos/x/y"), {"ok": True})
            finally:
                os.environ["PATH"] = old


def job(name):
    """The text of job ``name`` in ci.yml."""
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    match = re.search(r"^  %s:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)" % re.escape(name), text, re.M | re.S)
    assert match, name
    return match.group(1)


def job_names():
    text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    return re.findall(r"^  ([a-z][a-z0-9-]*):\n", text.split("\njobs:\n", 1)[1], re.M)


def gate_script():
    """ci-gate's run script, dedented."""
    body = job("ci-gate").split("        run: |\n", 1)[1]
    return "".join(line[10:] if line.strip() else "\n" for line in body.splitlines(True))


class Workflow(unittest.TestCase):
    def test_reuse_step_runs_on_merge_group_only_and_fails_open(self):
        changes = job("changes")
        step = changes.split("      - id: reuse\n", 1)[1]
        self.assertIn("        if: github.event_name == 'merge_group'\n", step)
        self.assertIn("        continue-on-error: true", step, "a crash leaves reuse unset, not the gate red")
        self.assertIn("        run: python3 -I -B .github/scripts/ci_reuse.py\n", step)
        for var, value in (("HEAD_REF", "github.event.merge_group.head_ref"),
                           ("HEAD_SHA", "github.event.merge_group.head_sha"),
                           ("BASE_SHA", "github.event.merge_group.base_sha"),
                           ("EVENT", "github.event_name"), ("GH_TOKEN", "github.token")):
            self.assertIn("          %s: ${{ %s }}\n" % (var, value), step)
        self.assertIn("      reuse: ${{ steps.reuse.outputs.reuse }}\n", changes)
        self.assertIn("      reused-run: ${{ steps.reuse.outputs.run }}\n", changes)
        self.assertEqual(changes.count("ci_reuse.py"), 1)

    def test_permissions_are_minimal(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("\npermissions:\n  contents: read\n\n", text.split("\njobs:\n", 1)[0])
        self.assertNotRegex(text, r":\s*write\b")
        perms = re.search(r"^    permissions:\n((?:      .+\n)+)", job("changes"), re.M).group(1)
        granted = dict(re.findall(r"^      ([\w-]+): (\w+)", perms, re.M))
        self.assertEqual(granted, {"contents": "read", "actions": "read", "pull-requests": "read"})
        for name in job_names():
            if name != "changes":
                with self.subTest(job=name):
                    self.assertNotRegex(job(name), r"(actions|pull-requests): ")

    def test_every_heavy_job_skips_on_reuse_and_nothing_else_does(self):
        gated = {name for name in job_names() if "needs.changes.outputs.reuse != 'true'" in job(name)}
        self.assertEqual(gated, {"unit", "wsl", "connected-failure"})
        for name in gated:
            with self.subTest(job=name):
                condition = re.search(r"^    if: (>-\n(?:      .+\n)+|.+\n)", job(name), re.M).group(1)
                self.assertIn("needs.changes.outputs.plugin == 'true' && needs.changes.outputs.reuse != 'true'",
                              " ".join(condition.split()))
        # native-host narrows its matrix instead (ci_workflow_regressions.py
        # checks the legs); the cheap always-on jobs never look at reuse.
        self.assertNotIn("reuse != 'true'", job("native-host"))
        self.assertEqual(job("native-host").count("needs.changes.outputs.reuse == 'true'"), 3)
        for name in ("runtime-provenance", "release-channel"):
            self.assertNotIn("reuse", job(name))

    def test_ci_gate_accepts_reuse_only_on_merge_group_and_not_for_native_host(self):
        gate = job("ci-gate")
        self.assertIn("          REUSE: ${{ github.event_name == 'merge_group' && needs.changes.outputs.reuse "
                      "|| 'false' }}\n", gate)
        script = gate_script()
        self.assertIn('accept "$NATIVE_HOST" || exit 1\n', script)
        self.assertIn('for result in "$UNIT" "$WSL"; do\n  accept "$result" "$REUSE" || exit 1\n', script)
        self.assertIn('if ! accept "$CONNECTED" "$REUSE"; then\n', script)
        self.assertNotIn('"$NATIVE_HOST" "$REUSE"', script)

    @unittest.skipIf(os.name == "nt", "the gate runs under Linux bash")
    def test_ci_gate_script_decisions(self):
        """Run ci-gate's script as Actions does (bash -eo pipefail)."""
        reused = {"CHANGES": "success", "PLUGIN_AFFECTED": "true", "UNIT": "skipped", "NATIVE_HOST": "success",
                  "WSL": "skipped", "PROVENANCE": "success", "CHANNEL": "success", "CONNECTED": "skipped",
                  "FORK": "false", "REUSE": "true", "REUSED_RUN": RUN_URL}
        cases = [
            ({}, 0),
            ({"REUSE": "false"}, 1),
            ({"REUSE": ""}, 1),
            ({"NATIVE_HOST": "skipped"}, 1),
            ({"UNIT": "failure"}, 1),
            ({"WSL": "cancelled"}, 1),
            ({"CONNECTED": "failure"}, 1),
            ({"CHANGES": "failure"}, 1),
            ({"PROVENANCE": "skipped"}, 1),
            ({"CHANNEL": "failure"}, 1),
            # A full run is accepted as today.
            ({"REUSE": "false", "UNIT": "success", "WSL": "success", "CONNECTED": "success"}, 0),
            ({"REUSE": "false", "PLUGIN_AFFECTED": "false", "NATIVE_HOST": "skipped"}, 0),
        ]
        script = gate_script()
        for change, status in cases:
            with self.subTest(**change):
                env = dict(os.environ, **dict(reused, **change))
                completed = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                                           cwd=str(ROOT), env=env, capture_output=True, text=True)
                self.assertEqual(completed.returncode, status, completed.stdout + completed.stderr)

    def test_reuse_checks_the_job_names_ci_yml_gives(self):
        names = []
        for name in job_names():
            names.append(re.search(r"^    name: (.+)$", job(name), re.M).group(1))
        self.assertIn(reuse.GATE_JOB, names)
        for prefix in reuse.HEAVY_JOB_PREFIXES:
            with self.subTest(prefix=prefix):
                self.assertEqual(sum(name.startswith(prefix) for name in names), 1, names)
        self.assertEqual(reuse.WORKFLOW_PATH, ".github/workflows/ci.yml")
        self.assertTrue((ROOT / reuse.WORKFLOW_PATH).is_file())


if __name__ == "__main__":
    unittest.main()
