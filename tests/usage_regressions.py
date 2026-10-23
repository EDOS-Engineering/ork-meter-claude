"""Usage replay and shadow capture invariants, with no model requests."""
import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/lib"))
import features
import hook
import measure
import session
from usage import CLAUDE_KEYS, UsageLedger


def claude(mid="m", output=2):
    return {"type": "assistant", "message": {"id": mid, "content": "do not expose me",
            "usage": dict(zip(CLAUDE_KEYS, (10, 20, 30, output)))}}


def codex(thread="t", amount=1, write=None):
    usage = {"inputTokens": 10 * amount, "cachedInputTokens": 3 * amount,
             "outputTokens": 5 * amount, "reasoningOutputTokens": 2 * amount,
             "totalTokens": 15 * amount}
    if write is not None:
        usage["cacheWriteInputTokens"] = write
    return {"method": "thread/tokenUsage/updated", "params": {
        "threadId": thread, "turnId": "turn-%s" % amount, "tokenUsage": {
            "total": usage, "last": dict(usage)}}}


class UsageTests(unittest.TestCase):
    def test_claude_streaming_and_delayed_duplicate(self):
        ledger = UsageLedger()
        for output in (0, 7, 7, 0, 9):
            ledger.add("claude", claude(output=output), "attempt")
        ledger.add("claude", claude("m2", 3), "attempt")
        report = ledger.report()
        self.assertEqual(report["totals"]["output_tokens"], 12)
        self.assertEqual(report["totals"]["prompt_tokens"], 120)
        self.assertEqual(len(report["records"]), 2)
        self.assertNotIn("do not expose me", json.dumps(report))

    def test_codex_total_is_not_sum_of_snapshots_or_last(self):
        ledger = UsageLedger()
        for amount in (1, 3, 2, 3):
            ledger.add("codex", codex(amount=amount), "attempt")
        report = ledger.report()
        self.assertEqual(report["totals"]["total_tokens"], 45)
        self.assertEqual(report["totals"]["output_tokens"], 15)
        self.assertEqual(report["totals"]["reasoning_output_tokens"], 6)
        self.assertIsNone(report["totals"]["cache_write_tokens"])
        self.assertIsNone(report["totals"]["uncached_input_tokens"])

    def test_capture_files_merge_while_thread_boundaries_are_distinct(self):
        ledger = UsageLedger()
        # A separate retry has a distinct thread/counter identity. Another
        # file capturing the same cumulative thread is not additional spend.
        for stream, thread, attribution in (("a", "t", "main"), ("b", "t", "main"), ("c", "retry", "main"),
                                             ("a", "child", "subagent")):
            ledger.add("codex", codex(thread), stream, attribution)
        self.assertEqual(ledger.report()["totals"]["total_tokens"], 45)
        self.assertEqual(len(ledger.report()["records"]), 3)
        thread = next(r for r in ledger.report()["records"] if r["identity"] == "t")
        self.assertEqual(thread["capture_streams"], ["a", "b"])
        with self.assertRaises(ValueError):
            ledger.add("codex", codex(), "a", "auxiliary")

    def test_resumed_or_rotated_capture_does_not_add_the_same_thread_twice(self):
        ledger = UsageLedger()
        ledger.add("codex", codex(amount=1), "before-resume.jsonl")
        ledger.add("codex", codex(amount=2), "after-resume.jsonl")
        self.assertEqual(ledger.report()["totals"]["total_tokens"], 30)
        self.assertEqual(len(ledger.report()["records"]), 1)

    def test_interval_counts_only_work_after_known_baseline(self):
        ledger = UsageLedger("interval")
        ledger.begin_interval(codex(amount=1), "start")
        ledger.add("codex", codex(amount=2), "end")
        report = ledger.report()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["totals"]["total_tokens"], 15)
        self.assertEqual(report["records"][0]["raw_usage"]["totalTokens"], 30)
        self.assertEqual(report["records"][0]["interval_baseline"]["totalTokens"], 15)

    def test_missing_interval_baseline_does_not_count_lifetime_work_as_attempt_spend(self):
        ledger = UsageLedger("interval")
        ledger.add("codex", codex(amount=2), "end")
        self.assertEqual(ledger.report()["status"], "partial")
        self.assertIsNone(ledger.report()["totals"]["total_tokens"])

    def test_counter_reset_requires_explicit_epoch_provenance(self):
        ledger = UsageLedger()
        ledger.add("codex", codex(amount=2), "old")
        with self.assertRaises(ValueError):
            ledger.add("codex", codex(), "new", counter_epoch="reset")
        ledger.add("codex", codex(), "new", counter_epoch="reset", epoch_evidence="provider reset event in capture manifest")
        self.assertEqual(ledger.report()["totals"]["total_tokens"], 45)
        self.assertEqual(len(ledger.report()["records"]), 2)

    def test_counter_regression_without_provenance_remains_a_gap(self):
        ledger = UsageLedger()
        ledger.add("codex", codex(amount=2), "first")
        ledger.add("codex", codex(), "second")
        report = ledger.report()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["totals"]["total_tokens"], 30)
        self.assertIn("reset", report["gaps"][0]["reason"])

    def test_duplicate_claude_message_across_files_is_one_provider_request(self):
        ledger = UsageLedger()
        ledger.add("claude", claude(), "first")
        ledger.add("claude", claude(), "second")
        self.assertEqual(len(ledger.report()["records"]), 1)
        self.assertEqual(ledger.report()["totals"]["total_tokens"], 62)

    def test_rotated_files_and_interval_baseline_through_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("before.jsonl", "after.jsonl")]
            for amount, path in enumerate(paths, 1):
                path.write_text(json.dumps(codex(amount=amount)) + "\n")
            command = [sys.executable, "-I", "-B", str(ROOT / "scripts/usage.py"), "--harness", "codex"]
            result = subprocess.run(command + list(map(str, paths)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["totals"]["total_tokens"], 30)
            result = subprocess.run(command + ["--scope", "interval", "--baseline", str(paths[0]), str(paths[1])],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["totals"]["total_tokens"], 15)

    def test_optional_write_unknown_and_explicit_zero_differ(self):
        ledger = UsageLedger()
        ledger.add("codex", codex(write=0), "a")
        self.assertEqual(ledger.report()["totals"]["cache_write_tokens"], 0)
        ledger.add("codex", codex(amount=2), "a")
        self.assertIsNone(ledger.report()["totals"]["cache_write_tokens"])

    def test_unknown_provider_usage_fields_preserved(self):
        ledger = UsageLedger()
        event = claude()
        event["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 30}
        ledger.add("claude", event, "a")
        event["message"]["usage"]["cache_creation"].clear()
        self.assertEqual(ledger.report()["records"][0]["provider_usage"]["cache_creation"],
                         {"ephemeral_5m_input_tokens": 30})

    def test_conflicting_snapshot_is_rejected_without_inventing_counts(self):
        ledger = UsageLedger()
        ledger.add("codex", codex(), "a")
        event = codex(amount=2)
        event["params"]["tokenUsage"]["total"]["cachedInputTokens"] = 1
        with self.assertRaisesRegex(ValueError, "conflicting"):
            ledger.add("codex", event, "a")
        self.assertEqual(ledger.report()["totals"]["total_tokens"], 15)

    def test_invalid_counters_never_become_zero(self):
        for value in (True, -1, "3", 1.2, None):
            event = codex()
            event["params"]["tokenUsage"]["total"]["outputTokens"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                UsageLedger().add("codex", event, "a")

    def test_inconsistent_codex_breakdown_is_rejected(self):
        for key, value in (("cachedInputTokens", 11), ("reasoningOutputTokens", 6), ("totalTokens", 99)):
            event = codex()
            event["params"]["tokenUsage"]["total"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                UsageLedger().add("codex", event, "a")

    def test_cli_exposes_bad_and_missing_streams_and_deduplicates_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            path.write_text(json.dumps(codex()) + "\nnot-json\n" + json.dumps(codex(amount=2)) + "\n")
            command = [sys.executable, "-I", "-B", str(ROOT / "scripts/usage.py"), "--harness", "codex"]
            result = subprocess.run(command + [str(path), str(path), str(path.parent / "missing")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "partial")
            self.assertEqual(len(report["gaps"]), 2)
            self.assertEqual(report["totals"]["total_tokens"], 30)
            path.write_text(json.dumps(codex()) + "\n")
            result = subprocess.run(command + [str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_transcript_updates_use_final_counts_and_mark_incomplete_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript.jsonl"
            path.write_text("".join(json.dumps(claude(output=n)) + "\n" for n in (0, 10, 3, 10)))
            result = measure.measure_transcript(str(path))
            self.assertEqual(result["output_tokens"], 10)
            self.assertEqual(result["assistant_messages"], 1)
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "assistant", "message": {"id": "missing"}}) + "\n")
            self.assertEqual(measure.measure_transcript(str(path))["status"], "partial")


class ShadowTests(unittest.TestCase):
    def test_session_metadata_is_published_once_without_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = {"session_id": "initialization", "scratchpad_dir": directory, "transcript_path": "transcript"}
            store = session.Store.for_payload(payload)
            with patch.object(os, "replace", side_effect=AssertionError("initialization must not replace metadata")):
                store.remember(payload)
                store.remember(payload)
            self.assertEqual(store.session_info(), {"session_id": "initialization", "transcript_path": "transcript"})
            self.assertFalse(list(Path(store.path).glob("*.tmp")))

    def test_concurrent_initializer_validates_winner_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "metadata.json")
            self.assertTrue(session.initialize_json(path, {"session_id": "a"}))
            self.assertFalse(session.initialize_json(path, {"session_id": "a"}))
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                session.initialize_json(path, {"session_id": "b"})
            self.assertEqual(json.loads(Path(path).read_text()), {"session_id": "a"})
            self.assertEqual(list(Path(directory).iterdir()), [Path(path)])

    def test_shadow_hooks_capture_exact_received_data_without_running_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
                payload = {"hook_event_name": event, "session_id": "shadow", "tool_use_id": event,
                           "tool_name": "Bash", "tool_input": {"command": "printf untouched"},
                           "tool_response": {"stdout": "a\r\nb\x1b[0m", "stderr": "error!", "exitCode": 1},
                           "scratchpad_dir": directory, "is_interrupt": True}
                def handler(_):
                    self.fail("shadow must not execute an optimization handler")
                output = io.StringIO()
                with patch.dict(os.environ, {"ORK_METER_MODE": "shadow"}), \
                     patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                     contextlib.redirect_stdout(output):
                    hook.run((event,), handler, feature="trim")
                self.assertEqual(output.getvalue(), "")
            store = session.Store.for_scratchpad(directory)
            traces = list(Path(store.trace_path("probe")).parent.glob("*.json"))
            self.assertEqual(len(traces), 3)
            for path in traces:
                document = json.loads(path.read_text())
                self.assertEqual(document["tool_response"], payload["tool_response"])
                self.assertIs(document["is_interrupt"], True)
                self.assertIn("unverified", document["provenance"])
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(store.reads_path).exists())
            self.assertFalse(Path(store.ledger_path).exists())

    def test_off_and_invalid_modes_do_not_capture_or_transform(self):
        for mode in ("off", "invalid"):
            payload = {"hook_event_name": "PostToolUse"}
            output = io.StringIO()
            with patch.dict(os.environ, {"ORK_METER_MODE": mode}), \
                 patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 contextlib.redirect_stdout(output):
                hook.run(("PostToolUse",), lambda _: self.fail("pass-through must not run handler"))
            self.assertEqual(output.getvalue(), "")
            self.assertFalse(features.enabled({"ORK_METER_MODE": mode}, "trim"))

    def test_shadow_feature_flags_do_not_claim_reduction(self):
        enabled = features.enabled_features({"ORK_METER_MODE": "shadow"})
        self.assertFalse(set(enabled) & set(features.AUTOMATIC_FEATURES))
        self.assertIn("shadow capture", features.status_line({"ORK_METER_MODE": "shadow"}))

    def test_shadow_storage_failure_still_passes_through(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"ORK_METER_MODE": "shadow"}), \
             patch.object(sys, "stdin", io.StringIO('{"hook_event_name":"PostToolUse"}')), \
             patch.object(session.Store, "for_payload", side_effect=OSError("disk full")), \
             contextlib.redirect_stdout(output):
            hook.run(("PostToolUse",), lambda _: self.fail("must not rerun"))
        self.assertEqual(output.getvalue(), "")

    def test_parallel_observations_keep_call_identity_and_status(self):
        import time
        import trace
        with tempfile.TemporaryDirectory() as directory:
            def capture(i):
                payload = {"scratchpad_dir": directory, "session_id": "parallel", "tool_use_id": "call-%d" % i,
                           "tool_name": "Bash", "tool_response": {"stdout": str(i), "exitCode": i % 3}}
                trace.capture(hook.Context({}, payload, "PostToolUse", time.monotonic() + 10))
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(capture, range(30)))
            store = session.Store.for_scratchpad(directory)
            files = list(Path(store.trace_path("probe")).parent.glob("*.json"))
            self.assertEqual(len(files), 30)
            for path in files:
                document = json.loads(path.read_text())
                i = int(document["tool_use_id"].split("-")[1])
                self.assertEqual(document["tool_response"], {"stdout": str(i), "exitCode": i % 3})


