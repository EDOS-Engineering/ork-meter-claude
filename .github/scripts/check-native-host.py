"""Bounded localhost installed-host qualification; skips never pass this gate.

CI runs it in ci.yml's `native-host` job, once per OS and Claude Code version.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import unittest


# Linux CI only, in its own job (.github/workflows/ci.yml connected-failure):
# it needs a trusted test CA and a keyring and holds expected failures.
CONNECTED = "native_failure_connected_qualification."


def iterate(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iterate(item)
        else:
            yield item


def snapshot(root):
    digest = hashlib.sha256()
    for directory in (".claude-plugin", "hooks", "scripts", "skills", "agents", "tests", ".github/scripts"):
        for path in sorted((root / directory).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in (".py", ".sh", ".json", ".md", ".gz", ".txt"):
                continue
            identity = [path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()]
            digest.update((json.dumps(identity) + "\n").encode("utf-8"))
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if os.environ.get("RUN_NATIVE_CLAUDE_TESTS") != "1":
        parser.error("set RUN_NATIVE_CLAUDE_TESTS=1 to authorize isolated localhost qualification")
    binary = shutil.which("claude")
    if not binary:
        parser.error("installed claude executable is required")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    print("Installed-host qualification: %s; %s; Python %s" % (version, platform.platform(), platform.python_version()), flush=True)
    root = Path(__file__).resolve().parents[2]
    before = snapshot(root)
    required = {"native_qualification.py", "native_output_qualification.py", "native_recovery_qualification.py",
                "native_failed_capture_qualification.py", "native_source_qualification.py", "native_cancellation_qualification.py",
                "native_skill_qualification.py", "native_json_qualification.py", "native_cache_qualification.py",
                "native_webfetch_qualification.py", "native_lifecycle_qualification.py",
                "native_capture_env_qualification.py", "native_lean_qualification.py",
                "native_canary_qualification.py", "native_failure_qualification.py"}
    missing = required - {path.name for path in (root / "tests").glob("native_*qualification.py")}
    if missing:
        raise SystemExit("Missing durable host qualification suites: " + ", ".join(sorted(missing)))
    suite = unittest.TestSuite(test for test in iterate(unittest.defaultTestLoader.discover(
        str(root / "tests"), pattern="native_*qualification.py")) if not test.id().startswith(CONNECTED))
    count = suite.countTestCases()
    if count < 34:
        raise SystemExit("Expected at least thirty-four durable native-host cases; found %d" % count)
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    after = snapshot(root)
    qualified = (result.wasSuccessful() and not result.skipped and not result.expectedFailures
                 and not result.unexpectedSuccesses and result.testsRun == count and before == after)
    report = {"schema_version": 1, "host_version": version, "platform": platform.platform(),
              "python_version": platform.python_version(), "inference": "localhost-synthetic",
              "source_sha256_before": before, "source_sha256_after": after, "source_unchanged": before == after,
              "tests_discovered": count, "tests_run": result.testsRun,
              "failures": [str(test) for test, _ in result.failures],
              "errors": [str(test) for test, _ in result.errors],
              "expected_failures": [str(test) for test, _ in result.expectedFailures],
              "unexpected_successes": [str(test) for test in result.unexpectedSuccesses],
              "skips": [{"test": str(test), "reason": reason} for test, reason in result.skipped],
              "elapsed_seconds": round(time.monotonic() - started, 3), "qualified": qualified}
    print(json.dumps(report, sort_keys=True), flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if qualified else 1


if __name__ == "__main__":
    sys.exit(main())
