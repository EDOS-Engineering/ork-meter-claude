"""Exact multi-file evidence, workspace binding and bounded batch behavior."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402

source = load("scripts/source.py")


class SourceBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.workspace = self.directory / "task"
        self.workspace.mkdir()

    def save(self, path, raw):
        target = self.workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return target

    def test_exact_decorated_symbol_imports_and_test_arrive_in_one_batch(self):
        raw = b'import math\r\n\r\n@decorator\r\ndef calculate(value):\r\n    return math.floor(value)\r\n'
        self.save("units.py", raw)
        test = b'def test_fraction():\n    assert calculate(1.25) == 1\n'
        self.save("tests/test_units.py", test)
        requests = [{"id": "body", "path": "units.py", "action": "symbol", "symbol": "calculate"},
                    {"id": "imports", "path": "units.py", "start": 1, "limit": 1},
                    {"id": "test", "path": "tests/test_units.py", "action": "symbol", "symbol": "test_fraction"}]
        output = source.batch(requests, self.workspace)
        self.assertTrue(output["complete"])
        results = [row["result"] for row in output["results"]]
        self.assertEqual(results[0]["source"], raw.decode()[raw.decode().index("@decorator"):])
        self.assertEqual(results[1]["source"], 'import math\r\n')
        self.assertEqual(results[2]["source"], test.decode())
        self.assertEqual(output["input_bytes"], len(raw) + len(test))
        self.assertEqual(results[0]["sha256"], hashlib.sha256(raw).hexdigest())

    def test_search_reports_omissions_and_retains_exact_physical_lines(self):
        for newline in ('\n', '\r\n', '\r'):
            with self.subTest(newline=newline):
                lines = ['heading\fpage', '# before\u2028after', '# before\u2029tail', 'last']
                self.save("text.py", (newline.join(lines) + newline).encode())
                output = source.batch([{"path": "text.py", "action": "search", "query": "before", "limit": 1,
                                        "context": 1}], self.workspace)
                result = output["results"][0]["result"]
                self.assertEqual(result["scanned_lines"], 4)
                self.assertEqual((result["matching_lines"], result["returned_matching_lines"], result["omitted_matching_lines"]), (2, 1, 1))
                self.assertEqual(result["lines"], [{"line": i + 1, "text": lines[i] + newline} for i in range(3)])

    def test_outside_paths_and_plugin_cwd_decoy_are_not_task_source(self):
        self.save("module.py", b'VALUE = "task"\n')
        (self.directory / "module.py").write_text('VALUE = "decoy"\n')
        with patch.object(Path, "cwd", return_value=self.directory):
            output = source.batch([{"path": "module.py"}, {"path": "../module.py"},
                                   {"path": str(self.directory / "module.py")}], self.workspace)
        self.assertFalse(output["complete"])
        self.assertEqual(output["results"][0]["result"]["source"], 'VALUE = "task"\n')
        self.assertEqual([row["status"] for row in output["results"]], ["ok", "error", "error"])
        self.assertNotIn('VALUE = "decoy"', json.dumps(output))

    def test_stale_and_missing_items_do_not_hide_valid_evidence(self):
        self.save("good.py", b'def f():\n    return 2\n')
        old = hashlib.sha256(b'def f():\n    return 1\n').hexdigest()
        output = source.batch([{"path": "good.py", "action": "symbol", "symbol": "f", "expected_hash": old},
                               {"path": "missing.py"}, {"path": "good.py", "action": "outline"}], self.workspace)
        self.assertFalse(output["complete"])
        self.assertIn("source changed", output["results"][0]["error"])
        self.assertEqual(output["results"][2]["result"]["symbols"][0]["name"], "f")

    def test_same_file_snapshot_is_reused_even_when_file_changes_between_requests(self):
        path = self.save("module.py", b'def f():\n    return 1\n')
        original = source.retrieve
        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            path.write_bytes(b'def f():\n    return 2\n')
            return result
        with patch.object(source, "retrieve", side_effect=mutate):
            output = source.batch([{"path": "module.py", "action": "outline"},
                                   {"path": "module.py", "action": "symbol", "symbol": "f"}], self.workspace)
        first, second = [row["result"] for row in output["results"]]
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(second["source"], 'def f():\n    return 1\n')
        later = source.batch([{"path": "module.py", "expected_hash": second["sha256"]}], self.workspace)
        self.assertFalse(later["complete"])

    def test_budget_withholds_whole_symbol_and_still_returns_smaller_item(self):
        self.save("big.py", b'def complete():\n' + b'    # important evidence ' + b'x' * 6000 + b'\n    return 1\n')
        self.save("small.py", b'def small():\n    return 3\n')
        output = source.batch([{"path": "big.py", "action": "symbol", "symbol": "complete"},
                               {"path": "small.py", "action": "symbol", "symbol": "small"}], self.workspace, budget=1024)
        self.assertLessEqual(len(source._serialized(output).encode()), 1024)
        self.assertFalse(output["complete"])
        withheld, included = output["results"]
        self.assertEqual(withheld["status"], "output_budget_exceeded")
        self.assertFalse(withheld["exact_evidence_included"])
        self.assertEqual(included["result"]["source"], 'def small():\n    return 3\n')

    def test_invalid_and_oversized_batches_fail_before_reading(self):
        for requests in ([], [{}] * 17, [{"path": "x", "action": []}], [{"path": "x", "start": True}],
                         [{"path": "x", "action": "lines", "symbol": "f"}],
                         [{"id": "same", "path": "x"}, {"id": "same", "path": "x"}],
                         [{"path": "x", "action": "search", "query": ""}]):
            with self.subTest(requests=requests), patch.object(Path, "open", side_effect=AssertionError("read before validation")):
                with self.assertRaises(ValueError):
                    source.batch(requests, self.workspace)

    def test_large_input_is_rejected_without_reading_unbounded_data(self):
        self.save("huge.txt", b'x' * (source.MAX_SOURCE_BYTES + 1))
        output = source.batch([{"path": "huge.txt"}], self.workspace)
        self.assertFalse(output["complete"])
        self.assertIn("input allowance", output["results"][0]["error"])
        self.assertEqual(output["input_bytes"], 0)

    def test_malformed_python_and_other_languages_keep_exact_range_fallback(self):
        for path, raw in (("broken.py", b'def (:\r\n# exact evidence\r\n'),
                          ("service.go", b'package service\nfunc f() {}\n')):
            self.save(path, raw)
            output = source.batch([{"path": path, "start": 1, "limit": 2}], self.workspace)
            result = output["results"][0]["result"]
            self.assertTrue(output["complete"])
            self.assertEqual(result["method"], "exact lines")
            self.assertEqual(result["source"], raw.decode())

    def test_cli_is_bound_to_explicit_workspace_and_payload_budget(self):
        self.save("units.py", b'def millis(x):\n    return x * 1000\n')
        (self.directory / "units.py").write_text('raise RuntimeError("must never execute")\n')
        command = [sys.executable, "-I", "-B", str(ROOT / "scripts/source.py"),
                   "--workspace", str(self.workspace), "--budget", "1024", "--batch",
                   json.dumps([{"path": "units.py", "action": "symbol", "symbol": "millis"}])]
        process = subprocess.run(command, cwd=self.directory, capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertLessEqual(len(process.stdout), 1024)
        result = json.loads(process.stdout)
        self.assertTrue(result["complete"])
        self.assertEqual(result["results"][0]["result"]["source"], 'def millis(x):\n    return x * 1000\n')


    def single(self, *args, env=None):
        command = [sys.executable, "-I", "-B", str(ROOT / "scripts/source.py"), *args]
        return subprocess.run(command, cwd=self.workspace, capture_output=True, env=env)

    def secret(self):
        """A ~/.ssh-like file outside the workspace."""
        key = self.directory / "home/.ssh/id_rsa"
        key.parent.mkdir(parents=True)
        key.write_bytes(b"PRIVATE KEY MATERIAL\n")
        return key

    def assert_refused(self, process):
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, b"")
        self.assertIn(b"refusing", process.stderr)
        self.assertNotIn(b"PRIVATE KEY", process.stderr)

    def test_single_retrieval_refuses_paths_outside_the_workspace(self):
        key = self.secret()
        self.assert_refused(self.single(str(key), "--start", "1", "--limit", "5"))
        self.assert_refused(self.single("../secrets/.ssh/id_rsa"))
        self.assert_refused(self.single(str(self.directory / "home/.ssh/missing")))

    def test_single_retrieval_refuses_symlink_escapes(self):
        key = self.secret()
        try:
            (self.workspace / "link.py").symlink_to(key)
            (self.workspace / "dir").symlink_to(key.parent, target_is_directory=True)
        except OSError as exc:  # Windows without symlink privilege
            self.skipTest("symlinks unavailable: %s" % exc)
        self.assert_refused(self.single("link.py"))
        self.assert_refused(self.single("dir/id_rsa", "--outline"))

    def test_single_retrieval_reads_workspace_files_and_the_plugin_artifact_store(self):
        self.save("pkg/units.py", b"def millis(x):\n    return x * 1000\n")
        for path in ("pkg/units.py", str(self.workspace / "pkg/units.py")):
            process = self.single(path, "--symbol", "millis")
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)["source"], "def millis(x):\n    return x * 1000\n")
        temp = self.directory / "tmp"
        store = temp / ("claude-ork-meter-%d" % getattr(os, "getuid", lambda: 0)()) / "session-1" / "ork-meter"
        (store / "tool-output").mkdir(parents=True)
        (store / "tool-output" / "abc.txt").write_bytes(b"saved output\n")
        (store / "session.json").write_bytes(b"{}\n")
        env = dict(os.environ, TMPDIR=str(temp))
        process = self.single(str(store / "tool-output" / "abc.txt"), env=env)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["source"], "saved output\n")
        self.assert_refused(self.single(str(store / "session.json"), env=env))


if __name__ == "__main__":
    unittest.main()
