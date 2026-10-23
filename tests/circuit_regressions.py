"""Distinct internal failures, tool isolation, status invariants and recovery."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, StoreCase, clean_environ, load, payload, run_hook  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import artifacts  # noqa: E402
import circuit  # noqa: E402
import features  # noqa: E402
import hook  # noqa: E402
import ledger  # noqa: E402
import session  # noqa: E402


class ReductionCircuits(StoreCase):
    def setUp(self):
        super().setUp()
        self.payload = payload(self.scratch, "Bash", response={"stdout": "original", "stderr": "failure evidence",
                                                              "exitCode": 7, "interrupted": False},
                               session_id="circuit-" + self.root.name, hook_event_name="PostToolUse", tool_use_id="first")
        self.env = dict(clean_environ(), XDG_STATE_HOME=str(self.root / "state"))

    def invoke(self, handler, reducer="trim-tool-output", payload=None):
        return run_hook(payload or self.payload, handler, env=self.env, raw=True, feature="trim", reducer=reducer)

    def test_repeated_observation_is_one_fault_and_distinct_calls_disable_only_bash(self):
        calls = []
        def broken(ctx):
            calls.append(ctx.payload["tool_use_id"])
            raise RuntimeError("secret command and raw error must not be stored")
        self.assertEqual(self.invoke(broken), "")
        self.assertEqual(self.invoke(broken), "")
        self.assertEqual(circuit.read(self.store, "trim-tool-output:Bash")["faults"], 1)
        self.payload["tool_use_id"] = "second"
        self.assertEqual(self.invoke(broken), "")
        self.payload["tool_use_id"] = "third"
        self.assertEqual(self.invoke(broken), "")
        self.assertEqual(calls, ["first", "first", "second"])
        other = dict(self.payload, tool_name="Grep", tool_response={"content": "evidence", "code": 200})
        self.assertEqual(json.loads(self.invoke(lambda ctx: {"systemMessage": "unaffected"}, payload=other)), {"systemMessage": "unaffected"})
        files = list(Path(self.store.path).rglob("*.json"))
        self.assertNotIn("secret command", "".join(path.read_text() for path in files))
        self.assertEqual(ledger.load(self.store), [])

    def test_status_change_is_rejected_before_emission_and_byte_accounting(self):
        def corrupt(ctx):
            ctx.record(ledger.trimmed("Bash", 1000, 100))
            return hook.updated_tool_output(ctx.event, dict(ctx.payload["tool_response"], exitCode=0))
        for identity in ("first", "second", "third"):
            self.payload["tool_use_id"] = identity
            self.assertEqual(self.invoke(corrupt), "")
        state = circuit.read(self.store, "trim-tool-output:Bash")
        self.assertTrue(state["disabled"])
        self.assertIn("invariant:InvariantError", state["failure_categories"])
        self.assertEqual(ledger.load(self.store), [])

    def test_serialization_faults_count_but_stdout_or_accounting_failures_do_not(self):
        for identity in ("first", "second"):
            self.payload["tool_use_id"] = identity
            self.assertEqual(self.invoke(lambda _: {"not_json": {1}}), "")
        self.assertTrue(circuit.read(self.store, "trim-tool-output:Bash")["disabled"])
        other = dict(self.payload, tool_name="Grep", tool_response={"content": "evidence"}, tool_use_id="other")
        class BrokenOutput(io.StringIO):
            def write(self, _value):
                raise BrokenPipeError("closed client")
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "stdin", io.StringIO(json.dumps(other))), \
                contextlib.redirect_stdout(BrokenOutput()):
            hook.run(("PostToolUse",), lambda _: {"systemMessage": "ok"}, reducer="trim-tool-output")
        self.assertEqual(circuit.read(self.store, "trim-tool-output:Grep")["faults"], 0)

    def test_quarantine_survives_compaction_and_does_not_hide_exact_recovery(self):
        session.Store.for_payload(self.payload).remember(self.payload)
        raw = {"stdout": "immutable original", "stderr": "important error", "exitCode": 7}
        path = artifacts.save(self.store, "Bash", raw, raw["stdout"])
        for identity in ("one", "two"):
            circuit.record(self.store, "trim-tool-output:Bash", identity, "pin", "handler", "RuntimeError", self.payload["session_id"])
        self.store.reset()
        self.assertTrue(circuit.read(self.store, "trim-tool-output:Bash")["disabled"])
        with patch.object(circuit, "THRESHOLD", 100):
            self.assertTrue(circuit.read(self.store, "trim-tool-output:Bash")["disabled"])
        command = [sys.executable, "-I", "-B", str(ROOT / "scripts/artifact.py"), "--scratchpad",
                   self.payload["scratchpad_dir"], "--id", Path(path).stem, "--full"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), raw)
        report = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/savings.py"), "--scratchpad",
                                 self.payload["scratchpad_dir"], "--no-measure", "--json"], capture_output=True, text=True)
        self.assertEqual(json.loads(report.stdout)["policy"]["reducer_health"]["entries"][0]["scope"], "trim-tool-output:Bash")
        readable = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "scripts/savings.py"), "--scratchpad",
                                   self.payload["scratchpad_dir"], "--no-measure"], capture_output=True, text=True)
        self.assertIn("Disabled automatic reducers: trim-tool-output:Bash", readable.stdout)

    def test_concurrent_fault_publication_keeps_all_independent_calls(self):
        def record(number):
            circuit.record(self.store, "trim-tool-output:Bash", str(number), "pin", "handler", "RuntimeError", "session")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(record, range(12)))
        state = circuit.read(self.store, "trim-tool-output:Bash")
        self.assertEqual(state["faults"], 12)
        self.assertTrue(state["disabled"])

    def test_damaged_diagnostics_disable_affected_scope_without_repair(self):
        directory = Path(self.store.reducer_health_path(circuit.sha("trim-tool-output:Bash"), create=True))
        path = directory / ("0" * 64 + ".json")
        path.write_bytes(b'{"incomplete":')
        self.assertEqual(circuit.read(self.store, "trim-tool-output:Bash")["status"], "unavailable")
        self.assertFalse(circuit.read(self.store, "trim-tool-output:Grep")["disabled"])
        self.assertEqual(path.read_bytes(), b'{"incomplete":')

    def test_entire_unavailable_family_is_removed_from_platform_flags(self):
        for identity in ("one", "two"):
            circuit.record(self.store, "read-guard:Read", identity, "pin", "handler", "OSError", self.payload["session_id"])
        ctx = hook.Context(self.env, dict(self.payload, tool_name="Read"), "PreToolUse", time.monotonic() + 5)
        self.assertTrue(circuit.bind(ctx, "read-guard"))
        self.assertFalse(features.enabled(ctx.env, "read_guard"))
        self.assertTrue(features.enabled(ctx.env, "trim"))
        flags = json.loads(Path(features.features_path(ctx.env, self.payload["session_id"])).read_text())
        self.assertEqual(set(flags), {"plugin_version", "features", "updated_at"})
        self.assertNotIn("read_guard", flags["features"])

    def test_command_reducer_exception_preserves_status_streams_and_one_execution(self):
        command = load("scripts/command.py")
        raw = b'important evidence\n' * 3000
        captured, emitted = [], []
        def capture(*_args):
            captured.append(1)
            return raw, b'stderr\r\n', 7, False, "test"
        with patch.object(command, "capture", side_effect=capture), patch.object(command, "emit", side_effect=lambda out, err: emitted.append((out, err))), \
                patch.object(command, "to_regular_files", return_value=True), \
                patch.object(command, "reduce_stream", side_effect=RuntimeError("broken reducer")) as reducer:
            for number in range(3):
                self.assertEqual(command.run(["fixture"], self.store, self.env, call_id=str(number)), 7)
            self.assertEqual(reducer.call_count, 2)
        self.assertEqual(captured, [1, 1, 1])
        self.assertEqual(emitted, [(raw, b'stderr\r\n')] * 3)
        self.assertEqual(ledger.load(self.store), [])
        self.assertTrue(circuit.read(self.store, "command-capture:Bash")["disabled"])


if __name__ == "__main__":
    unittest.main()