class WindowsPublicationTests(unittest.TestCase):
    """initialize_json under Windows sharing (#140): sharing applies per file,
    not per name, so the winner's unlink of its temporary name and a loser's
    read of the published name can each fail while the other holds the file.
    Simulated: os.name is "nt" (fs_path left as is) and os.unlink/read_json
    raise PermissionError/return None a few times, or for good."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = str(self.directory / "session.json")

    @contextlib.contextmanager
    def windows(self, bound=10.0):
        # A generous retry bound by default: the flaky fakes stop failing after
        # a fixed number of calls, so a slow runner must not run out of time
        # first. Tests that expect the bound to expire pass a short one.
        with patch.object(session.os, "name", "nt"), patch.object(session, "fs_path", lambda path: path), \
                patch.object(session, "REPLACE_RETRY_SECONDS", bound):
            yield

    def flaky_unlink(self, failures):
        real = os.unlink
        calls = []
        def unlink(path, *args, **kwargs):
            if os.path.basename(path).startswith(".ork-meter-"):
                calls.append(path)
                if failures is None or len(calls) <= failures:
                    raise PermissionError(32, "The process cannot access the file", path)
            return real(path, *args, **kwargs)
        return unlink, calls

    def flaky_read(self, failures):
        real = session.read_json
        calls = []
        def read_json(path, default=None):
            calls.append(path)
            if failures is None or len(calls) <= failures:
                return default
            return real(path, default)
        return read_json, calls

    def temporaries(self):
        return list(self.directory.glob(".ork-meter-*.tmp"))

    def test_winner_publication_survives_a_briefly_held_temporary(self):
        unlink, calls = self.flaky_unlink(3)
        with self.windows(), patch.object(session.os, "unlink", unlink):
            self.assertTrue(session.initialize_json(self.path, {"session_id": "a"}))
        self.assertEqual(len(calls), 4)
        self.assertEqual(json.loads(Path(self.path).read_text()), {"session_id": "a"})
        self.assertEqual(self.temporaries(), [])

    def test_winner_publication_survives_a_temporary_held_for_good(self):
        unlink, calls = self.flaky_unlink(None)
        with self.windows(bound=0.1), patch.object(session.os, "unlink", unlink):
            self.assertTrue(session.initialize_json(self.path, {"session_id": "a"}))
        self.assertGreater(len(calls), 1)
        self.assertEqual(json.loads(Path(self.path).read_text()), {"session_id": "a"})
        leftover = self.temporaries()
        self.assertEqual(len(leftover), 1)   # a complete private copy nothing reads
        self.assertEqual(leftover[0].read_text(), Path(self.path).read_text())
        with self.windows():
            self.assertFalse(session.initialize_json(self.path, {"session_id": "a"}))

    def test_loser_gets_the_winners_content_after_a_held_read(self):
        self.assertTrue(session.initialize_json(self.path, {"session_id": "a", "winner": 1}))
        read_json, calls = self.flaky_read(3)
        with self.windows(), patch.object(session, "read_json", read_json):
            self.assertFalse(session.initialize_json(self.path, {"session_id": "a", "winner": 2}))
        self.assertEqual(len(calls), 4)
        self.assertEqual(json.loads(Path(self.path).read_text()), {"session_id": "a", "winner": 1})
        self.assertEqual(self.temporaries(), [])

    def test_loser_still_rejects_another_identity_and_an_unreadable_winner(self):
        self.assertTrue(session.initialize_json(self.path, {"session_id": "a"}))
        with self.windows():
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                session.initialize_json(self.path, {"session_id": "b"})
        with self.windows(bound=0.1):
            read_json, calls = self.flaky_read(None)
            with patch.object(session, "read_json", read_json), self.assertRaisesRegex(ValueError, "identity mismatch"):
                session.initialize_json(self.path, {"session_id": "a"})
        self.assertGreater(len(calls), 1)
        self.assertEqual(self.temporaries(), [])

    def test_posix_path_is_unchanged(self):
        if os.name == "nt":
            self.skipTest("POSIX behaviour")
        unlink, calls = self.flaky_unlink(1)
        with patch.object(session.os, "unlink", unlink), self.assertRaises(PermissionError):
            session.initialize_json(self.path, {"session_id": "a"})
        self.assertEqual(len(calls), 1)   # no retry: the unlink error surfaces as before
        os.unlink(calls[0])
        read_json, reads = self.flaky_read(1)
        with patch.object(session, "read_json", read_json), self.assertRaisesRegex(ValueError, "identity mismatch"):
            session.initialize_json(self.path, {"session_id": "a"})
        self.assertEqual(len(reads), 1)   # no retry: one read, then the mismatch
        self.assertEqual(self.temporaries(), [])


if __name__ == "__main__":
    unittest.main()
