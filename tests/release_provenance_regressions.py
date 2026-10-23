"""Publish and CI refuse runtime binaries and release commits the package workflow did not produce."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402

scripts = ROOT / ".github/scripts"
commit_check = load(".github/scripts/check-release-commit.py")
attestations = load(".github/scripts/verify-runtime-attestations.py")
BOT = (commit_check.BOT_NAME, commit_check.BOT_EMAIL)


def git(root, *args, author=("t", "t@example.invalid")):
    env = dict(os.environ, GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1],
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.check_output(["git", *args], cwd=root, env=env, text=True).strip()


class ReleaseCommit(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".claude-plugin").mkdir()
        (self.root / ".claude-plugin/plugin.json").write_text('{"version": "0.3.0"}\n')
        (self.root / "README.md").write_text("plugin\n")
        git(self.root, "init", "-q")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "base")

    def tearDown(self):
        self.temp.cleanup()

    def release(self, extra=None, author=BOT):
        (self.root / ".claude-plugin/plugin.json").write_text('{"version": "0.4.0"}\n')
        (self.root / "runtime/linux-amd64").mkdir(parents=True, exist_ok=True)
        (self.root / "runtime/linux-amd64/ork-meter-plugin-runtime").write_bytes(b"binary\n")
        (self.root / "runtime/linux-amd64/SHA256").write_text("0" * 64 + "\n")
        if extra:
            (self.root / extra).write_text("smuggled\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "release: plugin 0.4.0 with runtime from ork-meter v1", author=author)
        return git(self.root, "rev-parse", "HEAD")

    def run_check(self, commit):
        return subprocess.run([sys.executable, "-I", "-B", str(scripts / "check-release-commit.py"), commit],
                              cwd=self.root, capture_output=True, text=True)

    def test_bot_authored_payload_only_commit_is_accepted(self):
        result = self.run_check(self.release())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_human_author_or_extra_file_is_refused(self):
        for kwargs in ({"author": ("Mallory", "m@example.invalid")},
                       {"author": ("github-actions[bot]", "m@example.invalid")},
                       {"extra": "README.md"}, {"extra": "runtime/linux-amd64/hook.sh"}):
            with self.subTest(kwargs=kwargs):
                git(self.root, "reset", "-q", "--hard", git(self.root, "rev-list", "--max-parents=0", "HEAD"))
                result = self.run_check(self.release(**kwargs))
                self.assertEqual(result.returncode, 1)
                self.assertIn("refused", result.stderr)

    def test_unknown_commit_is_refused(self):
        self.assertEqual(self.run_check("0" * 40).returncode, 1)

    def test_validate_rules(self):
        payload = [".claude-plugin/plugin.json", "runtime/windows-amd64/ork-meter-plugin-runtime.exe"]
        commit_check.validate(*BOT, ["p"], payload)
        refusals = [(BOT, [], payload), (BOT, ["p", "q"], payload),
                    (BOT, ["p"], ["runtime/linux-amd64/SHA256"]),
                    (BOT, ["p"], payload + ["runtime/freebsd-amd64/ork-meter-plugin-runtime"]),
                    (BOT, ["p"], payload + [".github/workflows/ci.yml"])]
        for author, parents, paths in refusals:
            with self.subTest(parents=parents, paths=paths), self.assertRaises(ValueError):
                commit_check.validate(*author, parents, paths)


class RuntimeAttestations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name) / "runtime"
        for target in attestations.TARGETS:
            package = self.runtime / target
            package.mkdir(parents=True)
            body = f"attested {target}\n".encode()
            (package / attestations.binary_name(target)).write_bytes(body)
            (package / "SHA256").write_text(hashlib.sha256(body).hexdigest() + "\n")
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def verifier(self, path):
        self.calls.append(path)
        return path.read_bytes().startswith(b"attested"), "no matching attestation"

    def test_every_attested_binary_is_verified(self):
        self.assertEqual(attestations.verify(self.runtime, self.verifier), [])
        self.assertEqual(len(self.calls), len(attestations.TARGETS))

    def test_unattested_missing_or_extra_files_are_refused(self):
        (self.runtime / "linux-arm64" / attestations.BINARY).write_bytes(b"swapped\n")
        (self.runtime / "darwin-amd64" / attestations.BINARY).unlink()
        (self.runtime / "freebsd-amd64").mkdir()
        (self.runtime / "freebsd-amd64" / attestations.BINARY).write_bytes(b"attested too\n")
        failures = attestations.verify(self.runtime, self.verifier)
        self.assertEqual(len(failures), 3, failures)
        self.assertTrue(any("linux-arm64" in f and "no valid" in f for f in failures))
        self.assertTrue(any("missing" in f and "darwin-amd64" in f for f in failures))
        self.assertTrue(any("unexpected" in f and "freebsd-amd64" in f for f in failures))
        self.assertTrue(attestations.verify(Path(self.temp.name) / "absent", self.verifier)[0]
                        .endswith("is not a directory"))

    def test_only_the_v0_3_0_digests_at_their_own_target_skip_verification(self):
        self.assertEqual(sorted(t for t, _ in attestations.LEGACY), sorted(attestations.TARGETS))
        never = lambda path: (False, "unattested")
        legacy = hashlib.sha256((self.runtime / "linux-amd64" / attestations.BINARY).read_bytes()).hexdigest()
        with mock.patch.object(attestations, "LEGACY", {("linux-amd64", legacy)}):
            failures = attestations.verify(self.runtime, never)
            self.assertEqual(len(failures), len(attestations.TARGETS) - 1, failures)
            self.assertFalse(any("linux-amd64" in f for f in failures))
            # The same bytes at another target are not exempt.
            (self.runtime / "linux-arm64" / attestations.BINARY).write_bytes(
                (self.runtime / "linux-amd64" / attestations.BINARY).read_bytes())
            self.assertTrue(any("linux-arm64" in f for f in attestations.verify(self.runtime, never)))

    @unittest.skipUnless(os.name == "posix", "the fake gh is a shell script")
    def test_command_line_runs_gh_with_the_signer_workflow_and_fails_closed(self):
        fake = Path(self.temp.name) / "bin"
        fake.mkdir()
        log = Path(self.temp.name) / "gh.log"
        (fake / "gh").write_text(
            "#!/bin/sh\n"
            f"echo \"$*\" >> '{log}'\n"
            "head -c 8 \"$3\" | grep -q attested\n")
        (fake / "gh").chmod(0o755)
        script = [sys.executable, "-I", "-B", str(scripts / "verify-runtime-attestations.py"), str(self.runtime)]
        result = subprocess.run(script, env={"PATH": f"{fake}:/usr/bin:/bin"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text().splitlines()
        self.assertEqual(len(calls), len(attestations.TARGETS))
        for call in calls:
            self.assertIn("--repo EDOS-Engineering/ork-meter-claude", call)
            self.assertIn("--signer-workflow EDOS-Engineering/ork-meter-claude/.github/workflows/package-runtime.yml", call)
            self.assertIn("--source-ref refs/heads/main", call)
        (self.runtime / "windows-amd64" / "ork-meter-plugin-runtime.exe").write_bytes(b"swapped\n")
        result = subprocess.run(script, env={"PATH": f"{fake}:/usr/bin:/bin"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("windows-amd64", result.stderr)
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        result = subprocess.run(script, env={"PATH": str(empty)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, "a missing gh must refuse, not skip")
        self.assertIn("cannot run gh", result.stderr)


if __name__ == "__main__":
    unittest.main()
