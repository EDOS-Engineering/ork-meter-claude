"""The release targets and runtime binary name agree everywhere (#191).

package_runtime.py, build-release-assets.py and verify-runtime-attestations.py
import them from .github/scripts/release_targets.py; check-release-commit.py
(standalone) and scripts/runtime.sh (shell) spell them out, so they are
checked here."""
import itertools
from pathlib import Path
import sys
import re
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402


targets = load(".github/scripts/release_targets.py")


class ReleaseTargets(unittest.TestCase):
    def test_python_scripts_share_one_definition(self):
        for path in ("scripts/package_runtime.py", ".github/scripts/build-release-assets.py",
                     ".github/scripts/verify-runtime-attestations.py"):
            module = load(path)
            with self.subTest(path=path):
                self.assertEqual(module.TARGETS, targets.TARGETS)
                self.assertEqual(module.BINARY, targets.BINARY)
                self.assertEqual([module.binary_name(t) for t in targets.TARGETS],
                                 [targets.binary_name(t) for t in targets.TARGETS])
        self.assertEqual(targets.binary_name("windows-amd64"), "ork-meter-plugin-runtime.exe")
        self.assertEqual(targets.binary_name("linux-arm64"), "ork-meter-plugin-runtime")

    def test_runtime_sh_lists_the_same_targets_and_binary(self):
        text = (ROOT / "scripts/runtime.sh").read_text(encoding="utf-8")
        cases = re.search(r'case "\$os-\$arch" in\n\s*([a-z0-9|-]+)\) ;;', text)
        self.assertIsNotNone(cases)
        self.assertEqual(set(cases.group(1).split("|")), set(targets.TARGETS))
        self.assertIn("\nname=%s\n" % targets.BINARY, text)

    def test_release_commit_allows_every_target_file_and_no_other_platform(self):
        commit = load(".github/scripts/check-release-commit.py")
        for target in targets.TARGETS:
            for name in (targets.binary_name(target), "SHA256"):
                self.assertTrue(commit.ALLOWED.match("runtime/%s/%s" % (target, name)), (target, name))
        systems = {t.split("-")[0] for t in targets.TARGETS}
        arches = {t.split("-")[1] for t in targets.TARGETS}
        for system, arch in itertools.product(("darwin", "linux", "windows", "freebsd"), ("amd64", "arm64", "386")):
            allowed = bool(commit.ALLOWED.match("runtime/%s-%s/SHA256" % (system, arch)))
            self.assertEqual(allowed, system in systems and arch in arches, (system, arch))
        self.assertFalse(commit.ALLOWED.match("runtime/linux-amd64/other-binary"))

    def test_semver(self):
        for good in ("0.3.0", "1.2.3-rc.1", "0.4.0-dev", "1.0.0+build.5"):
            self.assertTrue(targets.SEMVER.match(good), good)
        for bad in ("1.2", "01.2.3", "1.2.3-", "v1.2.3"):
            self.assertFalse(targets.SEMVER.match(bad), bad)

    def test_workflows_agree(self):
        workflow = (ROOT / ".github/workflows/package-runtime.yml").read_text(encoding="utf-8")
        listed = re.search(r"--targets ([a-z0-9 -]+?) \\\n", workflow)
        self.assertIsNotNone(listed, "package-runtime.yml passes --targets")
        self.assertEqual(listed.group(1).split(), list(targets.TARGETS))
        commit = load(".github/scripts/check-release-commit.py")
        staged = re.search(r"grep -Ev '(\^\(runtime/.*?\)\$)'", workflow)
        self.assertIsNotNone(staged)
        self.assertEqual(staged.group(1), commit.ALLOWED.pattern, "the workflow's staged-file check is ALLOWED")

    def test_workflow_semver_greps(self):
        """The workflows' shell check is a looser front door; the build itself
        (package_runtime --plugin-version) applies SEMVER. Every version
        SEMVER accepts passes the grep, and the grep's extra admissions are
        exactly the known ones below."""
        greps = set()
        for name in ("package-runtime.yml", "publish-release.yml"):
            text = (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
            greps.update(re.findall(r"grep -Eq '(\^\(0\|.*?\$)'", text))
        self.assertEqual(len(greps), 1, greps)
        grep = re.compile(greps.pop())
        accepted = ("0.3.0", "1.2.3-rc.1", "0.4.0-dev", "1.0.0+build.5", "1.0.0-x-y.7+z", "10.20.30")
        refused = ("1.2", "01.2.3", "v1.2.3", "1.2.3-", "1.2.3+", " 1.2.3")
        looser = ("1.2.3-01", "1.2.3-a..b", "1.2.3+a..b", "1.2.3-.")   # grep yes, SEMVER no
        for version in accepted:
            self.assertTrue(targets.SEMVER.match(version) and grep.match(version), version)
        for version in refused:
            self.assertFalse(targets.SEMVER.match(version) or grep.match(version), version)
        for version in looser:
            self.assertTrue(grep.match(version) and not targets.SEMVER.match(version), version)

    def test_shared_module_is_on_the_version_bump_surface(self):
        bump = load(".github/scripts/check-version-bump.py")
        self.assertIn(".github/scripts/release_targets.py", bump.SURFACE)
        self.assertIn("scripts", bump.SURFACE)


if __name__ == "__main__":
    unittest.main()
