"""publish-release accepts a release tag only if an allowed signer SSH-signed it (#31).

Each test makes throwaway ed25519 keys and a throwaway repository; nothing
touches the developer's keys or git config. Skipped where OpenSSH has no
`ssh-keygen -Y` (before 8.2) or git cannot sign with SSH (before 2.34), and on
native Windows, where publish-release never runs this check."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402

signature = load(".github/scripts/check-tag-signature.py")

RELEASER = "releaser@example.invalid"


def ssh_signing_available():
    if os.name == "nt" or not shutil.which("ssh-keygen") or not shutil.which("git"):
        return False
    probe = subprocess.run(["ssh-keygen", "-Y", "check-novalidate", "-n", "git", "-s", os.devnull],
                           input=b"", capture_output=True)
    if b"illegal option" in probe.stderr or b"unknown option" in probe.stderr:
        return False
    version = re.search(rb"(\d+)\.(\d+)", subprocess.run(["git", "--version"], capture_output=True).stdout)
    return bool(version) and (int(version.group(1)), int(version.group(2))) >= (2, 34)


class Repo:
    """A throwaway repository with one commit, and keys to sign tags with."""

    def __init__(self, base):
        self.base = Path(base)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.keys = {}
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "release: plugin 9.9.9 with runtime from ork-meter v1.2.3")
        self.commit = self.git("rev-parse", "HEAD")

    def env(self, email=RELEASER):
        return dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                    GIT_AUTHOR_NAME="Releaser", GIT_AUTHOR_EMAIL=email,
                    GIT_COMMITTER_NAME="Releaser", GIT_COMMITTER_EMAIL=email)

    def git(self, *args, email=RELEASER, data=None):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env(email), input=data,
                              capture_output=True, text=True, check=True).stdout.strip()

    def key(self, name):
        if name not in self.keys:
            path = self.base / name
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)],
                           check=True, stdin=subprocess.DEVNULL)
            self.keys[name] = path
        return self.keys[name]

    def public(self, name):
        return " ".join(Path(str(self.key(name)) + ".pub").read_text().split()[:2])

    def signers(self, *lines):
        path = self.base / "allowed_signers"
        path.write_text("# test signers\n" + "".join(line + "\n" for line in lines))
        return path

    def raw_signed_tag(self, tag, key, tagger, trailer=b""):
        """refs/tags/<tag> as a tag object with exactly this tagger header,
        SSH-signed by ``key`` and followed by ``trailer``: what a forger can
        build without git's checks."""
        payload = (f"object {self.commit}\ntype commit\ntag {tag}\ntagger {tagger} 1700000000 +0000\n\n"
                   f"Ork Meter plugin {tag}\n").encode()
        signed = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(self.key(key)), "-n", "git"],
                                input=payload, capture_output=True, check=True).stdout
        obj = subprocess.run(["git", "hash-object", "-t", "tag", "-w", "--literally", "--stdin"], cwd=self.root,
                             env=self.env(), input=payload + signed + trailer, capture_output=True, check=True).stdout.decode().strip()
        self.git("update-ref", f"refs/tags/{tag}", obj)

    def signed_tag(self, tag, key, email=RELEASER, target="HEAD"):
        self.git("-c", "gpg.format=ssh", "-c", f"user.signingkey={self.key(key)}",
                 "tag", "-s", tag, target, "-m", f"Ork Meter plugin {tag}", email=email)


