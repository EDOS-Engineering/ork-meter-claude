"""Exercise the actual isolated hook entry point with native Python on every OS."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import concurrent.futures
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, clean_environ  # noqa: E402
RUNTIME = ROOT / "hooks/lib/run.py"
import entitled  # noqa: E402
entitled.install()


class NativeHooks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = clean_environ(dict(os.environ, HOME=str(self.root), XDG_STATE_HOME=str(self.root / "state")))
        self.payload = {"hook_event_name": "SessionStart", "source": "startup", "session_id": "native-test",
                        "scratchpad_dir": str(self.root / "scratch")}

    def run_hook(self, name, data, env=None):
        result = subprocess.run([sys.executable, "-I", "-B", str(RUNTIME), str(ROOT / "hooks" / name)],
                                input=data, text=True, capture_output=True, cwd=self.root,
                                env=env or self.env, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def test_malformed_payload_fails_open(self):
        self.assertEqual(self.run_hook("trim-session.py", "{invalid"), "")

    def test_wrong_event_is_silent(self):
        self.payload["hook_event_name"] = "PreToolUse"
        self.assertEqual(self.run_hook("trim-session.py", json.dumps(self.payload)), "")

    def test_startup_writes_private_session_state(self):
        reply = json.loads(self.run_hook("trim-session.py", json.dumps(self.payload)))
        self.assertTrue(reply["systemMessage"].startswith("Ork Meter trim: on"))
        state = json.loads((self.root / "state/ork-meter/plugin-features.json").read_text())
        self.assertIn("trim", state["features"])
        self.assertTrue((self.root / "scratch/ork-meter").is_dir())

    def test_imports_ignore_current_directory_and_pythonpath(self):
        (self.root / "features.py").write_text('raise RuntimeError("untrusted import")\n')
        env = dict(self.env, PYTHONPATH=str(self.root))
        reply = json.loads(self.run_hook("trim-session.py", json.dumps(self.payload), env))
        self.assertTrue(reply["systemMessage"].startswith("Ork Meter trim: on"))

    def test_disabled_trim_does_not_write_tool_output(self):
        payload = dict(self.payload, hook_event_name="PostToolUse", tool_name="Bash",
                       tool_input={"command": "echo"}, tool_response={"stdout": "x" * 20000,
                       "stderr": "", "isImage": False, "interrupted": False})
        self.assertEqual(self.run_hook("trim-tool-output.py", json.dumps(payload),
                                     dict(self.env, ORK_METER_TRIM="off")), "")
        # Disabled sessions still pin configuration metadata, not tool data.
        self.assertFalse(list((self.root / "scratch").rglob("tool-output")))
        self.assertFalse(list((self.root / "scratch").rglob("savings.jsonl")))
        self.assertFalse(list((self.root / "scratch").rglob("shadow-traces")))

    @unittest.skipUnless(subprocess.run([sys.executable, "-I", "-c", "import tiktoken, regex"],
                                        capture_output=True).returncode == 0, "offline tokenizer not installed")
    def test_cold_process_trim_replies_within_its_budget(self):
        # The native qualification's Bash output, in a fresh process: the
        # reply must survive hook.py's deadline and the token gate, whose
        # vocabulary load dominates a cold invocation (#140). The wall time
        # goes to stderr so slow runners can be told apart from a lost reply.
        stdout = "".join("line_%04d " % i + "context " * 5 + "\n" for i in range(450))
        payload = dict(self.payload, hook_event_name="PostToolUse", tool_name="Bash", tool_use_id="cold-trim",
                       tool_input={"command": "python3 output.py"}, tool_response={"stdout": stdout,
                       "stderr": "fatal: separate stderr", "isImage": False, "interrupted": False})
        started = time.monotonic()
        out = self.run_hook("trim-tool-output.py", json.dumps(payload), dict(self.env, ORK_METER_MODE="reduce"))
        elapsed = time.monotonic() - started
        print("\n  cold trim-tool-output wall time: %.2fs (hooks.json timeout 2s)" % elapsed, file=sys.stderr)
        self.assertIn("Full output:", out, "no reply after %.2fs" % elapsed)

    def test_concurrent_process_startup_keeps_every_shadow_observation(self):
        # Synchronize separate processes just before the production runner.
        # Zero exit status alone cannot detect work swallowed by fail-open.
        helper = self.root / "invoke.py"
        helper.write_text('''import importlib.util, pathlib, sys, time
ready, gate, runner, target = sys.argv[1:]
pathlib.Path(ready).write_text("ready")
until = time.monotonic() + 10
while not pathlib.Path(gate).exists():
    if time.monotonic() > until: raise TimeoutError("startup gate")
    time.sleep(0.005)
spec = importlib.util.spec_from_file_location("runner", runner)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.main([runner, target])
''')
        for round_ in range(3):
            directory = self.root / ("round-%d" % round_)
            directory.mkdir()
            scratch = directory / "scratch"
            gate = directory / "go"
            payloads = []
            processes = []
            for i in range(12):
                payload = dict(self.payload, hook_event_name="PostToolUse", session_id="process-round-%d" % round_,
                               scratchpad_dir=str(scratch), tool_name="Bash", tool_use_id="call-%d" % i,
                               tool_input={"command": "fixture"}, tool_response={"stdout": str(i), "stderr": "", "exitCode": i % 3})
                payloads.append(payload)
                process = subprocess.Popen([sys.executable, "-I", "-B", str(helper), str(directory / ("ready-%d" % i)),
                                            str(gate), str(RUNTIME), str(ROOT / "hooks/trim-tool-output.py")],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           text=True, env=dict(self.env, ORK_METER_MODE="shadow"))
                processes.append(process)
                self.addCleanup(lambda p=process: p.kill() if p.poll() is None else None)
            deadline = time.monotonic() + 10
            while len(list(directory.glob("ready-*"))) < len(processes):
                self.assertLess(time.monotonic(), deadline, "process startup timed out")
                time.sleep(0.005)
            gate.write_text("go")
            def finish(item):
                process, payload = item
                output, error = process.communicate(json.dumps(payload), timeout=10)
                return process.returncode, output, error
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(processes)) as pool:
                results = list(pool.map(finish, zip(processes, payloads)))
            self.assertEqual(results, [(0, "", "")] * len(processes))
            store = scratch / "ork-meter"
            metadata = json.loads((store / "session.json").read_text())
            self.assertEqual(metadata["session_id"], payloads[0]["session_id"])
            traces = [json.loads(path.read_text()) for path in (store / "shadow-traces").glob("*.json")]
            self.assertEqual({t["tool_use_id"] for t in traces}, {p["tool_use_id"] for p in payloads})
            self.assertEqual(len(traces), len(processes), "silent capture losses are failures")
            self.assertFalse(list(store.rglob("*.tmp")), "startup must not leave partial files")
            for trace in traces:
                i = int(trace["tool_use_id"].split("-")[1])
                self.assertEqual(trace["tool_response"]["exitCode"], i % 3)
            if os.name != "nt":
                self.assertEqual((store / "session.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
