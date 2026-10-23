"""Emission-boundary accounting: abandoned replies cannot claim reductions."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import load, run_hook  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import hook
import ledger
import session


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.scratch = Path(self.temporary.name) / "scratch"
        stdout = "".join("test case_%04d ... ok\n" % i for i in range(1500))
        stdout += "".join("log_%04d " % i + "context " * 20 + "\n" for i in range(1500))
        self.payload = {"hook_event_name": "PostToolUse", "session_id": "delivery",
                        "scratchpad_dir": str(self.scratch), "tool_name": "Bash", "tool_use_id": "call",
                        "tool_input": {"command": "cargo test"}, "tool_response": {
                            "stdout": stdout, "stderr": "", "exitCode": 0, "isImage": False}}
        self.store = session.Store.for_scratchpad(str(self.scratch))
        self.reducer = load("hooks/trim-tool-output.py")

    def invoke(self, handler=None):
        return run_hook(self.payload, handler or self.reducer.handle, raw=True)

    def test_successful_emission_keeps_stage_totals_and_matches_returned_bytes(self):
        output = self.invoke()
        response = json.loads(output)["hookSpecificOutput"]["updatedToolOutput"]
        rows = ledger.load(self.store)
        self.assertEqual([r["kind"] for r in rows], ["test_filter", "trim"])
        self.assertEqual(rows[-1]["bytes_after"], len(response["stdout"].encode()) + len(response["stderr"].encode()))
        self.assertEqual(sum(r["bytes_before"] - r["bytes_after"] for r in rows),
                         len(self.payload["tool_response"]["stdout"].encode()) - len(response["stdout"].encode()))
        self.assertEqual(self.store.accounting_gaps(), [])

    def test_each_append_failure_keeps_emitted_replacement_and_exposes_accounting_gap(self):
        original = ledger.append
        for failure in (1, 2):
            with self.subTest(failure=failure):
                if Path(self.store.ledger_path).exists():
                    Path(self.store.ledger_path).unlink()
                before_gaps = len(self.store.accounting_gaps())
                calls = []
                def append(store, row):
                    calls.append(row)
                    if len(calls) == failure:
                        raise OSError("injected ledger failure")
                    original(store, row)
                with patch.object(ledger, "append", side_effect=append):
                    output = self.invoke()
                response = json.loads(output)["hookSpecificOutput"]["updatedToolOutput"]
                self.assertIn("Full output:", response["stdout"])
                self.assertEqual(len(ledger.load(self.store)), failure - 1)
                gaps = self.store.accounting_gaps()
                self.assertEqual(len(gaps), before_gaps + 1)
                matching = [gap for gap in gaps if gap["rows_written"] == failure - 1]
                self.assertTrue(matching[-1]["reply_emitted"])
                self.assertEqual(matching[-1]["rows_planned"], 2)

    def test_result_serialization_failure_has_no_successful_interventions(self):
        original = hook.json.dumps
        def serialize(value, *args, **kwargs):
            if isinstance(value, dict) and "hookSpecificOutput" in value:
                raise TypeError("injected reply serialization failure")
            return original(value, *args, **kwargs)
        with patch.object(hook.json, "dumps", side_effect=serialize):
            output = self.invoke()
        self.assertEqual(output, "")
        self.assertEqual(ledger.load(self.store), [])

    def test_handler_failure_and_no_reply_cannot_commit_byte_interventions(self):
        for failure in (True, False):
            with self.subTest(failure=failure):
                def handle(ctx):
                    ctx.record(ledger.trimmed("Bash", 10000, 100))
                    if failure:
                        raise ValueError("abandoned")
                    return None
                self.assertEqual(self.invoke(handle), "")
                self.assertEqual(ledger.load(self.store), [])

    def test_stdout_failure_cannot_commit_interventions(self):
        class FailedOutput:
            def write(self, _):
                raise OSError("broken pipe")
            def flush(self):
                raise AssertionError("write failed")
        with patch.object(sys, "stdin", io.StringIO(json.dumps(self.payload))), patch.object(sys, "stdout", FailedOutput()):
            hook.run(("PostToolUse",), self.reducer.handle)
        self.assertEqual(ledger.load(self.store), [])

    def test_silent_count_only_events_still_commit(self):
        def handle(ctx):
            ctx.record(ledger.loop("Bash", 3, False))
        self.assertEqual(self.invoke(handle), "")
        self.assertEqual(ledger.load(self.store)[0]["kind"], "loop_warning")

    def test_first_append_failure_remains_discoverable_and_reported_as_partial(self):
        with patch.object(ledger, "append", side_effect=OSError("disk full")):
            self.assertTrue(self.invoke())
        report = load("scripts/savings.py")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.assertEqual(report.main(["--scratchpad", str(self.scratch), "--no-measure", "--json"]), 0)
        value = json.loads(captured.getvalue())
        self.assertEqual(value["accounting_status"], "partial")
        self.assertEqual(value["accounting_gap_count"], 1)
        self.assertEqual(value["estimated"]["totals"]["est_tokens_avoided"], 0)

    def test_all_sessions_includes_gap_only_stores(self):
        root = Path(self.temporary.name) / "sessions"
        scratch = root / "project" / "session" / "scratchpad"
        store = session.Store.for_payload({"session_id": "gap-only", "scratchpad_dir": str(scratch)})
        session.write_json(store.accounting_gap_path("failure"), {"reply_emitted": True, "rows_written": 0})
        self.assertFalse(Path(store.ledger_path).exists())
        self.assertIn(store, session.Store.all({}, roots=[str(root)]))


if __name__ == "__main__":
    unittest.main()
