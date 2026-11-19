"""A release's version bump and nothing else (.github/scripts/ci_bump.py): the
only change that lets the unit and native-host matrices skip, and every doubt
runs them in full."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402,F401  (scrubs ORK_METER_*)

bump = load(".github/scripts/ci_bump.py")
SCRIPT = ROOT / ".github/scripts/ci_bump.py"
MANIFEST = ".claude-plugin/plugin.json"


def manifest(version="0.5.8", **extra):
    data = {"name": "ork-meter", "version": version, "license": "x", "hooks": "./hooks/hooks.json"}
    data.update(extra)
    return json.dumps(data, indent=2) + "\n"


class VersionBumpOnly(unittest.TestCase):
    def test_a_new_plain_version_and_nothing_else_is_a_bump(self):
        self.assertTrue(bump.version_bump_only([MANIFEST], manifest("0.5.8"), manifest("0.5.9")))
        self.assertTrue(bump.version_bump_only([MANIFEST], manifest("0.5.9"), manifest("1.0.0")))

    def test_the_real_manifest_bumped_is_a_bump(self):
        text = (ROOT / MANIFEST).read_text(encoding="utf-8")
        version = json.loads(text)["version"]
        self.assertEqual(text.count('"version": "%s"' % version), 1)
        self.assertTrue(bump.version_bump_only([MANIFEST], text, text.replace(
            '"version": "%s"' % version, '"version": "999.0.0"')))

    def test_any_other_path_is_not_a_bump(self):
        for paths in ([], None, [MANIFEST, "hooks/read-guard.py"], ["hooks/read-guard.py"],
                      [MANIFEST, "runtime/linux-amd64/SHA256"], [MANIFEST, "README.md"],
                      [".claude-plugin/marketplace.json"], [MANIFEST, MANIFEST]):
            with self.subTest(paths=paths):
                self.assertFalse(bump.version_bump_only(paths, manifest("0.5.8"), manifest("0.5.9")))

    def test_a_change_beside_the_version_is_not_a_bump(self):
        for new in (manifest("0.5.9", hooks="./other.json"), manifest("0.5.9", extra=True),
                    manifest("0.5.9").replace('"license": "x"', '"license": "y"'),
                    json.dumps({"version": "0.5.9"}) + "\n"):
            with self.subTest(new=new):
                self.assertFalse(bump.version_bump_only([MANIFEST], manifest("0.5.8"), new))

    def test_an_unchanged_malformed_or_prerelease_version_is_not_a_bump(self):
        for version in ("0.5.8", "0.5.9-rc.1", "0.5", "v0.5.9", "0.5.9 ", "", "0.5.9.1", "00000.1.1"):
            with self.subTest(version=version):
                self.assertFalse(bump.version_bump_only([MANIFEST], manifest("0.5.8"), manifest(version)))
        self.assertFalse(bump.version_bump_only([MANIFEST], manifest("0.5.8"), manifest("0.5.9").replace('"0.5.9"', "3")))

    def test_reformatting_beside_the_bump_is_not_a_bump(self):
        """Equal JSON, but more than the version line changed: run in full."""
        old = manifest("0.5.8")
        self.assertFalse(bump.version_bump_only([MANIFEST], old, json.dumps(json.loads(manifest("0.5.9")), indent=4) + "\n"))
        self.assertFalse(bump.version_bump_only([MANIFEST], old, manifest("0.5.9") + "\n"))
        self.assertFalse(bump.version_bump_only([MANIFEST], old, manifest("0.5.9").replace('"license": "x"', '"license":  "x"')))

    def test_unreadable_manifests_are_not_a_bump(self):
        for old, new in ((None, manifest("0.5.9")), (manifest("0.5.8"), None), ("{not json", manifest("0.5.9")),
                         (manifest("0.5.8"), "[1]"), ("[]", "[]"), (b"{}", manifest("0.5.9"))):
            with self.subTest(old=old, new=new):
                self.assertFalse(bump.version_bump_only([MANIFEST], old, new))


class Accept(unittest.TestCase):
    def test_only_a_skip_under_a_successful_changes_job_saying_bump(self):
        self.assertTrue(bump.accept("skipped", "success", "true"))
        for result, changes, flag in (("skipped", "success", "false"), ("skipped", "success", ""),
                                      ("skipped", "failure", "true"), ("skipped", "cancelled", "true"),
                                      ("skipped", "skipped", "true"), ("failure", "success", "true"),
                                      ("cancelled", "success", "true"), ("success", "success", "true"),
                                      ("", "success", "true"), ("skipped", "success", "True")):
            with self.subTest(result=result, changes=changes, flag=flag):
                self.assertFalse(bump.accept(result, changes, flag))

    def test_cli_exit_status(self):
        for args, status in ((("skipped", "success", "true"), 0), (("skipped", "success", "false"), 1),
                             (("failure", "success", "true"), 1)):
            with self.subTest(args=args):
                completed = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "accept", *args],
                                           cwd=str(ROOT), capture_output=True, text=True)
                self.assertEqual(completed.returncode, status, completed.stdout + completed.stderr)


class Detect(unittest.TestCase):
    def repo(self, temp):
        def git(*args):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                   "-c", "commit.gpgsign=false", *args], cwd=temp, check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        Path(temp, ".claude-plugin").mkdir()
        Path(temp, MANIFEST).write_text(manifest("0.5.8"), encoding="utf-8")
        Path(temp, "hook.py").write_text("one\n", encoding="utf-8")
        git("add", ".")
        git("commit", "-qm", "base")
        return git, git("rev-parse", "HEAD")

    def run_detect(self, temp, base):
        output = Path(temp) / "out"
        output.unlink(missing_ok=True)  # the script appends, as $GITHUB_OUTPUT expects
        env = dict(os.environ, GITHUB_OUTPUT=str(output))
        completed = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "detect", "--base", base],
                                   cwd=temp, env=env, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return output.read_text(encoding="utf-8")

    def test_a_version_bump_commit_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            git, base = self.repo(temp)
            Path(temp, MANIFEST).write_text(manifest("0.5.9"), encoding="utf-8")
            git("commit", "-qam", "release 0.5.9")
            self.assertEqual(self.run_detect(temp, base), "bump=true\n")

    def test_a_bump_with_any_other_change_runs_in_full(self):
        with tempfile.TemporaryDirectory() as temp:
            git, base = self.repo(temp)
            Path(temp, MANIFEST).write_text(manifest("0.5.9"), encoding="utf-8")
            Path(temp, "hook.py").write_text("two\n", encoding="utf-8")
            git("commit", "-qam", "release and a fix")
            self.assertEqual(self.run_detect(temp, base), "bump=false\n")

    def test_no_change_an_unknown_base_or_no_repository_runs_in_full(self):
        with tempfile.TemporaryDirectory() as temp:
            _, base = self.repo(temp)
            for value in (base, "", "0" * 40, "not-a-sha"):
                with self.subTest(base=value):
                    self.assertEqual(self.run_detect(temp, value), "bump=false\n")
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(self.run_detect(temp, "0" * 40), "bump=false\n")

    def test_detect_never_raises(self):
        self.assertEqual(bump.detect(""), (False, "no base commit"))
        self.assertFalse(bump.detect("0" * 40)[0])


if __name__ == "__main__":
    unittest.main()
