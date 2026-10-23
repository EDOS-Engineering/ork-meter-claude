"""Selected-command cancellation and exact partial recovery through Claude."""
import json
import os
from pathlib import Path
import shlex
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_host import Fixture, Host, latest_tool_results  # noqa: E402
import host_retry  # noqa: E402
import artifacts  # noqa: E402
import ledger  # noqa: E402
import session  # noqa: E402


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeCancellation(unittest.TestCase):
    def test_cancelled_command_recovers_exact_partial_streams_without_rerun(self):
        with Host(self, "ork-native-cancel-", "Bash", inference=Fixture) as host:
            base, plugin, work = host.base, host.plugin, host.work
            (work / "child.py").write_text("import signal,sys,time\nfrom pathlib import Path\n"
                                            "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
                                            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                                            "sys.stdout.buffer.write(b'child partial\\r\\n');sys.stdout.buffer.flush()\n"
                                            "Path('child-ready').touch()\ntime.sleep(10)\nPath('escaped').touch()\n")
            program = work / "parent.py"
            program.write_text("import signal,subprocess,sys,time\nfrom pathlib import Path\n"
                               "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
                               "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                               "p=Path('executions');p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
                               "sys.stdout.buffer.write(b'partial\\n'*100);sys.stdout.buffer.flush()\n"
                               "sys.stderr.buffer.write(b'partial diagnostic\\r\\n');sys.stderr.buffer.flush()\n"
                               "subprocess.Popen([sys.executable,'child.py'])\n"
                               "while not Path('child-ready').exists(): time.sleep(.01)\n"
                               "Path('ready').touch()\ntime.sleep(10)\n")
            cancel = base / "cancel"
            origin = base / "capture"
            quoted = lambda path: shlex.quote(str(path).replace("\\", "/"))
            command = " ".join([quoted(sys.executable), "-I", "-B", quoted(plugin / "scripts/command.py"),
                                "--scratchpad", quoted(origin), "--cancel-file", quoted(cancel),
                                "--", quoted(sys.executable), quoted(program)])
            expected = []
            def recover(_body):
                store = session.Store.for_scratchpad(str(origin))
                paths = list(Path(store.tool_output_dir()).glob("*.json"))
                self.assertEqual(len(paths), 1)
                expected.append(artifacts.load(store, paths[0].stem)["response"])
                return {"command": " ".join([quoted(sys.executable), "-I", "-B", quoted(plugin / "scripts/artifact.py"),
                                               "--scratchpad", quoted(origin), "--id", paths[0].stem, "--full"])}
            Fixture.calls, Fixture.requests, Fixture.arrivals = [("Bash", {"command": command}), ("Bash", recover)], [], []
            stop = threading.Event()
            cancelled = threading.Event()
            def request_cancel():
                deadline = time.monotonic() + 30
                while not stop.wait(.02):
                    if (work / "ready").exists():
                        cancel.touch()
                        cancelled.set()
                        return
                    if time.monotonic() > deadline:
                        return
            cancel_thread = threading.Thread(target=request_cancel, daemon=True)
            cancel_thread.start()
            try:
                host.run("Run the fixture once.", "--max-turns", "4", system="Inspect partial command evidence.")
                host.assert_no_external_traffic()
                self.assertTrue(cancelled.is_set(), "cancellation must actually occur")
                self.assertEqual((work / "executions").read_text(), "x")
                self.assertFalse((work / "escaped").exists())
                self.assertEqual(len(Fixture.requests), 3)
                first = latest_tool_results(Fixture.requests[1])
                self.assertTrue(first[0].get("is_error", False))
                visible = json.dumps(first[0]["content"])
                self.assertIn("child partial", visible)
                self.assertIn("partial diagnostic", visible)
                self.assertTrue(expected[0]["interrupted"])
                self.assertEqual(expected[0]["stdout"].encode(), b"partial\n" * 100 + b"child partial\r\n")
                self.assertEqual(expected[0]["stderr"], "partial diagnostic\r\n")
                final = latest_tool_results(Fixture.requests[2])
                raw = final[0]["content"]
                if isinstance(raw, list):
                    raw = "".join(x.get("text", "") for x in raw)
                recovered, _ = json.JSONDecoder().raw_decode(raw)
                self.assertEqual(recovered, expected[0])
                store = session.Store.for_scratchpad(str(origin))
                self.assertEqual([row["kind"] for row in ledger.load(store)], ["recovery"])
            finally:
                stop.set()
                cancel_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