@unittest.skipUnless(ssh_signing_available(), "needs OpenSSH 8.2+ ssh-keygen -Y and git 2.34+, not native Windows")
class TagSignature(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repo(self.temp.name)
        self.allowed = self.repo.signers(f'{RELEASER} namespaces="git" {self.repo.public("releaser")}')

    def tearDown(self):
        self.temp.cleanup()

    def check(self, tag="v9.9.9", commit=None, allowed=None):
        return signature.check(self.repo.root, tag, commit or self.repo.commit, allowed or self.allowed)

    def main(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return signature.main(["--tag", "v9.9.9", "--commit", self.repo.commit,
                                   "--allowed-signers", str(self.allowed), "--repo", str(self.repo.root)])

    def refused(self, pattern, **kwargs):
        with self.assertRaisesRegex(signature.Refused, pattern):
            self.check(**kwargs)

    def test_a_tag_signed_by_an_allowed_signer_for_its_tagger_is_accepted(self):
        self.repo.signed_tag("v9.9.9", "releaser")
        principal, detail = self.check()
        self.assertEqual(principal, RELEASER)
        self.assertIn("Good", detail)
        self.assertEqual(self.main(), 0)

    def test_an_unsigned_annotated_tag_is_refused(self):
        self.repo.git("tag", "-a", "v9.9.9", "-m", "unsigned")
        self.refused("not signed")
        self.assertEqual(self.main(), 1)

    def test_a_lightweight_tag_is_refused(self):
        self.repo.git("tag", "v9.9.9")
        self.refused("lightweight")

    def test_a_missing_tag_is_refused(self):
        self.refused("does not exist")

    def test_a_key_not_in_allowed_signers_is_refused(self):
        self.repo.signed_tag("v9.9.9", "stranger")
        self.refused("not by a key")

    def test_an_allowed_key_used_under_another_tagger_email_is_refused(self):
        self.repo.signed_tag("v9.9.9", "releaser", email="someone-else@example.invalid")
        self.refused("not by a key .* someone-else@example.invalid")

    def test_an_allowed_signer_listed_only_for_another_email_is_refused(self):
        allowed = self.repo.signers(f'other@example.invalid namespaces="git" {self.repo.public("releaser")}')
        self.repo.signed_tag("v9.9.9", "releaser")
        self.refused("not by a key", allowed=allowed)

    def test_an_expired_signer_is_refused(self):
        allowed = self.repo.signers(
            f'{RELEASER} namespaces="git",valid-before="20200101" {self.repo.public("releaser")}')
        self.repo.signed_tag("v9.9.9", "releaser")
        self.refused("not by a key", allowed=allowed)

    def test_a_signed_tag_object_reused_under_another_name_is_refused(self):
        self.repo.signed_tag("v9.9.8", "releaser")
        tag_object = self.repo.git("rev-parse", "refs/tags/v9.9.8")
        self.repo.git("update-ref", "refs/tags/v9.9.9", tag_object)
        self.refused("named 'v9.9.8'")

    def test_a_signed_tag_on_another_commit_is_refused(self):
        self.repo.signed_tag("v9.9.9", "releaser")
        self.refused("not the release commit", commit="0" * 40)

    def test_an_openpgp_signature_block_is_refused(self):
        body = (f"object {self.repo.commit}\ntype commit\ntag v9.9.9\n"
                f"tagger Releaser <{RELEASER}> 1700000000 +0000\n\nm\n"
                "-----BEGIN PGP SIGNATURE-----\n\nAAAA\n-----END PGP SIGNATURE-----\n")
        tag_object = self.repo.git("mktag", data=body)
        self.repo.git("update-ref", "refs/tags/v9.9.9", tag_object)
        self.refused("OpenPGP")

    def test_a_tampered_signed_tag_is_refused(self):
        self.repo.signed_tag("v9.9.8", "releaser")
        raw = self.repo.git("cat-file", "tag", "v9.9.8") + "\n"
        forged = raw.replace("tag v9.9.8", "tag v9.9.9").replace("Ork Meter plugin v9.9.8", "Ork Meter plugin v9.9.9")
        tag_object = self.repo.git("hash-object", "-t", "tag", "-w", "--stdin", data=forged)
        self.repo.git("update-ref", "refs/tags/v9.9.9", tag_object)
        self.refused("not by a key")

    def test_a_unicode_blank_comment_that_ssh_keygen_reads_as_a_wildcard_signer_is_refused(self):
        # Grakk's #264 review: Python's strip() made this line a comment, while
        # ssh-keygen (blanks: space and tab only) read it as the live pattern
        # principal "\u00a0#*", and the check before this fix accepted the
        # attacker's tag for tagger email "\u00a0#evil@x".
        allowed = self.repo.base / "allowed_signers"
        allowed.write_bytes(f'{RELEASER} namespaces="git" {self.repo.public("releaser")}\n'.encode()
                            + b'\xc2\xa0#* namespaces="git" ' + self.repo.public("attacker").encode() + b"\n")
        self.repo.raw_signed_tag("v9.9.9", "attacker", "Evil <\u00a0#evil@x>")
        self.refused("byte .* only printable ASCII", allowed=allowed)

    def test_a_tagger_email_that_is_not_one_plain_address_is_refused_before_ssh_keygen(self):
        for number, tagger in enumerate(("Evil <\u00a0#evil@x>", "Evil < #evil@x>", "Evil <*>", "Evil <a@example.invalid,b@example.invalid>", "Evil <>")):
            tag = f"v9.9.{number}"
            with self.subTest(tagger=tagger):
                self.repo.raw_signed_tag(tag, "releaser", tagger)
                self.refused("not one plain email|no tagger", tag=tag)

    def test_a_forged_tag_object_with_a_good_signature_and_tagger_is_accepted(self):
        # The raw builder above makes objects the check accepts when nothing is wrong,
        # so the refusals around it are about the one field each test changes.
        self.repo.raw_signed_tag("v9.9.9", "releaser", f"Releaser <{RELEASER}>")
        self.assertEqual(self.check()[0], RELEASER)

    def test_bytes_after_the_ssh_signature_are_refused(self):
        for number, trailer in enumerate((b"extra\n", b"\n", b"-----END SSH SIGNATURE-----\n", b"x")):
            tag = f"v9.9.{number}"
            with self.subTest(trailer=trailer):
                self.repo.raw_signed_tag(tag, "releaser", f"Releaser <{RELEASER}>", trailer)
                self.refused("bytes after its SSH signature", tag=tag)

    def test_a_signature_without_its_end_line_is_refused(self):
        payload = (f"object {self.repo.commit}\ntype commit\ntag v9.9.9\n"
                   f"tagger Releaser <{RELEASER}> 1700000000 +0000\n\nm\n"
                   "-----BEGIN SSH SIGNATURE-----\nAAAA\n")
        tag_object = self.repo.git("hash-object", "-t", "tag", "-w", "--literally", "--stdin", data=payload)
        self.repo.git("update-ref", "refs/tags/v9.9.9", tag_object)
        self.refused("no END line")

    def test_an_empty_allowed_signers_file_refuses_every_tag(self):
        self.repo.signed_tag("v9.9.9", "releaser")
        self.refused("lists no signer", allowed=self.repo.signers())


WORKFLOW = ROOT / ".github/workflows/publish-release.yml"


def step_script(name, start=None, end=None):
    """The `run: |` script of publish-release's step ``name``, dedented; with
    ``start``/``end``, only the lines from the one containing ``start`` up to
    and including the first after it that equals ``end``."""
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    at = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    run = next(i for i in range(at, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 1
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line)
    script = textwrap.dedent("\n".join(body)) + "\n"
    assert "${{" not in script, name   # values reach the script only through env
    if start:
        part = script.splitlines()
        first = next(i for i, line in enumerate(part) if start in line)
        last = next(i for i in range(first, len(part)) if part[i] == end)
        script = "set -euo pipefail\n" + "\n".join(part[first:last + 1]) + "\n"
    return script


@unittest.skipUnless(ssh_signing_available() and shutil.which("bash"),
                     "needs bash, OpenSSH 8.2+ ssh-keygen -Y and git 2.34+, not native Windows")
class ReleaseSteps(unittest.TestCase):
    """publish-release's own Release steps, run by bash against a fake `gh`
    (tests/fixtures/fake_gh.py) and a real signed tag: not against GitHub."""
    TAG = "v9.9.9"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Repo(self.temp.name)
        self.repo.signed_tag(self.TAG, "releaser")
        self.tag_object = self.repo.git("rev-parse", f"refs/tags/{self.TAG}")
        base = self.repo.base
        self.runner = base / "runner"
        checks = self.runner / "main-checks"
        checks.mkdir(parents=True)
        shutil.copy(ROOT / ".github/scripts/check-tag-signature.py", checks)
        (checks / "allowed_signers").write_text(f'{RELEASER} namespaces="git" {self.repo.public("releaser")}\n')
        release = self.runner / "release"
        release.mkdir()
        (release / "manifest.json").write_text(json.dumps({"assets": {"a.zip": "c" * 64}}))
        (release / "a.zip").write_bytes(b"zip")
        self.bin = base / "bin"
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -B "{ROOT / "tests/fixtures/fake_gh.py"}" "$@"\n')
        gh.chmod(0o755)
        self.state = base / "gh.json"
        self.save({"releases": [], "refs": {self.TAG: ["tag", self.tag_object]},
                   "tags": {self.tag_object: ["commit", self.repo.commit]}, "move_on_publish": None, "calls": []})
        self.output = base / "github-output"

    def tearDown(self):
        self.temp.cleanup()

    def save(self, state):
        self.state.write_text(json.dumps(state))

    def load(self):
        return json.loads(self.state.read_text())

    def run_step(self, script, **env):
        self.output.write_text("")
        full = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", FAKE_GH_STATE=str(self.state),
                    RUNNER_TEMP=str(self.runner), GITHUB_OUTPUT=str(self.output),
                    GITHUB_STEP_SUMMARY=str(self.repo.base / "summary"), GITHUB_REPOSITORY="o/r",
                    GITHUB_SERVER_URL="https://github.com", GITHUB_RUN_ID="1", PLUGIN_VERSION="9.9.9",
                    TAG=self.TAG, TAG_OBJECT=self.tag_object, COMMIT=self.repo.commit, ORK_METER_REF="v1.2.3",
                    DISPATCHER="releaser", GH_TOKEN="unused", **env)
        return subprocess.run(["bash", "-c", script], cwd=self.repo.root, env=full, capture_output=True, text=True)

    def create(self, **env):
        done = self.run_step(step_script("Create the GitHub Release on the existing tag, as a draft"), **env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return self.recorded_id()

    def recorded_id(self):
        return self.output.read_text().strip().removeprefix("id=")

    def publish(self, release_id):
        return self.run_step(step_script("Re-check the tag under the draft, then publish the Release"),
                             RELEASE_ID=release_id)

    def cleanup(self, release_id):
        return self.run_step(step_script("Delete the draft Release after a failure or cancellation"),
                             RELEASE_ID=release_id)

    def release(self, release_id):
        return next((r for r in self.load()["releases"] if r["id"] == int(release_id)), None)

    def test_the_draft_is_created_on_the_release_commit_and_published_by_id(self):
        release_id = self.create()
        release = self.release(release_id)
        self.assertEqual((release["tag_name"], release["draft"], release["target"], release["name"]),
                         (self.TAG, True, self.repo.commit, "Ork Meter plugin 9.9.9"))
        self.assertEqual(sorted(release["assets"]), sorted(p.name for p in (self.runner / "release").iterdir()))
        self.assertIn("| a.zip | `" + "c" * 64 + "` |", release["body"])
        done = self.publish(release_id)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        state = self.load()
        self.assertFalse(state["releases"][0]["draft"])
        patches = [c for c in state["calls"] if "PATCH" in c]
        self.assertEqual(len(patches), 1)
        self.assertIn(f"repos/o/r/releases/{release_id}", patches[0])
        self.assertIn(f"target_commitish={self.repo.commit}", patches[0])
        # Never found by tag name: no `gh release ...` call, no release listing.
        self.assertFalse([c for c in state["calls"] if c[0] == "release"])
        self.assertFalse([c for c in state["calls"] if "repos/o/r/releases?per_page=100" in c])

    def test_a_stale_draft_for_the_tag_stops_the_run_before_anything_is_built(self):
        state = self.load()
        state["releases"].append({"id": 7, "tag_name": self.TAG, "draft": True, "target": "0" * 40})
        self.save(state)
        guard = step_script("Validate the version, the tag and the release commit", "existing=$(gh api", "fi")
        done = self.run_step(guard.replace("$tag", "$TAG"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("7 draft=true", done.stdout)

    def test_a_published_release_for_the_tag_also_stops_the_run(self):
        state = self.load()
        state["releases"].append({"id": 7, "tag_name": self.TAG, "draft": False, "target": self.repo.commit})
        self.save(state)
        guard = step_script("Validate the version, the tag and the release commit", "existing=$(gh api", "fi")
        self.assertEqual(self.run_step(guard.replace("$tag", "$TAG")).returncode, 1)

    def test_a_tag_moved_before_publishing_leaves_the_draft_unpublished(self):
        release_id = self.create()
        for refs, tags in (({self.TAG: ["tag", "f" * 40]}, {}),                     # another tag object
                           ({self.TAG: ["commit", self.repo.commit]}, {}),          # now lightweight
                           ({}, {}),                                                 # deleted
                           (None, {self.tag_object: ["commit", "0" * 40]})):         # object on another commit
            state = self.load()
            if refs is not None:
                state["refs"] = refs
            state["tags"].update(tags)
            self.save(state)
            with self.subTest(refs=refs, tags=tags):
                done = self.publish(release_id)
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertIn("nothing is published", done.stdout)
                self.assertTrue(self.load()["releases"][0]["draft"])
                self.assertNotIn("PATCH", json.dumps(self.load()["calls"]))
            self.save(dict(self.load(), refs={self.TAG: ["tag", self.tag_object]},
                           tags={self.tag_object: ["commit", self.repo.commit]}))

    def test_a_re_point_to_another_validly_signed_tag_object_is_refused(self):
        # Same name, same commit, an allowed signer's good signature: only the
        # pinned object id tells it apart, and that is what refuses it.
        release_id = self.create()
        self.repo.git("-c", "gpg.format=ssh", "-c", f"user.signingkey={self.repo.key('releaser')}",
                      "tag", "-s", "-f", self.TAG, "HEAD", "-m", "re-pointed")
        other = self.repo.git("rev-parse", f"refs/tags/{self.TAG}")
        self.assertNotEqual(other, self.tag_object)
        allowed = self.runner / "main-checks" / "allowed_signers"
        self.assertEqual(signature.check(self.repo.root, self.TAG, self.repo.commit, allowed)[0], RELEASER)
        state = self.load()
        state["refs"][self.TAG] = ["tag", other]
        state["tags"][other] = ["commit", self.repo.commit]
        self.save(state)
        done = self.publish(release_id)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn(f"not the verified tag object {self.tag_object}", done.stdout)
        self.assertTrue(self.release(release_id)["draft"])
        self.assertNotIn("PATCH", json.dumps(self.load()["calls"]))

    def test_a_release_id_that_is_not_this_runs_draft_is_not_published(self):
        release_id = self.create()
        for bad in ("", "abc", str(int(release_id) + 1)):
            with self.subTest(release_id=bad):
                self.assertEqual(self.publish(bad).returncode, 1)
        state = self.load()
        state["releases"][0]["draft"] = False
        self.save(state)
        done = self.publish(release_id)
        self.assertEqual(done.returncode, 1)
        self.assertIn("not this run's draft", done.stdout)

    def test_a_tag_that_moves_as_the_release_is_published_is_reverted_then_cleaned_up(self):
        release_id = self.create()
        self.save(dict(self.load(), move_on_publish=["commit", self.repo.commit]))
        done = self.publish(release_id)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("right after publishing", done.stdout)
        self.assertIn("back to a draft", done.stdout)
        self.assertTrue(self.release(release_id)["draft"])
        done = self.cleanup(release_id)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIsNone(self.release(release_id))

    def test_a_failing_revert_is_reported_and_the_published_release_is_never_deleted(self):
        release_id = self.create()
        self.save(dict(self.load(), move_on_publish=["commit", self.repo.commit], fail_revert=True))
        done = self.publish(release_id)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("STILL PUBLISHED", done.stdout)
        self.assertNotIn("back to a draft, which", done.stdout)
        self.assertFalse(self.release(release_id)["draft"])
        done = self.cleanup(release_id)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("left in place", done.stdout)
        self.assertIsNotNone(self.release(release_id))
        self.assertNotIn("DELETE", json.dumps(self.load()["calls"]))

    def test_a_cancelled_or_failed_run_deletes_its_own_draft_and_no_other(self):
        release_id = self.create()
        state = self.load()
        state["releases"].append({"id": 900, "tag_name": self.TAG, "draft": True, "target": None})   # a human's
        self.save(state)
        done = self.cleanup(release_id)   # what runs after a cancellation or a failure
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual([r["id"] for r in self.load()["releases"]], [900])

    def test_a_failed_upload_leaves_the_id_for_cleanup(self):
        self.save(dict(self.load(), fail_upload=True))
        done = self.run_step(step_script("Create the GitHub Release on the existing tag, as a draft"))
        self.assertNotEqual(done.returncode, 0)
        release_id = self.recorded_id()
        self.assertTrue(self.release(release_id)["draft"])
        self.assertEqual(self.cleanup(release_id).returncode, 0)
        self.assertEqual(self.load()["releases"], [])

    def test_without_an_id_cleanup_deletes_only_drafts_for_the_tag(self):
        # The create step was cancelled before GitHub returned the id.
        self.save(dict(self.load(), releases=[
            {"id": 1, "tag_name": self.TAG, "draft": True, "target": None},
            {"id": 3, "tag_name": self.TAG, "draft": False, "target": None},
            {"id": 4, "tag_name": "v9.9.8", "draft": True, "target": None}]))
        done = self.cleanup("")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual([r["id"] for r in self.load()["releases"]], [3, 4])

    def test_cleanup_re_reads_the_release_and_never_deletes_a_published_one(self):
        self.save(dict(self.load(), releases=[{"id": 5, "tag_name": self.TAG, "draft": False, "target": None},
                                              {"id": 6, "tag_name": "v9.9.8", "draft": True, "target": None}]))
        for release_id in ("5", "6", "77"):
            with self.subTest(release_id=release_id):
                done = self.cleanup(release_id)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("left in place", done.stdout)
        self.assertEqual([r["id"] for r in self.load()["releases"]], [5, 6])


class AllowedSigners(unittest.TestCase):
    KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIc5tUsbAm4V1PZl7kv2u/2Ac0VFG2jF5zDY3s9IxPqG"

    def test_well_formed_entries_parse(self):
        entries = signature.parse_allowed_signers(
            "# comment\n\n"
            f'travis@edos.io namespaces="git" {self.KEY} travis laptop\n'
            f'travis@edos.io namespaces="git",valid-after="20261020" {self.KEY}\n')
        self.assertEqual([e[0] for e in entries], ["travis@edos.io", "travis@edos.io"])
        self.assertEqual(entries[1][1], ('namespaces="git"', 'valid-after="20261020"'))

    def test_weak_or_malformed_entries_are_refused(self):
        for line in (f"travis@edos.io {self.KEY}",                                  # any namespace
                     f'*@edos.io namespaces="git" {self.KEY}',                       # pattern
                     f'a@edos.io,b@edos.io namespaces="git" {self.KEY}',             # list
                     f'travis@edos.io cert-authority,namespaces="git" {self.KEY}',  # CA
                     f'travis@edos.io namespaces="git,file" {self.KEY}',             # extra namespace
                     'travis@edos.io namespaces="git" ssh-ed25519',                  # no key
                     f'travis@edos.io namespaces="git" ssh-dss {self.KEY.split()[1]}',
                     f'travis@edos.io namespaces="git" valid-after="20261020" {self.KEY}',   # two option fields
                     f'travis@edos.io namespaces="git" namespaces="git" {self.KEY}'):
            with self.subTest(line=line), self.assertRaises(signature.Refused):
                signature.parse_allowed_signers(line + "\n")

    def test_unicode_blanks_line_separators_and_control_bytes_are_refused_anywhere(self):
        good = f'travis@edos.io namespaces="git" {self.KEY}'.encode()
        for odd in ("\u00a0", "\u2028", "\u2029", "\u0085", "\u200b", "\ufeff", "\u3000", "\x0b", "\x0c", "\x1c", "\x00", "\x7f"):
            for data in (odd.encode() + b"#" + good + b"\n",            # before a comment marker
                         good + odd.encode() + b"\n",                  # trailing
                         b"# comment" + odd.encode() + b"tail\n" + good + b"\n",   # inside a comment
                         good.replace(b" ", odd.encode(), 1) + b"\n"):  # as a field separator
                with self.subTest(odd=repr(odd), data=data[:20]), self.assertRaisesRegex(signature.Refused, "printable ASCII"):
                    signature.parse_allowed_signers(data)
        with self.assertRaises(signature.Refused):
            signature.parse_allowed_signers(good.replace(b" ", b"\r", 1) + b"\n")   # \r is a line ending only

    def test_lines_end_only_at_newline_and_blanks_are_only_space_and_tab(self):
        good = f'travis@edos.io namespaces="git" {self.KEY}'
        entries = signature.parse_allowed_signers(f" \t# indented comment\r\n\t{good}\t c\r\n\n".encode())
        self.assertEqual(entries, [("travis@edos.io", ('namespaces="git"',), "ssh-ed25519")])

    def test_tagger_email(self):
        self.assertEqual(signature.tagger_email({"tagger": "R <r@edos.io> 1700000000 +0000"}), "r@edos.io")
        for tagger in ("R <\u00a0#evil@x> 1 +0000", "R < #evil@x> 1 +0000", "R <*@edos.io> 1 +0000", "R <r@edos.io\n> 1 +0000", "R <r@edos.io>", ""):
            with self.subTest(tagger=tagger), self.assertRaises(signature.Refused):
                signature.tagger_email({"tagger": tagger})

    def test_the_repository_file_parses(self):
        signature.parse_allowed_signers((ROOT / ".github/allowed_signers").read_bytes())


class Wiring(unittest.TestCase):
    def test_publish_release_checks_the_signature_with_mains_copy_before_anything_is_attested(self):
        workflow = (ROOT / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
        check = workflow.index('python3 -I -B "$RUNNER_TEMP/main-checks/check-tag-signature.py"')
        self.assertLess(check, workflow.index("Check out the tagged release commit"))
        self.assertLess(check, workflow.index("actions/attest-build-provenance"))
        self.assertLess(check, workflow.index('id=$(gh api -X POST "repos/$GITHUB_REPOSITORY/releases"'))

    def test_publish_release_publishes_a_draft_only_after_re_checking_the_tag(self):
        # ReleaseSteps runs these steps; this pins what a fake gh cannot show.
        workflow = (ROOT / ".github/workflows/publish-release.yml").read_text(encoding="utf-8")
        create = workflow.index('id=$(gh api -X POST "repos/$GITHUB_REPOSITORY/releases"')
        self.assertIn('-f target_commitish="$COMMIT"', workflow[create:create + 300])
        self.assertLess(create, workflow.index("- name: Re-check the tag under the draft"))
        self.assertIn("RELEASE_ID: ${{ steps.draft.outputs.id }}", workflow)
        # Releases are addressed by id after creation, never found by tag name.
        for by_name in ("gh release create", "gh release upload", "gh release edit", "gh release view", "gh release delete"):
            self.assertFalse(by_name in workflow, by_name)
        # Main's copies are saved before the tagged commit is checked out.
        saved = workflow.index('cp .github/scripts/check-tag-signature.py .github/allowed_signers "$RUNNER_TEMP/main-checks/"')
        self.assertLess(saved, workflow.index("Check out the tagged release commit"))
        # A failure or a cancellation deletes the draft.
        self.assertIn("if: (failure() || cancelled()) && steps.draft.conclusion != 'skipped'", workflow)
        self.assertNotIn("--cleanup-tag", workflow)

    def test_the_allowed_signers_example_writes_one_line(self):
        docs = (ROOT / "docs/release.md").read_text(encoding="utf-8")
        line = next(line for line in docs.splitlines() if line.startswith("printf '%s namespaces=\"git\" %s"))
        self.assertIn(r"%s\n' ", line)
        self.assertTrue(line.endswith(">> .github/allowed_signers"), line)

    def test_the_tagging_instructions_sign(self):
        for path in (".github/workflows/package-runtime.yml", "docs/release.md"):
            text = (ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                self.assertIn("git tag -s v", text)
                self.assertNotIn("git tag -a v", text)


if __name__ == "__main__":
    unittest.main()
