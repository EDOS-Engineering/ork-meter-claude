"""The release channel: version bumps after a release, and the marketplace pinned to a release tag (#142)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402

scripts = ROOT / ".github/scripts"
bump = load(".github/scripts/check-version-bump.py")
pointer = load(".github/scripts/point-marketplace.py")


def git(root, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.check_output(["git", "-c", "init.defaultBranch=main", "-c", "tag.gpgSign=false",
                                    "-c", "commit.gpgSign=false", *args], cwd=root, env=env, text=True).strip()


class Repo:
    """A throwaway plugin repository with a plugin.json, a hook, docs and a marketplace."""

    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest = {"name": "ork-meter", "version": "0.3.0", "description": "d"}
        self.write(".claude-plugin/plugin.json", json.dumps(self.manifest, indent=2) + "\n")
        self.write(".claude-plugin/marketplace.json", json.dumps(
            {"name": "edos", "owner": {"name": "EDOS"}, "plugins": [{"name": "ork-meter", "source": "./"}]},
            indent=2) + "\n")
        self.write("hooks/hook.py", "print('v1')\n")
        self.write("docs/notes.md", "notes\n")
        git(self.root, "init", "-q")
        self.commit("base")

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def set_manifest(self, **fields):
        self.manifest.update(fields)
        self.write(".claude-plugin/plugin.json", json.dumps(self.manifest, indent=2) + "\n")

    def commit(self, message):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def tag(self, name):
        git(self.root, "tag", "-a", name, "-m", name)

    def run(self, script, *args):
        return subprocess.run([sys.executable, "-I", "-B", str(scripts / script), *args],
                              cwd=self.root, capture_output=True, text=True)


class Versions(unittest.TestCase):
    def test_semver_precedence(self):
        ordered = ["0.2.9", "0.3.0-1", "0.3.0-dev", "0.3.0-dev.1", "0.3.0-dev.2", "0.3.0-dev.10",
                   "0.3.0-rc.1", "0.3.0", "0.3.1", "0.4.0-dev", "0.4.0", "1.0.0"]
        keys = [bump.parse(v) for v in ordered]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), len(keys))

    def test_versions_outside_the_readers_rule_are_refused(self):
        for version in ("0.3", "v0.3.0", "0.3.0+build.1", "01.3.0", "0.3.0-", "0.3.0-dev.",
                        "0.3.0-dev..1", "0.3.0-01", "0.3.0-dev_1", "0.3.0-pre-release",
                        "0.3.0-a2345678901234567", "10000.0.0", "", None, 3):
            with self.subTest(version=version), self.assertRaises(ValueError):
                bump.parse(version)

    def test_validate(self):
        self.assertIn("unchanged", bump.validate("v0.3.0", "0.3.0", "0.3.0", False))
        self.assertIn("ok", bump.validate("v0.3.0", "0.3.0", "0.3.1", True))
        self.assertIn("ok", bump.validate("v0.3.0", "0.3.0", "0.4.0-dev", True))
        for head, changed in (("0.3.0", True), ("0.3.0-dev", True), ("0.3.0-dev", False), ("0.2.9", False)):
            with self.subTest(head=head, changed=changed), self.assertRaises(ValueError):
                bump.validate("v0.3.0", "0.3.0", head, changed)

    def test_latest_tag_is_the_highest_version_not_the_last_listed(self):
        self.assertEqual(bump.latest_tag(["v0.10.0", "v0.9.0", "v0.10.0-rc.1", "vnext", "release-1"]), "v0.10.0")
        self.assertIsNone(bump.latest_tag(["vnext", "1.0.0"]))


class VersionBumpCheck(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.temp.cleanup()

    def check(self):
        return self.repo.run("check-version-bump.py")

    def assertPasses(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def assertFails(self):
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::error::", result.stdout)
        return result.stdout

    def test_no_release_tag_passes(self):
        self.repo.write("hooks/hook.py", "print('v2')\n")
        self.repo.commit("change")
        self.assertIn("No v<version> release tag", self.assertPasses())

    def test_non_surface_change_needs_no_bump(self):
        self.repo.tag("v0.3.0")
        self.repo.write("docs/notes.md", "more notes\n")
        self.repo.write("tests/new.test.sh", "true\n")
        self.repo.commit("docs")
        self.assertIn("unchanged", self.assertPasses())

    def test_surface_change_without_bump_fails_and_with_bump_passes(self):
        self.repo.tag("v0.3.0")
        for path in ("hooks/hook.py", "scripts/runtime.sh", "skills/status/SKILL.md", "runtime/linux-amd64/SHA256",
                     ".mcp.json", "requirements-hooks.txt"):
            with self.subTest(path=path):
                git(self.repo.root, "reset", "-q", "--hard", "v0.3.0")
                self.repo.manifest["version"] = "0.3.0"
                self.repo.write(path, "changed\n")
                self.repo.commit(f"change {path}")
                self.assertIn(path, self.assertFails())
                for version in ("0.3.1", "0.4.0-dev"):
                    self.repo.set_manifest(version=version)
                    self.repo.commit(f"bump {version}")
                    self.assertPasses()

    def test_plugin_json_fields_other_than_version_are_surface(self):
        self.repo.tag("v0.3.0")
        self.repo.set_manifest(description="new")
        self.repo.commit("describe")
        self.assertIn("fields other than version", self.assertFails())

    def test_version_only_change_is_not_a_surface_change(self):
        self.repo.tag("v0.3.0")
        self.repo.set_manifest(version="0.4.0-dev")
        self.repo.commit("bump")
        self.assertIn("unchanged", self.assertPasses())

    def test_pre_release_of_the_released_version_or_a_lower_version_fails(self):
        self.repo.tag("v0.3.0")
        for version in ("0.3.0-dev", "0.2.9"):
            with self.subTest(version=version):
                self.repo.set_manifest(version=version)
                self.repo.commit(f"version {version}")
                self.assertFails()

    def test_release_commit_then_tag_then_next_change(self):
        self.repo.tag("v0.3.0")
        self.repo.write("hooks/hook.py", "print('v2')\n")
        self.repo.set_manifest(version="0.4.0-dev")
        self.repo.commit("feature")
        self.assertPasses()
        # package-runtime's release commit: runtime/ and the release version.
        self.repo.write("runtime/linux-amd64/SHA256", "1" * 64 + "\n")
        self.repo.set_manifest(version="0.4.0")
        self.repo.commit("release: plugin 0.4.0")
        self.assertPasses()
        self.repo.tag("v0.4.0")
        self.assertIn("unchanged since v0.4.0", self.assertPasses())
        self.repo.write("hooks/hook.py", "print('v3')\n")
        self.repo.commit("next change")
        self.assertIn("v0.4.0", self.assertFails())

    def test_tags_outside_the_history_are_ignored(self):
        self.repo.tag("v0.3.0")
        git(self.repo.root, "checkout", "-q", "-b", "stray")
        self.repo.set_manifest(version="9.9.9")
        self.repo.commit("stray")
        self.repo.tag("v9.9.9")
        git(self.repo.root, "checkout", "-q", "main")
        self.repo.set_manifest(version="0.3.1")
        self.repo.write("hooks/hook.py", "print('v2')\n")
        self.repo.commit("fix")
        self.assertIn("v0.3.0", self.assertPasses())

    def test_unparseable_version_fails(self):
        self.repo.set_manifest(version="0.4.0+build")
        self.repo.commit("bad")
        self.assertFails()


class MarketplacePointer(unittest.TestCase):
    SHA = "a" * 40

    def resolver(self, ref):
        return {"v0.3.0": (self.SHA, "0.3.0"), "v0.4.0": (self.SHA, "0.3.9")}[ref]

    def source(self, **fields):
        return dict(pointer.source_for("0.3.0", self.SHA), **fields)

    def test_pinned_release_is_accepted(self):
        self.assertIn("v0.3.0", pointer.validate(self.source(), self.resolver))

    def test_unpinned_or_mismatched_sources_are_refused(self):
        variants = ["./", {"source": "url", "url": pointer.URL, "ref": "v0.3.0"},
                    {"source": "github", "repo": "EDOS-Engineering/ork-meter-claude", "ref": "v0.3.0", "sha": self.SHA},
                    self.source(source="github"), self.source(url="https://github.com/someone/fork.git"),
                    self.source(url="git@github.com:EDOS-Engineering/ork-meter-claude.git"), self.source(ref="main"),
                    self.source(ref="v0.3.0+meta"), self.source(sha="A" * 40), self.source(sha="a" * 7),
                    self.source(sha="b" * 40), self.source(ref="v0.4.0"), dict(self.source(), path="x")]
        for source in variants:
            with self.subTest(source=source), self.assertRaises(ValueError):
                pointer.validate(source, self.resolver)

    def test_bootstrap_entry_is_accepted_only_before_the_first_release(self):
        entry = pointer.bootstrap_source()
        self.assertIn("installs main", pointer.validate(entry, self.resolver, released=False))
        with self.assertRaises(ValueError):
            pointer.validate(entry, self.resolver, released=True)
        for source in (dict(entry, ref="main"), dict(entry, sha=self.SHA), "./"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                pointer.validate(source, self.resolver, released=False)


class MarketplaceBootstrap(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.repo.write(".claude-plugin/marketplace.json", json.dumps(
            {"name": "edos", "owner": {"name": "EDOS"},
             "plugins": [{"name": "ork-meter", "source": pointer.bootstrap_source()}]}, indent=2) + "\n")
        self.repo.commit("bootstrap")

    def tearDown(self):
        self.repo.temp.cleanup()

    def test_check_accepts_the_bootstrap_entry_until_a_release_is_merged(self):
        result = self.repo.run("point-marketplace.py", "check")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("installs main", result.stdout)
        self.repo.tag("v0.3.0")
        result = self.repo.run("point-marketplace.py", "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pin it", result.stdout)
        self.assertEqual(self.repo.run("point-marketplace.py", "set", "0.3.0").returncode, 0)
        self.assertEqual(self.repo.run("point-marketplace.py", "check").returncode, 0)


class MarketplacePointerCommand(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.repo.tag("v0.3.0")
        self.released = git(self.repo.root, "rev-parse", "v0.3.0^{commit}")

    def tearDown(self):
        self.repo.temp.cleanup()

    def marketplace_source(self):
        text = (self.repo.root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        return json.loads(text)["plugins"][0]["source"]

    def test_set_then_check(self):
        self.assertEqual(self.repo.run("point-marketplace.py", "check").returncode, 1)
        result = self.repo.run("point-marketplace.py", "set", "0.3.0")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.marketplace_source(), {"source": "url", "url": pointer.URL,
                                                     "ref": "v0.3.0", "sha": self.released})
        self.assertEqual(self.repo.run("point-marketplace.py", "check").returncode, 0)

    def test_rollback_re_points_to_an_earlier_release(self):
        self.repo.set_manifest(version="0.4.0")
        self.repo.commit("release 0.4.0")
        self.repo.tag("v0.4.0")
        self.assertEqual(self.repo.run("point-marketplace.py", "set", "v0.4.0").returncode, 0)
        self.assertEqual(self.marketplace_source()["ref"], "v0.4.0")
        self.assertEqual(self.repo.run("point-marketplace.py", "set", "0.3.0").returncode, 0)
        self.assertEqual(self.marketplace_source()["sha"], self.released)

    def test_missing_moved_or_unmerged_tags_are_refused(self):
        self.assertEqual(self.repo.run("point-marketplace.py", "set", "0.3.0").returncode, 0)
        self.repo.commit("point at v0.3.0")
        before = (self.repo.root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        self.assertEqual(self.repo.run("point-marketplace.py", "set", "0.9.0").returncode, 1)
        git(self.repo.root, "checkout", "-q", "-b", "side")
        self.repo.set_manifest(version="0.5.0")
        self.repo.commit("side")
        self.repo.tag("v0.5.0")
        git(self.repo.root, "checkout", "-q", "main")
        self.assertEqual(self.repo.run("point-marketplace.py", "set", "0.5.0").returncode, 1)
        self.assertEqual((self.repo.root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"), before)
        # A tag moved after the pointer was written no longer matches its sha.
        self.repo.commit("later")
        git(self.repo.root, "tag", "-f", "-a", "v0.3.0", "-m", "moved")
        result = self.repo.run("point-marketplace.py", "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("pins sha", result.stdout)

    def test_tag_whose_plugin_json_disagrees_is_refused(self):
        self.repo.commit("no bump")
        self.repo.tag("v0.3.1")
        result = self.repo.run("point-marketplace.py", "set", "0.3.1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not 0.3.1", result.stdout)


if __name__ == "__main__":
    unittest.main()
