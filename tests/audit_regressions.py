"""Behavioral regressions for the token-measurement audit; native Python only."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import load  # noqa: E402
import features
import hook
import ledger
import measure
import session
import token_gate


class AuditRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.usage = dict.fromkeys(measure.USAGE_KEYS, 5)

    def transcript(self, rows):
        path = self.root / "transcript.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return measure.measure_transcript(str(path))

    def row(self, identity="a", usage=None):
        message = {"id": identity}
        if usage is not None:
            message["usage"] = usage
        return {"type": "assistant", "message": message}

    def test_missing_usage_is_partial(self):
        result = self.transcript([self.row(usage=self.usage), self.row("b")])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["prompt_tokens"], 15)

    def test_duplicate_content_block_without_usage_is_not_missing_message(self):
        result = self.transcript([self.row(), self.row(usage=self.usage), self.row()])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["assistant_messages"], 1)

    def test_invalid_counters_and_missing_identity_are_unavailable(self):
        for usage in ({}, {"input_tokens": 0}, dict(self.usage, output_tokens=True),
                      dict(self.usage, output_tokens=-1), dict(self.usage, output_tokens=1.5)):
            self.assertEqual(self.transcript([self.row(usage=usage)])["status"], "unavailable")
        self.assertEqual(self.transcript([self.row(None, self.usage)])["status"], "unavailable")

    def test_conflicting_usage_is_unavailable(self):
        # An output counter growing during streaming is compatible. Mixed
        # directions cannot be a monotone snapshot and must be rejected.
        result = self.transcript([self.row(usage=self.usage),
                                  self.row(usage=dict(self.usage, input_tokens=99, output_tokens=1))])
        self.assertEqual(result["status"], "unavailable")

    def test_explicit_zero_is_known(self):
        result = self.transcript([self.row(usage=dict.fromkeys(measure.USAGE_KEYS, 0))])
        self.assertEqual((result["status"], result["prompt_tokens"]), ("ok", 0))

    def test_aggregate_keeps_partial_and_unavailable_coverage(self):
        savings = load("scripts/savings.py")
        for status in ("partial", "unavailable"):
            result = savings.measured_total([{"measured": {"status": "ok", "prompt_tokens": 12}},
                                             {"measured": {"status": status, "prompt_tokens": 3}}])
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["sessions_total"], 2)
            self.assertEqual(result["sessions_" + status], 1)

    def test_session_feature_files_are_independent(self):
        env = {"XDG_STATE_HOME": str(self.root)}
        features.write_features_file(env, session_id="session-a")
        features.write_features_file(dict(env, ORK_METER_TRIM="off"), session_id="session-b")
        a = features.features_path(env, "session-a")
        b = features.features_path(env, "session-b")
        self.assertNotEqual(a, b)
        self.assertIn("trim", session.read_json(a)["features"])
        self.assertNotIn("trim", session.read_json(b)["features"])
        self.assertNotIn("session-a", Path(a).name)

    def test_live_measurement_rescans_a_resumed_session(self):
        savings = load("scripts/savings.py")
        store = session.Store.for_payload({"scratchpad_dir": str(self.root)})
        transcript = self.root / "resumed.jsonl"
        transcript.write_text(json.dumps(self.row(usage=self.usage)) + "\n")
        session.write_json(store.session_path, {"session_id": "resumed", "transcript_path": str(transcript)})
        session.write_json(store.summary_path, {"measured_from_transcript": {"status": "ok", "prompt_tokens": 1}})
        self.assertEqual(savings.load_session(store)["measured"]["prompt_tokens"], 15)
        self.assertEqual(savings.load_session(store, measure_live=False)["measured"]["prompt_tokens"], 1)

    def test_bash_recovery_note_is_counted_once(self):
        module = load("hooks/trim-tool-output.py")
        for count in (6, 100):
            with self.subTest(count=count):
                text = "\n".join("--- PASS: Test" + "x" * 60 + str(i) + " (0.00s)" for i in range(count))
                # Unclassified diagnostic text forces a second (trim) intervention.
                if count == 100:
                    text += "\n" + "\n".join("diagnostic %d %s" % (i, "x" * 100) for i in range(100))
                scratch = self.root / str(count)
                payload = {"session_id": str(count), "scratchpad_dir": str(scratch), "tool_name": "Bash",
                           "tool_use_id": "fixture", "tool_input": {"command": "go test ./... -v"},
                           "tool_response": {"stdout": text, "stderr": "", "isImage": False, "interrupted": False}}
                ctx = hook.Context({"ORK_METER_TRIM_BUDGET_BASH": "1024"}, payload, "PostToolUse", time.monotonic() + 10)
                # The handler gates its own reply (#130); this output is too
                # small to pass, and only the note accounting is under test.
                with patch.object(token_gate, "permits_reply", return_value=True):
                    reply = module.handle(ctx)
                rows = ledger.load(ctx.store())
                actual = reply["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
                self.assertEqual(rows[-1]["bytes_after"], len(actual.encode("utf-8")))
                self.assertEqual(sum(r["est_tokens_avoided"] for r in rows),
                                 ledger.est_tokens(len(text.encode())) - ledger.est_tokens(len(actual.encode())))

    def test_expanding_error_preservation_does_not_claim_filter_savings(self):
        module = load("hooks/trim-tool-output.py")
        payload = {"session_id": "expanding", "scratchpad_dir": str(self.root), "tool_name": "Bash",
                   "tool_use_id": "expanding", "tool_input": {"command": "go test ./... -v"},
                   "tool_response": {"stdout": "\n".join("--- PASS: Test%d (0.00s)" % i for i in range(6)),
                                     "stderr": "\n".join("error %d %s" % (i, "x" * 180) for i in range(6)),
                                     "isImage": False, "interrupted": False}}
        ctx = hook.Context({"ORK_METER_TRIM_BUDGET_BASH": "1024"}, payload, "PostToolUse", time.monotonic() + 10)
        self.assertIsNone(module.handle(ctx))
        self.assertEqual(ledger.load(ctx.store()), [])


if __name__ == "__main__":
    unittest.main()
