"""Regression checks for stop thresholds, unknown counters and bounded scans."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import context, load  # noqa: E402
import measure
import session
import ledger
import trimmer


class Regressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def loop_context(self, env):
        payload = {"session_id": "regression", "tool_name": "Bash",
                   "tool_input": {"command": "false"}, "scratchpad_dir": str(self.root)}
        return context(payload, env, "PostToolUseFailure")

    def test_stop_before_warning(self):
        module = load("hooks/loop-guard.py")
        for stop in (1, 2):
            with self.subTest(stop=stop):
                ctx = self.loop_context({"ORK_METER_LOOP_STOP": str(stop)})
                ctx.store().reset()
                for _ in range(stop - 1):
                    self.assertIsNone(module.handle(ctx))
                reply = module.handle(ctx)
                self.assertIs(reply["continue"], False)
                self.assertNotIn("systemMessage", reply)
                rows = [json.loads(line) for line in Path(ctx.store().ledger_path).read_text().splitlines()]
                self.assertEqual(rows[-1]["kind"], "loop_stop")
                self.assertEqual(rows[-1]["failures"], stop)

    def test_warning_remains_independent_of_stop(self):
        module = load("hooks/loop-guard.py")
        ctx = self.loop_context({"ORK_METER_LOOP_WARN": "2", "ORK_METER_LOOP_STOP": "3"})
        self.assertIsNone(module.handle(ctx))
        warning = module.handle(ctx)
        self.assertIn("systemMessage", warning)
        self.assertNotIn("continue", warning)
        self.assertIs(module.handle(ctx)["continue"], False)

    def transcript(self, usages):
        path = self.root / "transcript.jsonl"
        path.write_text("".join(json.dumps({"type": "assistant", "message": {
            "id": "m%d" % i, "usage": usage}}) + "\n" for i, usage in enumerate(usages)))
        return measure.measure_transcript(str(path))

    def test_missing_or_invalid_counters_are_unavailable(self):
        valid = dict.fromkeys(measure.USAGE_KEYS, 1)
        invalid = [{}, {"output_tokens": 1}]
        for value in (None, True, -1, 1.5, "1"):
            invalid.append(dict(valid, output_tokens=value))
        for usage in invalid:
            with self.subTest(usage=usage):
                result = self.transcript([valid, usage])
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("prompt_tokens", result)

    def test_explicit_zero_counters_are_measured(self):
        result = self.transcript([dict.fromkeys(measure.USAGE_KEYS, 0)])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["prompt_tokens"], 0)

    def test_repeated_message_is_counted_once(self):
        path = self.root / "transcript.jsonl"
        row = json.dumps({"type": "assistant", "message": {
            "id": "m", "usage": dict.fromkeys(measure.USAGE_KEYS, 2)}}) + "\n"
        path.write_text(row * 2)
        result = measure.measure_transcript(str(path))
        self.assertEqual(result["assistant_messages"], 1)
        self.assertEqual(result["prompt_tokens"], 6)

    def test_missing_and_invalid_identities_are_unavailable(self):
        path = self.root / "identities.jsonl"
        for identity in (None, "", "   ", [], ["bad"], {"bad": 1}, 42, True):
            with self.subTest(identity=identity):
                row = {"type": "assistant", "message": {
                    "id": identity, "usage": dict.fromkeys(measure.USAGE_KEYS, 1)}}
                path.write_text((json.dumps(row) + "\n") * 2)
                result = measure.measure_transcript(str(path))
                self.assertEqual(result["status"], "unavailable")
                self.assertNotIn("prompt_tokens", result)

    def test_fallback_identities_still_deduplicate(self):
        path = self.root / "fallback-identities.jsonl"
        for field in ("requestId", "uuid"):
            with self.subTest(field=field):
                row = {"type": "assistant", field: "fallback", "message": {
                    "usage": dict.fromkeys(measure.USAGE_KEYS, 1)}}
                path.write_text((json.dumps(row) + "\n") * 2)
                result = measure.measure_transcript(str(path))
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["assistant_messages"], 1)

    def saved_session(self, status="ok"):
        store = session.Store.for_payload({"session_id": "saved", "scratchpad_dir": str(self.root)})
        path = self.root / "saved-transcript.jsonl"
        row = {"type": "assistant", "message": {
            "id": "m1", "usage": dict.fromkeys(measure.USAGE_KEYS, 100)}}
        path.write_text(json.dumps(row) + "\n")
        session.write_json(store.session_path, {"session_id": "saved", "transcript_path": str(path)})
        cached = dict.fromkeys(measure.USAGE_KEYS, 0)
        cached.update(status=status, source="transcript", prompt_tokens=0, assistant_messages=1)
        session.write_json(store.summary_path, {"measured_from_transcript": cached})
        return store, path

    def test_default_savings_refreshes_successful_stored_summary(self):
        savings = load("scripts/savings.py")
        store, path = self.saved_session()
        self.assertEqual(savings.load_session(store)["measured"]["prompt_tokens"], 300)
        # Later usage must be included even after a previously successful scan.
        with path.open("a") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {
                "id": "m2", "usage": dict.fromkeys(measure.USAGE_KEYS, 100)}}) + "\n")
        self.assertEqual(savings.load_session(store)["measured"]["prompt_tokens"], 600)

    def test_no_measure_preserves_cached_measurement_without_scanning(self):
        savings = load("scripts/savings.py")
        store, _ = self.saved_session()
        with patch.object(measure, "measure_transcript", side_effect=AssertionError("must not scan")):
            self.assertEqual(savings.load_session(store, measure_live=False)["measured"]["prompt_tokens"], 0)

    def test_live_savings_does_not_hide_an_unreadable_transcript(self):
        savings = load("scripts/savings.py")
        store, path = self.saved_session()
        path.unlink()
        self.assertEqual(savings.load_session(store)["measured"]["status"], "unavailable")

    def test_aggregate_preserves_incomplete_coverage_in_json_and_text(self):
        savings = load("scripts/savings.py")
        for statuses, expected in ((["ok"], "ok"), (["partial"], "partial"),
                                   (["ok", "partial"], "partial"),
                                   (["ok", "unavailable"], "partial"),
                                   (["unavailable"], "unavailable")):
            with self.subTest(statuses=statuses):
                stores = []
                for i, status in enumerate(statuses):
                    store = session.Store.for_payload({"scratchpad_dir": str(self.root / str(i))})
                    cached = dict.fromkeys(measure.USAGE_KEYS, 1)
                    cached.update(status=status, prompt_tokens=3, assistant_messages=1)
                    session.write_json(store.summary_path, {"measured_from_transcript": cached})
                    stores.append(store)
                with patch.object(session.Store, "all", return_value=stores):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(savings.main(["--all", "--no-measure", "--json"]), 0)
                    result = json.loads(output.getvalue())["measured"]
                    self.assertEqual(result["status"], expected)
                    if expected != "unavailable":
                        self.assertEqual(result["prompt_tokens"], 3 * sum(s != "unavailable" for s in statuses))
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        savings.main(["--all", "--no-measure"])
                    if expected == "partial":
                        self.assertIn("partial measurement", output.getvalue())
                    elif expected == "unavailable":
                        self.assertIn("unavailable", output.getvalue())

    def test_long_lines_cannot_bypass_scan_cap(self):
        module = load("hooks/read-guard.py")
        stream = io.BytesIO(b"x" * 4096)
        stream.close = lambda: None
        with patch.object(module, "MAX_COUNT_BYTES", 1024), patch("builtins.open", return_value=stream):
            self.assertEqual(module.count_lines("fixture"), (None, 0, False))
        self.assertEqual(stream.tell(), 1024)

    def test_expired_deadline_does_not_scan(self):
        module = load("hooks/read-guard.py")
        stream = io.BytesIO(b"line\n" * 400)
        stream.close = lambda: None
        with patch("builtins.open", return_value=stream):
            self.assertEqual(module.count_lines("fixture", time.monotonic() - 1), (None, 0, False))
        self.assertEqual(stream.tell(), 0)

    def test_normal_scan_retains_first_page_and_exact_line_count(self):
        module = load("hooks/read-guard.py")
        path = self.root / "lines.txt"
        path.write_bytes(b"line\n" * 400)
        self.assertEqual(module.count_lines(str(path)), (1500, 400, True))

    def test_long_output_line_preserves_error_at_end(self):
        text = "x" * 13000 + " ERROR: database migration failed"
        output, info = trimmer.trim_text(text, 12288)
        self.assertIsNone(info)
        self.assertEqual(output, text)

    def test_long_line_with_early_error_keeps_late_diagnostic_too(self):
        text = "error count: 1 " + "x" * 13000 + " fatal: database migration failed"
        output, _ = trimmer.trim_text(text, 12288)
        self.assertIn("error count: 1", output)
        self.assertIn("fatal: database migration failed", output)

    def test_single_line_read_is_bounded_and_marked_partial(self):
        for text in ("x" * 1000000, "\N{SNOWMAN}" * 10000, "x" * 30000 + "\nlater line"):
            with self.subTest(size=trimmer.nbytes(text)):
                output, info = trimmer.trim_head(text, 24576, start_line=7)
                self.assertLessEqual(trimmer.nbytes(output), 24576)
                self.assertTrue(info["partial_line"])
                self.assertEqual(info["lines_kept"], 1)
                self.assertEqual(info["first_elided_line"], 7)

    def test_long_read_reply_describes_partial_line_and_never_dedupes_it(self):
        module = load("hooks/dedupe-reads.py")
        payload = {"session_id": "long-read", "tool_name": "Read", "scratchpad_dir": str(self.root),
                   "tool_input": {"file_path": "large.txt"}, "tool_response": {
                       "type": "text", "file": {"filePath": "large.txt", "content": "x" * 1000000,
                                                 "numLines": 1, "startLine": 1, "totalLines": 1}}}
        for _ in range(2):
            ctx = context(payload)
            reply = module.handle(ctx)["hookSpecificOutput"]
            file = reply["updatedToolOutput"]["file"]
            self.assertLessEqual(trimmer.nbytes(file["content"]), 24576)
            self.assertEqual(file["numLines"], 1)
            self.assertTrue(file["truncatedByTokenCap"])
            self.assertIn("rest of that line", reply["additionalContext"])
            self.assertNotIn("unchanged", reply["additionalContext"])

    def test_fallback_store_survives_late_scratchpad_and_lookup(self):
        sid = "late-scratchpad"
        env = {"CLAUDE_CODE_TMPDIR": str(self.root)}
        scratch = self.root / ("claude-%d" % session._uid()) / "project" / sid / "scratchpad"
        with patch.object(tempfile, "gettempdir", return_value=str(self.root)), \
             patch.object(session, "_temp_bases", return_value=[str(self.root)]):
            first = session.Store.for_payload({"session_id": sid})
            first.remember({"session_id": sid, "transcript_path": "transcript.jsonl"})
            first.save_reads({"earlier": {"sha256": "digest"}})
            ledger.append(first, ledger.stamp(ledger.trimmed("Bash", 1000, 100), sid))
            scratch.mkdir(parents=True)
            later = session.Store.for_payload({"session_id": sid, "scratchpad_dir": str(scratch)})
            self.assertEqual(first, later)
            self.assertEqual(session.Store.find(sid, env), first)
            self.assertEqual(later.reads(), first.reads())
            self.assertEqual(len(ledger.load(later)), 1)
            self.assertEqual(session.Store.all(env), [first])
            later.reset()
            self.assertEqual(first.reads(), {})
            self.assertTrue(first.has_ledger())

    def test_scratchpad_store_survives_missing_scratchpad_field(self):
        sid = "early-scratchpad"
        scratch = self.root / ("claude-%d" % session._uid()) / "project" / sid / "scratchpad"
        env = {"CLAUDE_CODE_TMPDIR": str(self.root)}
        with patch.object(tempfile, "gettempdir", return_value=str(self.root)), \
             patch.dict(os.environ, env):
            first = session.Store.for_payload({"session_id": sid, "scratchpad_dir": str(scratch)})
            first.save_reads({"earlier": {"sha256": "digest"}})
            later = session.Store.for_payload({"session_id": sid})
            self.assertEqual(later, first)
            self.assertEqual(later.reads(), first.reads())

    @unittest.skipUnless(os.name == "posix", "requires Unix symlinks")
    def test_private_writer_cannot_follow_old_staging_symlink(self):
        victim = self.root / "victim"
        victim.write_text("untouched")
        output = self.root / "output"
        old_staging = Path("%s.%d.tmp" % (output, os.getpid()))
        old_staging.symlink_to(victim)
        session.write_private(str(output), "replacement")
        self.assertEqual(victim.read_text(), "untouched")
        self.assertFalse(output.is_symlink())
        self.assertEqual(output.read_text(), "replacement")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "requires Unix symlinks")
    def test_private_directory_refuses_symlink(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            session.private_dir(str(link))

    @unittest.skipUnless(os.name == "posix", "requires Unix ownership")
    def test_private_directory_refuses_foreign_owner(self):
        target = self.root / "target"
        target.mkdir()
        info = target.stat()
        foreign = type("ForeignStat", (), {"st_mode": info.st_mode, "st_uid": os.getuid() + 1})()
        with patch.object(session.os, "lstat", return_value=foreign), self.assertRaises(PermissionError):
            session.private_dir(str(target))

    # From audit_safety_regressions.py, whose other tests were copies of the ones above (#193).
    def nudge_context(self, sid):
        payload = {"session_id": sid, "scratchpad_dir": str(self.root / sid),
                   "prompt_cache_likely_expired": True, "context_tokens": 40000,
                   "estimated_cache_write_usd": 1}
        return context(payload, {"XDG_STATE_HOME": str(self.root / "state")}, "SessionStart")

    def test_other_session_clear_cannot_consume_nudge(self):
        module = load("hooks/trim-session.py")
        first, other = self.nudge_context("first"), self.nudge_context("other")
        module.cache_nudge(first, 100)
        module.nudge_accepted(other, 101)
        self.assertEqual(ledger.load(other.store()), [])
        module.nudge_accepted(first, 102)
        self.assertEqual(ledger.load(first.store())[-1]["kind"], "cache_nudge_accepted")

    def test_concurrent_sessions_keep_independent_nudges(self):
        module = load("hooks/trim-session.py")
        contexts = [self.nudge_context(sid) for sid in ("first", "other")]
        for ctx in contexts:
            module.cache_nudge(ctx, 100)
        for ctx in contexts:
            module.nudge_accepted(ctx, 101)
            module.nudge_accepted(ctx, 102)
            accepted = [r for r in ledger.load(ctx.store()) if r["kind"] == "cache_nudge_accepted"]
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0]["nudged_session_id"], ctx.payload["session_id"])


if __name__ == "__main__":
    unittest.main()
