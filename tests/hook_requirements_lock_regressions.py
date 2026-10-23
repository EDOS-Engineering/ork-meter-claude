"""requirements-hooks.txt is the hash-locked closure of requirements-hooks.in (#265).

Offline, on every run: the lock records the current requirements-hooks.in,
every package in it is an exact pin with hashes, every direct pin is locked at
its version, every other package is needed by one in the lock, and everything
that installs the hook dependencies installs the lock, hash-checked.

With ORK_LOCK_CHECK=1 (needs uv and PyPI) it also runs
``.github/scripts/lock-hook-requirements.py --check``, which re-resolves the lock and
checks every supported platform and Python has a wheel.
"""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402

lock = load(".github/scripts/lock-hook-requirements.py")
sbom = load(".github/scripts/build-release-sbom.py")
LOCK = (ROOT / "requirements-hooks.txt").read_text(encoding="utf-8")
INPUT = (ROOT / "requirements-hooks.in").read_bytes()


def wheel(name, digest, filename):
    return {"filename": filename, "digests": {"sha256": digest}}


class TheLock(unittest.TestCase):
    def test_it_records_the_current_input(self):
        recorded = re.search(r"^# input: requirements-hooks\.in sha256=([0-9a-f]{64})$", LOCK, re.M)
        self.assertIsNotNone(recorded, "requirements-hooks.txt records no input digest")
        self.assertEqual(recorded.group(1), hashlib.sha256(INPUT).hexdigest(),
                         "requirements-hooks.in changed since the lock was generated: "
                         "run python3 .github/scripts/lock-hook-requirements.py")
        self.assertRegex(lock.recorded_date(LOCK) or "", r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_every_package_is_pinned_with_hashes_and_every_direct_pin_is_locked(self):
        locked, direct = sbom.locked_requirements(LOCK, INPUT.decode())
        self.assertEqual(direct, ["tiktoken", "certifi"])
        self.assertEqual(set(locked), {"tiktoken", "regex", "requests", "charset-normalizer", "idna",
                                       "urllib3", "certifi"})
        for name, (_, _, hashes) in locked.items():
            with self.subTest(package=name):
                self.assertTrue(hashes)
                self.assertEqual(len(hashes), len(set(hashes)))
        # The lock script's own parser reads the same pins and hashes.
        parsed = lock.entries(LOCK)
        self.assertEqual({n: (v, sorted(h)) for n, (_, v, h) in locked.items()},
                         {n: (v, h) for n, (v, m, h) in parsed.items()})
        self.assertTrue(all(not marker for _, marker, _ in parsed.values()), "a marker the SBOM would refuse")

    def test_every_package_is_needed(self):
        locked, direct = sbom.locked_requirements(LOCK, INPUT.decode())
        via = {}
        for block in re.split(r"\n(?=[A-Za-z0-9])", LOCK):
            name = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==", block)
            if name:
                via[sbom.normalize(name.group(1))] = re.findall(r"#\s+(?:via\s+)?(-r \S+|[A-Za-z0-9][\w.-]*)\s*$",
                                                                 block, re.M)
        for name in locked:
            with self.subTest(package=name):
                wanted = via.get(name)
                self.assertTrue(wanted, f"{name} records no '# via'")
                if name in direct:
                    self.assertIn("-r requirements-hooks.in", wanted)
                else:
                    self.assertTrue(any(sbom.normalize(w) in locked for w in wanted), wanted)

    @unittest.skipUnless(os.environ.get("ORK_LOCK_CHECK") == "1",
                         "set ORK_LOCK_CHECK=1 (needs uv and PyPI) to re-resolve the lock")
    def test_the_lock_is_what_the_input_resolves_to(self):
        result = subprocess.run([sys.executable, str(ROOT / ".github/scripts/lock-hook-requirements.py"), "--check"],
                                capture_output=True, text=True, timeout=900)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class Script(unittest.TestCase):
    def test_entries_ignore_layout_and_comments(self):
        a = "# header\nfoo==1.0 \\\n    --hash=sha256:%s \\\n    --hash=sha256:%s\n    # via bar\n" % ("b" * 64, "a" * 64)
        b = "Foo==1.0 --hash=sha256:%s --hash=sha256:%s  # moved\n" % ("a" * 64, "b" * 64)
        self.assertEqual(lock.entries(a), lock.entries(b))
        self.assertEqual(lock.entries(a), {"foo": ("1.0", "", ["a" * 64, "b" * 64])})
        self.assertNotEqual(lock.entries(a), lock.entries(b.replace("1.0", "1.1")))
        self.assertNotEqual(lock.entries(a), lock.entries(b.replace("a" * 64, "c" * 64)))

    def test_wheel_tags_serve_their_platforms_and_pythons(self):
        cases = [
            ("x-1-py3-none-any.whl", "3.15", "Windows x86_64", True),
            ("x-1-cp312-cp312-macosx_11_0_arm64.whl", "3.12", "macOS arm64", True),
            ("x-1-cp312-cp312-macosx_11_0_arm64.whl", "3.12", "macOS x86_64", False),
            ("x-1-cp312-cp312-macosx_11_0_arm64.whl", "3.13", "macOS arm64", False),
            ("x-1-cp310-cp310-macosx_10_9_universal2.whl", "3.10", "macOS x86_64", True),
            ("x-1-cp314-cp314t-manylinux_2_28_x86_64.whl", "3.14", "Linux x86_64", False),
            ("x-1-cp311-cp311-manylinux2014_aarch64.manylinux_2_17_aarch64.whl", "3.11", "Linux aarch64", True),
            ("x-1-cp311-cp311-musllinux_1_2_aarch64.whl", "3.11", "Linux aarch64", False),
            ("x-1-cp39-abi3-win_amd64.whl", "3.15", "Windows x86_64", True),
            ("x-1-cp312-abi3-win_amd64.whl", "3.11", "Windows x86_64", False),
            ("x-1-cp313-cp313-win_arm64.whl", "3.13", "Windows x86_64", False),
        ]
        for filename, python, platform, expected in cases:
            with self.subTest(filename=filename, python=python, platform=platform):
                self.assertEqual(lock.serves(filename, python, platform), expected)

    def test_coverage_gaps_name_missing_wheels_and_unhashed_files(self):
        full = [wheel("x", "1" * 64, "x-1-py3-none-any.whl"), wheel("x", "2" * 64, "x-1.tar.gz")]
        partial = [wheel("y", "3" * 64, f"y-2-cp{v.replace('.', '')}-cp{v.replace('.', '')}-{tag}.whl")
                   for v in lock.PYTHONS for tag in ("macosx_11_0_arm64", "manylinux_2_28_x86_64", "win_amd64")]
        files = {"x": full, "y": partial}
        locked = {"x": ("1", "", ["1" * 64]), "y": ("2", "", sorted(f["digests"]["sha256"] for f in partial))}
        gaps = lock.coverage_gaps(locked, fetch=lambda name, version: files[name])
        self.assertIn("x 1: x-1.tar.gz has no hash in the lock", gaps)
        self.assertIn("y 2: no wheel for macOS x86_64 CPython 3.10", gaps)
        self.assertIn("y 2: no wheel for Linux aarch64 CPython 3.15", gaps)
        self.assertFalse([g for g in gaps if "Windows" in g or "macOS arm64" in g])


class Installs(unittest.TestCase):
    """Everything that installs the hook dependencies installs the lock, hash-checked."""

    def test_nothing_installs_the_unlocked_input(self):
        tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True)
        for name in tracked.stdout.split("\0"):
            path = ROOT / name
            if not name or not path.is_file() or path.suffix not in (".md", ".py", ".sh", ".yml", ".txt"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertNotRegex(text, r"pip install[^\n]*requirements-hooks\.in")

    def test_customer_and_contributor_instructions_require_hashes(self):
        for path in ("docs/token-optimization.md", "CONTRIBUTING.md", "hooks/lib/tokenizer_check.py"):
            text = (ROOT / path).read_text(encoding="utf-8")
            installs = re.findall(r"pip install[^\n]*(?:\n[^\n]*)?requirements-hooks\.txt", text)
            installs += re.findall(r"pip install %s -r", text)
            with self.subTest(path=path):
                self.assertTrue(installs)
                for install in installs:
                    self.assertTrue("--require-hashes --only-binary=:all:" in install or "LOCKED_INSTALL" in text,
                                    install)
        self.assertEqual(load("hooks/lib/tokenizer_check.py").LOCKED_INSTALL, "--require-hashes --only-binary=:all:")

    def test_ci_installs_match_the_customer_install(self):
        # CI, packaging, release and WSL install exactly as customers do:
        # hash-checked and wheels only, so CI never builds an sdist a customer couldn't.
        sites = []
        for path in list((ROOT / ".github/workflows").glob("*.yml")) + [ROOT / ".github/scripts/wsl-setup.sh"]:
            sites += re.findall(r"pip install[^\n]*-r requirements-hooks\.[a-z]+", path.read_text(encoding="utf-8"))
        self.assertTrue(sites)
        for site in sites:
            with self.subTest(site=site):
                self.assertTrue(site.endswith("requirements-hooks.txt"), site)
                self.assertIn("--require-hashes --only-binary=:all:", site)
        self.assertTrue(all(h for _, _, h in sbom.requirements(LOCK)))

if __name__ == "__main__":
    unittest.main()
