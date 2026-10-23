"""Real subprocess capture: failure status, exact bytes, and one execution."""
import base64
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, StoreCase, clean_environ, load, run_like_bash_tool  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import artifacts  # noqa: E402
import ledger  # noqa: E402


class CommandCapture(StoreCase):
    def test_test_filter_classifies_the_executable_not_arguments_or_shell_tails(self):
        import trimmer
        for command in ("cat pytest.log", "echo pytest", "python -c 'print(\" go test \" )'",
                        "pytest\ncat report.txt", "pytest |cat report.txt", "pytest -q&&cat report.txt",
                        'pytest "$(cat report.txt)"', 'pytest "`cat report.txt`"'):
            with self.subTest(command=command):
                self.assertFalse(trimmer.is_test_command(command))
        for command in ("go test -v ./...", "python3.12 -I -m pytest -v", "cargo nextest run",
                        "npm run test", "npx vitest", "mvn -q test", "./gradlew test",
                        "cd project && pytest -v", "bash -lc 'pytest -v'", "pytest 'test.py::test[a|b]'"):
            with self.subTest(command=command):
                self.assertTrue(trimmer.is_test_command(command))

    def test_small_parameterized_passes_survive_filter_and_middle_elision(self):
        import trimmer
        passes = "".join(f"tests/test_math.py::test_add[{i}] PASSED [100%]\n" for i in range(8))
        text = "".join(f"ordinary setup {i}\n" for i in range(1000)) + passes
        text += "".join(f"=== RUN   Test{i}\n--- PASS: Test{i} (0.00s)\n" for i in range(1000))
        text += "".join(f"ordinary teardown {i}\n" for i in range(1000))
        filtered, omitted = trimmer.filter_test_output(text)
        output, info = trimmer.trim_text(filtered, 1024)
        self.assertEqual(omitted, 2000)
        self.assertIsNotNone(info)
        self.assertLess(trimmer.nbytes(output), trimmer.nbytes(text))
        for line in passes.splitlines():
            self.assertIn(line, output)

    def test_go_filter_preserves_active_tests_without_a_pass_record(self):
        import trimmer
        text = "".join(f"=== RUN   TestPassed{i}\n--- PASS: TestPassed{i} (0.00s)\n" for i in range(50))
        text += "=== RUN   TestActive\n=== PAUSE TestActive\n=== CONT  TestActive\npanic: interrupted\n"
        # A previous pass of the same name must not hide a later interrupted
        # execution (go test -count, or multiple packages sharing a test name).
        text += "=== RUN   TestPassed0\npanic: second execution interrupted\n"
        filtered, omitted = trimmer.filter_test_output(text)
        self.assertEqual(omitted, 100)
        for line in ("=== RUN   TestActive", "=== PAUSE TestActive", "=== CONT  TestActive", "panic: interrupted"):
            self.assertIn(line, filtered)
        self.assertEqual(filtered.count("=== RUN   TestPassed0\n"), 1)

    def test_pytest_argv_recognition_does_not_inspect_embedded_program_text(self):
        capture = load("scripts/command.py")
        self.assertTrue(capture.is_pytest_argv([sys.executable, "-I", "-m", "pytest", "-v"]))
        self.assertTrue(capture.is_pytest_argv(["pytest.exe", "-v"]))
        self.assertFalse(capture.is_pytest_argv([sys.executable, "-c", "print('pytest')", "-m", "pytest"]))
        self.assertFalse(capture.is_pytest_argv(["bash", "-c", "pytest -v"]))

    def test_pytest_argv_recognition_accepts_versioned_interpreters(self):
        # Homebrew's sys.executable is python3.15, not python3; CI's is python.
        capture = load("scripts/command.py")
        for name in ("python", "python3", "python3.10", "python3.15", "python3.13t", "python3.12.exe",
                     "/opt/homebrew/opt/python@3.15/bin/python3.15", "PYTHON3.EXE"):
            self.assertTrue(capture.is_pytest_argv([name, "-m", "pytest"]), name)
        for name in ("python3-config", "python3.15-config", "pythonista", "python2.7", "python3.", "python3.123", "ipython3"):
            self.assertFalse(capture.is_pytest_argv([name, "-m", "pytest"]), name)

    def test_direct_test_capture_filters_passes_but_preserves_skip_failure_and_original(self):
        original = "".join("tests/test_sample.py::test_%04d PASSED [ 50%%]\n" % i for i in range(300))
        original += "tests/test_sample.py::test_security SKIPPED (needs credentials)\nFAILED tests/test_sample.py::test_bug\n1 failed, 300 passed, 1 skipped\n"
        (self.root / "pytest.py").write_text("import sys\nsys.stdout.buffer.write(" + repr(original.encode()) + ")\nsys.exit(1)\n", encoding="utf-8")
        result = run_like_bash_tool([sys.executable, "-I", "-B", str(ROOT / "scripts/command.py"),
            "--scratchpad", str(self.scratch), "--", sys.executable, "-m", "pytest"],
            env=self.env, cwd=self.root, timeout=10)
        self.assertEqual(result.returncode, 1)
        output = result.stdout.decode()
        self.assertNotIn("test_0000 PASSED", output)
        self.assertIn("SKIPPED (needs credentials)", output)
        self.assertIn("FAILED tests/test_sample.py::test_bug", output)
        self.assertIn("1 failed, 300 passed, 1 skipped", output)
        _, saved = self.saved()
        self.assertEqual(saved["response"]["stdout"], original)
        self.assertLess(len(result.stdout), len(original.encode()))

    def test_pytest_filter_keeps_few_named_passes_amid_parameterized_noise(self):
        import trimmer
        original = "".join("tests/test_slug.py::test_stable[%d] PASSED [ 50%%]\n" % i for i in range(300))
        original += "tests/test_slug.py::test_repeated_whitespace PASSED [ 99%]\n"
        original += "tests/test_slug.py::test_security SKIPPED (needs credentials)\n301 passed, 1 skipped\n"
        filtered, omitted = trimmer.filter_test_output(original)
        self.assertEqual(omitted, 300)
        self.assertIn("test_repeated_whitespace PASSED", filtered)
        self.assertIn("test_security SKIPPED (needs credentials)", filtered)
        self.assertIn("301 passed, 1 skipped", filtered)
        self.assertNotIn("test_stable[0] PASSED", filtered)

    def setUp(self):
        super().setUp()
        self.env = clean_environ()

    def run_command(self, program, extra=(), input_=None):
        return run_like_bash_tool([sys.executable, "-I", "-B", str(ROOT / "scripts/command.py"),
                                   "--scratchpad", str(self.scratch), *extra, "--", sys.executable, "-c", program],
                                  input=input_, env=self.env, cwd=self.root, timeout=10)

    def saved(self):
        files = list(Path(self.store.tool_output_dir()).glob("*.json"))
        self.assertEqual(len(files), 1)
        return files[0], artifacts.load(self.store, files[0].stem)

    def test_nonzero_exit_has_middle_errors_and_exact_original_streams(self):
        original = "".join("row_%04d context\n" % i for i in range(3000))
        original = original.replace("row_1500 context", "ERROR: middle-first").replace("row_1510 context", "ERROR: middle-second")
        program = "import sys;s=''.join('row_%04d context\\n'%i for i in range(3000));s=s.replace('row_1500 context','ERROR: middle-first').replace('row_1510 context','ERROR: middle-second');sys.stdout.buffer.write(s.encode());sys.stderr.buffer.write(b'fatal: separate stderr\\r\\n');sys.exit(7)"
        result = self.run_command(program)
        self.assertEqual(result.returncode, 7)
        for marker in (b"ERROR: middle-first", b"ERROR: middle-second"):
            self.assertIn(marker, result.stdout)
        self.assertIn(b"Full output:", result.stdout)
        self.assertIn(b"fatal: separate stderr", result.stderr)
        path, document = self.saved()
        response = document["response"]
        self.assertTrue(response["stdout"] == original, "captured stdout must match the exact emitted fixture bytes")
        self.assertEqual(response["stderr"], "fatal: separate stderr\r\n")
        self.assertEqual(response["exitCode"], 7)
        self.assertEqual(document["completeness"]["before_hook"], "complete")
        self.assertEqual(ledger.load(self.store)[0]["bytes_after"], len(result.stdout) + len(result.stderr))
        restored = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/artifact.py"),
                                   "--scratchpad", str(self.scratch), "--id", path.stem, "--stream", "stdout"], capture_output=True)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(restored.stdout, original.encode())

    def test_full_and_modes_preserve_argv_cwd_environment_stdin_and_streams(self):
        program = "import os,sys;print(os.getcwd());print(os.environ['CAPTURE_SENTINEL']);print(repr(sys.stdin.buffer.read()));sys.stderr.buffer.write(b'err\\r\\n');sys.exit(3)"
        self.env["CAPTURE_SENTINEL"] = "kept"
        direct = subprocess.run([sys.executable, "-c", program], input=b"input\x00bytes", capture_output=True,
                                cwd=self.root, env=self.env)
        for mode, flags in (("reduce", ("--full",)), ("off", ()), ("shadow", ())):
            self.env["ORK_METER_MODE"] = mode
            result = self.run_command(program, flags, b"input\x00bytes")
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (direct.returncode, direct.stdout, direct.stderr))
        self.assertEqual(ledger.load(self.store), [])

    def test_binary_passes_through_and_recovers_original_bytes(self):
        raw = b"\x00\xff\xfeabc\r\n" * 5000
        result = self.run_command("import sys;sys.stdout.buffer.write(b'\\x00\\xff\\xfeabc\\r\\n'*5000)")
        self.assertEqual(result.stdout, raw)
        path, document = self.saved()
        self.assertEqual(document["response"]["stream_encodings"]["stdout"], "base64")
        self.assertEqual(base64.b64decode(document["response"]["stdout"]), raw)
        restored = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/artifact.py"),
                                   "--scratchpad", str(self.scratch), "--id", path.stem, "--stream", "stdout"], capture_output=True)
        self.assertEqual(restored.stdout, raw)

    def test_storage_failure_passes_captured_output_without_second_execution(self):
        capture = load("scripts/command.py")
        marker = self.root / "executions"
        program = "from pathlib import Path;import sys;p=Path(%r);p.write_text(p.read_text()+'x' if p.exists() else 'x');sys.stdout.buffer.write(b'line\\n'*10000);sys.exit(7)" % str(marker)
        emitted = []
        with patch.object(artifacts, "save", side_effect=OSError("disk full")), \
             patch.object(capture, "emit", side_effect=lambda out, err: emitted.append((out, err))):
            code = capture.run([sys.executable, "-c", program], self.store, {})
        self.assertEqual(code, 7)
        self.assertEqual(marker.read_text(), "x")
        self.assertEqual(len(emitted), 1)
        self.assertTrue(emitted[0] == (b"line\n" * 10000, b""), "storage failure must return exact captured streams")
        self.assertEqual(ledger.load(self.store), [])

    def test_emit_failure_cannot_record_successful_reduction(self):
        capture = load("scripts/command.py")
        with patch.object(capture, "emit", side_effect=OSError("broken pipe")), \
                patch.object(capture, "to_regular_files", return_value=True), self.assertRaises(OSError):
            capture.run([sys.executable, "-c", "print('line\\n'*10000)"], self.store,
                        {"CLAUDE_PLUGIN_DATA": os.environ["CLAUDE_PLUGIN_DATA"]})
        self.assertEqual(ledger.load(self.store), [])

    def test_accounting_failure_preserves_emission_and_reports_gap(self):
        capture = load("scripts/command.py")
        emitted = []
        with patch.object(ledger, "append", side_effect=OSError("disk full")), \
             patch.object(capture, "emit", side_effect=lambda out, err: emitted.append(out)), \
             patch.object(capture, "to_regular_files", return_value=True):
            capture.run([sys.executable, "-c", "print('line\\n'*10000)"], self.store,
                        {"CLAUDE_PLUGIN_DATA": os.environ["CLAUDE_PLUGIN_DATA"]})
        self.assertIn(b"Full output:", emitted[0])
        self.assertEqual(self.store.accounting_gaps()[0]["event"], "command-capture")


if __name__ == "__main__":
    unittest.main()
