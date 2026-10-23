"""CI path filtering (#260): which changes skip the plugin
matrix, and the gates accept a skip only when the changes job succeeded and
said the area is unaffected (.github/scripts/ci_paths.py)."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402,F401  (scrubs ORK_METER_*)


paths = load(".github/scripts/ci_paths.py")
bump = load(".github/scripts/check-version-bump.py")
SCRIPT = ROOT / ".github/scripts/ci_paths.py"


def areas(*changed):
    return paths.classify(list(changed))


class Classify(unittest.TestCase):

    def test_root_docs_only_skip_plugin(self):
        for changed in (["README.md"], ["CONTRIBUTING.md", "SECURITY.md", "LICENSE"]):
            with self.subTest(changed=changed):
                self.assertEqual(areas(*changed), {"plugin": False})

    def test_docs_run_plugin(self):
        # recovery_regressions.py reads docs/*.md, native_skill_qualification.py
        # docs/optional-agents/explore.md.
        for changed in ("docs/release.md", "docs/optional-agents/explore.md"):
            with self.subTest(changed=changed):
                self.assertEqual(areas(changed), {"plugin": True})
        self.assertEqual(areas("README.md", "docs/release.md")["plugin"], True)

    def test_nested_markdown_is_not_a_root_doc(self):
        for changed in ("skills/explore/SKILL.md", "contracts/README.md", "tests/README.md"):
            with self.subTest(changed=changed):
                self.assertTrue(areas(changed)["plugin"])

    def test_hooks_run_plugin(self):
        for changed in (["hooks/read-guard.py"], ["hooks/lib/hook.py", "README.md"],
                        ["hooks/hooks.json", "LICENSE"]):
            with self.subTest(changed=changed):
                self.assertEqual(areas(*changed), {"plugin": True})

    def test_plugin_tree_runs_plugin(self):
        for changed in ("scripts/source.py", "skills/explore/SKILL.md", ".claude-plugin/plugin.json",
                        "agents/helper.md", "requirements-hooks.txt", ".gitattributes"):
            with self.subTest(changed=changed):
                self.assertEqual(areas(changed), {"plugin": True})


    def test_plugin_only_changes_run_plugin(self):
        for changed in ("tests/hook_regressions.py", "contracts/check.py", "docs/release.md"):
            with self.subTest(changed=changed):
                self.assertEqual(areas(changed), {"plugin": True})

    def test_workflow_or_ci_script_change_runs_everything(self):
        for changed in (".github/workflows/ci.yml", ".github/workflows/publish-release.yml",
                        ".github/scripts/ci_paths.py"):
            with self.subTest(changed=changed):
                self.assertEqual(areas(changed, "README.md"), {"plugin": True})

    def test_empty_or_unknown_runs_everything(self):
        everything = {"plugin": True}
        self.assertEqual(paths.classify(None), everything)
        self.assertEqual(paths.classify([]), everything)
        self.assertEqual(paths.classify([""]), everything)

    def test_cli_unknown_base_runs_everything(self):
        for base in ("", "0" * 40):
            with self.subTest(base=base), tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "out"
                env = dict(os.environ, GITHUB_OUTPUT=str(output))
                completed = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "changes", "--base", base],
                                           cwd=str(ROOT), env=env, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(output.read_text(encoding="utf-8"), "plugin=true\n")

    def test_cli_diffs_against_the_base(self):
        with tempfile.TemporaryDirectory() as temp:
            def git(*args):
                return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                       "-c", "commit.gpgsign=false", *args], cwd=temp, check=True,
                                      capture_output=True, text=True).stdout.strip()
            git("init", "-q")
            Path(temp, "README.md").write_text("one\n")
            git("add", ".")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD")
            Path(temp, "LICENSE").write_text("l\n")
            Path(temp, "README.md").write_text("two\n")
            git("add", ".")
            git("commit", "-qm", "change")
            output = Path(temp) / "out"
            env = dict(os.environ, GITHUB_OUTPUT=str(output))
            completed = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "changes", "--base", base],
                                       cwd=temp, env=env, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "plugin=false\n")


class Gates(unittest.TestCase):
    def test_success_always_passes(self):
        for changes in ("success", "failure", "cancelled", "skipped"):
            self.assertTrue(paths.accept("success", changes, "true"))

    def test_skip_needs_a_successful_changes_job_saying_unaffected(self):
        self.assertTrue(paths.accept("skipped", "success", "false"))
        self.assertFalse(paths.accept("skipped", "success", "true"))
        self.assertFalse(paths.accept("skipped", "success", ""))
        for changes in ("failure", "cancelled", "skipped", ""):
            with self.subTest(changes=changes):
                self.assertFalse(paths.accept("skipped", changes, "false"))

    def test_failure_or_cancel_never_passes(self):
        for result in ("failure", "cancelled", ""):
            self.assertFalse(paths.accept(result, "success", "false"))
            self.assertFalse(paths.accept(result, "success", "true", "true"))

    def test_skip_on_reuse_needs_a_successful_changes_job_saying_reuse(self):
        """#289: a merge group reusing its PR's run skips the plugin jobs."""
        self.assertTrue(paths.accept("skipped", "success", "true", "true"))
        for reused in ("false", "", "TRUE", "1"):
            with self.subTest(reused=reused):
                self.assertFalse(paths.accept("skipped", "success", "true", reused))
        for changes in ("failure", "cancelled", "skipped", ""):
            with self.subTest(changes=changes):
                self.assertFalse(paths.accept("skipped", changes, "true", "true"))

    def test_cli_exit_status(self):
        def run(*args):
            return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "accept", *args],
                                  capture_output=True, text=True).returncode
        self.assertEqual(run("skipped", "success", "false"), 0)
        self.assertEqual(run("skipped", "failure", "false"), 1)
        self.assertEqual(run("failure", "success", "true"), 1)
        self.assertEqual(run("skipped", "success", "true"), 1)
        self.assertEqual(run("skipped", "success", "true", "--reused", "false"), 1)
        self.assertEqual(run("skipped", "success", "true", "--reused", "true"), 0)
        self.assertEqual(run("skipped", "failure", "true", "--reused", "true"), 1)


