"""Deep-path atomicity, complete recovery and circuit persistence on real disk."""
import concurrent.futures
import ntpath
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/lib"))
import artifacts
import circuit
import ledger
import session


class DeepStorage(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ork-deep-storage-"))
        self.addCleanup(self.cleanup)
        self.scratch = self.root / ("profile-" + "x" * 90) / ("workspace-" + "y" * 75) / "scratchpad"
        self.store = session.Store.for_payload({"scratchpad_dir": str(self.scratch), "session_id": "deep-session"})
        self.store.remember({"session_id": "deep-session"})

    def cleanup(self):
        # Only the fixture's resolved, named temporary root; use one filesystem API.
        self.assertEqual(self.root.resolve().parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("ork-deep-storage-"))
        shutil.rmtree(session.fs_path(self.root))

    def test_concurrent_atomic_winner_and_reads_beyond_300_characters(self):
        path = os.path.join(self.store.path, "v" * 64 + ".json")
        self.assertGreater(len(path), 300)
        def publish(number):
            return session.initialize_json(path, {"session_id": "deep-session", "winner": number})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            winners = list(pool.map(publish, range(12)))
        self.assertEqual(sum(winners), 1)
        winner = session.read_json(path)
        self.assertIn(winner["winner"], range(12))
        self.assertEqual(list(session.glob_paths(os.path.join(self.store.path, ".ork-meter-*.tmp"))), [])
        self.assertEqual(session.read_json(self.store.session_path)["session_id"], "deep-session")

    def test_deep_faults_disable_scope_and_remain_readable_in_fresh_process(self):
        for identity in ("first", "second"):
            circuit.record(self.store, "trim-tool-output:Bash", identity, "pin", "handler", "RuntimeError", "deep-session")
        value = circuit.read(self.store, "trim-tool-output:Bash")
        self.assertTrue(value["disabled"])
        self.assertEqual(value["faults"], 2)
        self.assertFalse(circuit.read(self.store, "trim-tool-output:Grep")["disabled"])
        program = "import sys;sys.path.insert(0,sys.argv[1]);import circuit,session;v=circuit.read(session.Store(sys.argv[2]),'trim-tool-output:Bash');assert v['disabled'] and v['faults']==2"
        result = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(ROOT / "hooks/lib"), self.store.path],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deep_artifacts_ledger_and_cli_recovery_preserve_exact_data(self):
        raw = {"stdout": "exact\r\nsource\n", "stderr": "fatal: original\r\n", "exitCode": 7, "interrupted": False}
        path = artifacts.save(self.store, "Bash", raw, raw["stdout"])
        self.assertGreater(len(path), 300)
        self.assertFalse(path.startswith("\\\\?\\"), "recovery paths retain their ordinary spelling")
        self.assertEqual(artifacts.load(self.store, Path(path).stem)["response"], raw)
        ledger.append(self.store, ledger.trimmed("Bash", 100, 50))
        self.assertEqual(ledger.scan(self.store)["status"], "ok")
        command = [sys.executable, "-I", "-B", str(ROOT / "scripts/artifact.py"), "--scratchpad", str(self.scratch),
                   "--id", Path(path).stem, "--stream", "stdout"]
        result = subprocess.run(command, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, raw["stdout"].encode())
        self.assertEqual([row["kind"] for row in ledger.load(self.store)], ["trim", "recovery"])

    def test_windows_drive_unc_and_existing_namespace_spelling(self):
        with patch.object(session.os, "name", "nt"), patch.object(session.os, "path", ntpath):
            for ordinary, extended in (("C:\\root\\file.json", "\\\\?\\C:\\root\\file.json"),
                                       ("\\\\server\\share\\file.json", "\\\\?\\UNC\\server\\share\\file.json")):
                self.assertEqual(session.fs_path(ordinary), extended)
                self.assertEqual(session.fs_path(extended), extended)
                self.assertEqual(session.display_path(extended), ordinary)


if __name__ == "__main__":
    unittest.main()
