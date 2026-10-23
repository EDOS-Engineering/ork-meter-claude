"""Actual exit-7 command capture through installed Claude, localhost inference."""
import json
import os
import re
from pathlib import Path
import shlex
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_host import Fixture, Host  # noqa: E402
import entitled  # noqa: E402
import host_retry  # noqa: E402
import artifacts  # noqa: E402
import ledger  # noqa: E402
import session  # noqa: E402


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "installed host + localhost opt-in")
@host_retry.retry_once
class NativeFailedCapture(unittest.TestCase):
    def test_actual_nonzero_command_is_captured_before_native_error_rendering(self):
        self.qualify()

    def test_soft_budget_overflow_recovers_every_distinct_complete_diagnostic(self):
        self.qualify(overflow=True)

    def qualify(self, overflow=False):
        with Host(self, "ork-native-failure-", "Bash,Read", inference=Fixture) as host:
            base, plugin, work = host.base, host.plugin, host.work
            original = "".join("row_%04d context\n" % i for i in range(3000))
            original = original.replace("row_1500 context", "ERROR: middle-first").replace("row_1510 context", "ERROR: middle-second")
            diagnostics = []
            if overflow:
                diagnostics = ["ERROR: diagnostic_%03d expected %s got %s" % (i, "A" * 300, "B" * 300) for i in range(75)]
                original = "".join("ordinary_%04d\n" % i for i in range(1000)) + "\n".join(diagnostics) + "\n"
            program = work / "failure.py"
            counter = work / "executions.txt"
            program.write_text("from pathlib import Path\nimport sys\np=Path(%r)\np.write_text(p.read_text()+'x' if p.exists() else 'x')\ns=''.join('row_%%04d context\\n'%%i for i in range(3000))\ns=s.replace('row_1500 context','ERROR: middle-first').replace('row_1510 context','ERROR: middle-second')\nsys.stdout.buffer.write(s.encode())\nsys.stderr.buffer.write(b'fatal: separate stderr\\r\\n')\nsys.exit(7)\n" % str(counter))
            if overflow:
                payload = work / "diagnostics.bin"
                payload.write_bytes(original.encode())
                program.write_text("from pathlib import Path\nimport sys\np=Path(%r)\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nsys.stdout.buffer.write(Path(%r).read_bytes())\nsys.stderr.buffer.write(b'fatal: separate stderr\\r\\n')\nsys.exit(7)\n" % (str(counter), str(payload)))
            origin = base / "capture"
            quoted = lambda path: shlex.quote(str(path).replace("\\", "/"))
            command = " ".join([quoted(sys.executable), "-I", "-B", quoted(plugin / "scripts/command.py"),
                                "--scratchpad", quoted(origin),
                                "--plugin-data", quoted(entitled.write(str(base / "plugin-data"))),
                                "--", quoted(sys.executable), quoted(program)])
            recovered = []
            def recover_failure(_body):
                store = session.Store.for_scratchpad(str(origin))
                files = list(Path(store.tool_output_dir()).glob("*.json"))
                self.assertEqual(len(files), 1)
                recovered.append(artifacts.load(store, files[0].stem)["response"])
                cli = " ".join([quoted(sys.executable), "-I", "-B", quoted(plugin / "scripts/artifact.py"),
                                "--scratchpad", quoted(origin), "--id", files[0].stem, "--full"])
                return {"command": cli}
            def read_persisted(body):
                entries = [x for message in body["messages"] if isinstance(message.get("content"), list)
                           for x in message["content"] if x.get("type") == "tool_result" and x.get("tool_use_id") == "native_2"]
                raw = entries[0]["content"]
                if isinstance(raw, list):
                    raw = "".join(x.get("text", "") for x in raw)
                match = re.search(r"Full output saved to: ([^\r\n]+)", raw)
                self.assertIsNotNone(match)
                self.assertEqual(json.loads(Path(match.group(1)).read_text()), recovered[0])
                return {"file_path": match.group(1), "offset": 1, "limit": 10000}
            Fixture.calls = [("Bash", {"command": command}), ("Bash", recover_failure), ("Read", read_persisted)]
            Fixture.requests, Fixture.arrivals = [], []
            host.run("Run the fixture once.", system="Inspect the captured command failure.")
            host.assert_no_external_traffic()
            self.assertEqual(counter.read_text(), "x")
            self.assertEqual(len(Fixture.requests), 4)
            results = [x for message in Fixture.requests[1]["messages"] if isinstance(message.get("content"), list)
                       for x in message["content"] if x.get("type") == "tool_result" and x.get("tool_use_id") == "native_1"]
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].get("is_error", False), "real exit 7 must remain a failed tool call")
            visible = json.dumps(results[0]["content"])
            markers = ("Full output:", "diagnostic_000") if overflow else ("Full output:", "middle-first", "middle-second", "fatal: separate stderr")
            for marker in markers:
                self.assertIn(marker, visible)
            store = session.Store.for_scratchpad(str(origin))
            files = list(Path(store.tool_output_dir()).glob("*.json"))
            self.assertEqual(len(files), 1)
            captured = artifacts.load(store, files[0].stem)
            self.assertTrue(captured["response"]["stdout"] == original)
            self.assertEqual(captured["response"]["stderr"], "fatal: separate stderr\r\n")
            self.assertEqual(captured["response"]["exitCode"], 7)
            self.assertEqual(captured["completeness"]["before_hook"], "complete")
            self.assertEqual([r["kind"] for r in ledger.load(store)], ["trim", "recovery"])
            final = [x for message in Fixture.requests[3]["messages"] if isinstance(message.get("content"), list)
                     for x in message["content"] if x.get("type") == "tool_result" and x.get("tool_use_id") == "native_3"]
            self.assertFalse(final[0].get("is_error", False))
            raw = final[0]["content"]
            if isinstance(raw, list):
                raw = "".join(x.get("text", "") for x in raw)
            raw = re.sub(r"^\s*\d+(?:→|\t|$)", "", raw, flags=re.MULTILINE)
            value, end = json.JSONDecoder().raw_decode(raw)
            self.assertEqual(value, recovered[0], "full failed-command response must reach a later model request")
            if overflow:
                for diagnostic in diagnostics:
                    self.assertIn(diagnostic + "\n", value["stdout"])
                self.assertEqual(value["stdout"].count("ERROR: diagnostic_"), 75)
                row = ledger.load(store)[0]
                self.assertGreater(row["bytes_after"], 4096, "complete diagnostics overflow the soft failure budget")
            else:
                self.assertIn("row_2999", value["stdout"])
            self.assertEqual(value["exitCode"], 7)
            self.assertEqual(len(list(Path(store.tool_output_dir()).glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