def job(workflow, name):
    """The text of job ``name`` in a workflow file."""
    text = (ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8")
    match = re.search(r"^  %s:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|\Z)" % re.escape(name), text, re.M | re.S)
    assert match, (workflow, name)
    return match.group(1)


class Workflows(unittest.TestCase):
    def test_no_workflow_level_paths_filter(self):
        for workflow in ("ci.yml", "package-runtime.yml", "publish-release.yml"):
            text = (ROOT / ".github/workflows" / workflow).read_text(encoding="utf-8")
            on = text.split("\njobs:", 1)[0]
            with self.subTest(workflow=workflow):
                self.assertNotRegex(on, r"\bpaths(-ignore)?:")

    def test_changes_jobs_diff_against_the_event_base(self):
        for workflow, area in (("ci.yml", "plugin"),):
            body = job(workflow, "changes")
            with self.subTest(workflow=workflow):
                self.assertIn("github.event.pull_request.base.sha || github.event.merge_group.base_sha", body)
                self.assertIn("fetch-depth: 0", body)
                self.assertIn("ci_paths.py changes", body)
                self.assertIn("%s: ${{ steps.paths.outputs.%s }}" % (area, area), body)

    def test_heavy_jobs_depend_on_changes(self):
        for workflow, name, area in (("ci.yml", "unit", "plugin"), ("ci.yml", "native-host", "plugin"),
                                     ("ci.yml", "wsl", "plugin"), ("ci.yml", "connected-failure", "plugin")):
            body = job(workflow, name)
            with self.subTest(job=name):
                self.assertIn("needs: changes", body)
                self.assertIn("needs.changes.outputs.%s == 'true'" % area, body)

    def test_always_on_jobs_are_not_filtered(self):
        for name in ("runtime-provenance", "release-channel"):
            self.assertNotIn("needs.changes", job("ci.yml", name))

    def test_only_the_heavy_jobs_are_filtered(self):
        text = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        names = re.findall(r"^  ([a-z][a-z0-9-]*):\n", text.split("\njobs:\n", 1)[1], re.M)
        filtered = {name for name in names if "needs.changes.outputs.plugin == 'true'" in job("ci.yml", name)}
        self.assertEqual(filtered, {"unit", "native-host", "wsl", "connected-failure"})

    def test_gates_always_run_and_require_changes_success(self):
        for workflow, gate in (("ci.yml", "ci-gate"),):
            body = job(workflow, gate)
            with self.subTest(gate=gate):
                self.assertIn("if: always()", body)
                self.assertRegex(body, r"needs: \[changes, ")
                self.assertIn("CHANGES: ${{ needs.changes.result }}", body)
                self.assertRegex(body, r'\[ "\$(CHANGES|result)" = "success" \] \|\| exit 1')
                self.assertIn("ci_paths.py accept", body)
                self.assertIn('"$CHANGES"', body.split("ci_paths.py accept", 1)[1].splitlines()[0])

    def test_ci_gate_accepts_every_filtered_job_only_through_ci_paths(self):
        """Each job gated on changes reaches accept(); the others must succeed."""
        body = job("ci.yml", "ci-gate")
        env = dict(re.findall(r"^\s+([A-Z_]+): \$\{\{ needs\.([\w-]+)\.result \}\}$", body, re.M))
        by_job = {name: var for var, name in env.items()}
        script = body.split("run: |", 1)[1]
        loops = re.findall(r"for result in ([^;]+); do\n\s+(.+)\n", script)
        strict = {v for vars_, action in loops if "exit 1" in action and "accept" not in action
                  for v in re.findall(r'"\$(\w+)"', vars_)}
        accepted = {v for vars_, action in loops if action.startswith("accept")
                    for v in re.findall(r'"\$(\w+)"', vars_)}
        accepted |= set(re.findall(r'^\s+(?:if ! )?accept "\$(\w+)"', script, re.M)) - {"result"}
        self.assertEqual(strict, {by_job["changes"], by_job["runtime-provenance"], by_job["release-channel"]})
        self.assertEqual(accepted, {by_job["unit"], by_job["native-host"], by_job["wsl"],
                                    by_job["connected-failure"]})
        self.assertIn('accept() { python3 -I -B .github/scripts/ci_paths.py accept "$1" "$CHANGES" '
                      '"$PLUGIN_AFFECTED" --reused "${2:-false}"; }', script)

if __name__ == "__main__":
    unittest.main()
