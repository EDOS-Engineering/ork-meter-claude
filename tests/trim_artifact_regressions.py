"""The full-output artifact is written only for a reply that is emitted (#130).

trim-tool-output.py names the artifact in its "Full output: <path>" note,
gates the reply, and only then writes the file; dedupe-reads.py does the same
for its Read snapshot (#190). Cases run the production entry point
(``hook.run``) unless they need to steer the clock mid-handler. The text is
stored once, in the ``.txt``, and every layout written before still loads.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import StoreCase, context, load, run_hook  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import artifacts  # noqa: E402
import delta  # noqa: E402
import hook  # noqa: E402
import ledger  # noqa: E402
import session  # noqa: E402
import token_gate  # noqa: E402

LARGE = "".join("record_%05d evidence line\n" % i for i in range(4000))
# Over a 1 KiB budget, but trimming it saves fewer tokens than the gate's reserve.
SMALL = "".join("item %03d value\n" % i for i in range(160))


class FullOutputAfterGate(StoreCase):
    def setUp(self):
        super().setUp()
        self.trim = load("hooks/trim-tool-output.py")
        token_gate._last_verdict = None

    def payload(self, stdout, command="make build"):
        return {"hook_event_name": "PostToolUse", "session_id": "artifact-session", "scratchpad_dir": str(self.scratch),
                "cwd": "/tmp/project", "tool_use_id": "call", "tool_name": "Bash", "tool_input": {"command": command},
                "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}}

    def run_hook(self, payload, env=None, budget=hook.DEFAULT_BUDGET, started=None):
        return run_hook(payload, self.trim.handle, self.trim.EVENTS, env=env, budget=budget, started=started)

    def files(self):
        directory = Path(self.store.path) / session.TOOL_OUTPUT_DIR
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    @staticmethod
    def cited(reply):
        stdout = reply["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        return Path(stdout.rsplit("Full output: ", 1)[1].rsplit(" (", 1)[0])

    def test_emitted_reply_cites_an_artifact_holding_the_full_output(self):
        payload = self.payload(LARGE)
        reply = self.run_hook(payload)
        self.assertIsNotNone(reply)
        path = self.cited(reply)
        self.assertEqual(path.read_text(), LARGE)
        self.assertEqual(artifacts.load(self.store, path.stem)["response"], payload["tool_response"])
        self.assertEqual(self.files(), [path.stem + ".json", path.name])
        self.assertEqual([row["kind"] for row in ledger.load(self.store)], ["trim"])

    def test_reply_refused_by_the_token_gate_leaves_no_artifact(self):
        reply = self.run_hook(self.payload(SMALL), env={"ORK_METER_TRIM_BUDGET_BASH": "1024"})
        self.assertIsNone(reply)
        self.assertEqual(self.files(), [])
        self.assertEqual(ledger.load(self.store), [])
        # The same output does trim; only the gate turned it down.
        with patch.object(token_gate, "permits", return_value=True):
            self.assertIsNotNone(self.run_hook(self.payload(SMALL), env={"ORK_METER_TRIM_BUDGET_BASH": "1024"}))

    def test_reply_past_its_deadline_leaves_no_artifact_and_hashes_nothing(self):
        with patch.object(artifacts, "prepare", wraps=artifacts.prepare) as prepare:
            self.assertIsNone(self.run_hook(self.payload(LARGE), budget=0.0, started=time.monotonic() - 60))
        prepare.assert_not_called()
        self.assertEqual(self.files(), [])
        self.assertEqual(ledger.load(self.store), [])

    def test_deadline_passing_during_the_write_removes_the_artifact(self):
        ctx = context(self.payload(LARGE))
        write = artifacts.Pending.write

        def slow(pending):
            path = write(pending)
            self.assertTrue(os.path.exists(path))
            ctx.deadline = time.monotonic() - 1
            return path
        with patch.object(artifacts.Pending, "write", slow):
            self.assertIsNone(self.trim.handle(ctx))
        self.assertEqual(self.files(), [])

    def test_reply_past_the_budget_but_within_the_emit_limit_keeps_its_artifact(self):
        # hook.run prints a finished reply until EMIT_LIMIT after the process
        # started (#140); the handler must not drop it or skip its file sooner.
        ctx = hook.Context({}, self.payload(LARGE), "PostToolUse", time.monotonic() - 0.1)
        ctx.started = time.monotonic() - 1.6
        self.assertTrue(hook.may_emit(ctx))
        reply = self.trim.handle(ctx)
        self.assertIsNotNone(reply)
        self.assertEqual(self.cited(reply).read_text(), LARGE)

    def test_late_bash_reply_drops_the_delta_base(self):
        name = delta.key("Bash", None, "/tmp/project", "make build")
        delta.save(self.store, name, "earlier\n", inline=True)
        # Past the budget and past EMIT_LIMIT since the process started.
        self.assertIsNone(self.run_hook(self.payload(LARGE), env={"ORK_METER_DELTA": "on"}, budget=0.0,
                                        started=time.monotonic() - 60))
        self.assertIsNone(delta.load(self.store, name)[0])

    def test_write_failure_passes_the_output_through_and_leaves_nothing(self):
        real = session.write_private

        def failing(path, text):
            if path.endswith(".json"):
                raise OSError("disk full")
            return real(path, text)
        with patch.object(session, "write_private", failing):
            self.assertIsNone(self.run_hook(self.payload(LARGE)))
        # The text file was written first; it is removed with the failure.
        self.assertEqual(self.files(), [])
        self.assertEqual(ledger.load(self.store), [])

    def test_failed_rewrite_keeps_an_identical_artifact_an_earlier_reply_cites(self):
        path = self.cited(self.run_hook(self.payload(LARGE)))
        before = self.files()
        with patch.object(session, "write_private", side_effect=OSError("disk full")):
            self.assertIsNone(self.run_hook(self.payload(LARGE)))
        self.assertEqual(self.files(), before)
        self.assertEqual(path.read_text(), LARGE)

    def test_naming_an_artifact_touches_no_disk(self):
        pending = artifacts.prepare(self.store, "Bash", {"stdout": LARGE}, LARGE)
        self.assertFalse((Path(self.store.path) / session.TOOL_OUTPUT_DIR).exists())
        self.assertEqual(Path(pending.write()).read_text(), LARGE)

    def test_discard_keeps_a_file_a_concurrent_writer_replaced(self):
        # Another hook with the same output replaces the file between this
        # write and the discard: its reply cites it, so it must survive.
        pending = artifacts.prepare(self.store, "Bash", {"stdout": LARGE}, LARGE)
        path = pending.write()
        session.write_private(path, LARGE)
        pending.discard()
        self.assertEqual(Path(path).read_text(), LARGE)

    def test_handler_and_hook_run_share_one_gate_verdict(self):
        with patch.object(token_gate, "permits", wraps=token_gate.permits) as permits:
            self.assertIsNotNone(self.run_hook(self.payload(LARGE)))
        self.assertEqual(permits.call_count, 1)

    def test_gate_verdict_is_not_reused_for_a_different_reply(self):
        payload = self.payload(LARGE)
        reply = hook.updated_tool_output("PostToolUse", dict(payload["tool_response"], stdout="short"))
        with patch.object(token_gate, "permits", return_value=True) as permits:
            token_gate.permits_reply(payload, reply)
            token_gate.permits_reply(payload, reply)
            self.assertEqual(permits.call_count, 1)
            reply["hookSpecificOutput"]["updatedToolOutput"]["stdout"] = "changed"
            token_gate.permits_reply(payload, reply)
            self.assertEqual(permits.call_count, 2)
            token_gate.permits_reply(json.loads(json.dumps(payload)), reply)
            self.assertEqual(permits.call_count, 3)


class PersistedOutputIsLeftAlone(StoreCase):
    """#100, #350: Claude Code saved the output under tool-results/ and the
    model gets only its <persisted-output> preview, whatever the hook replies.
    A trim would change nothing the model sees, so the hook neither replies,
    nor copies the output, nor records a saving measured against text the
    model never received. The payload is tests/fixtures/
    posttooluse.bash.persisted.json, shaped as Claude Code 2.1.308 sends it."""

    def setUp(self):
        super().setUp()
        self.trim = load("hooks/trim-tool-output.py")
        token_gate._last_verdict = None
        fixture = Path(__file__).resolve().parent / "fixtures/posttooluse.bash.persisted.json"
        self.persisted = json.loads(fixture.read_text(encoding="utf-8"))
        self.persisted["scratchpad_dir"] = str(self.scratch)

    def run_hook(self, payload, env=None):
        return run_hook(payload, self.trim.handle, self.trim.EVENTS, env=env)

    def inline(self, payload):
        """The same payload as if the host had delivered it inline."""
        payload = json.loads(json.dumps(payload))
        for key in ("persistedOutputPath", "persistedOutputSize"):
            del payload["tool_response"][key]
        return payload

    def files(self):
        directory = Path(self.store.path) / session.TOOL_OUTPUT_DIR
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def test_fixture_is_the_hosts_persisted_shape(self):
        response = self.persisted["tool_response"]
        self.assertEqual(len(response["stdout"]), 30000, "the host's inline cap, cut mid-line")
        self.assertGreater(response["persistedOutputSize"], len(response["stdout"].encode("utf-8")))
        self.assertEqual(Path(response["persistedOutputPath"]).parent.name, "tool-results")
        self.assertEqual(Path(response["persistedOutputPath"]).parent.parent.name, self.persisted["session_id"])

    def test_persisted_bash_output_gets_no_reply_no_copy_and_no_saving(self):
        with patch.object(self.trim.write_guard, "observe_output") as observe:
            self.assertIsNone(self.run_hook(self.persisted))
        observe.assert_not_called()   # the model saw only the host's preview
        self.assertEqual(self.files(), [])
        self.assertEqual(ledger.load(self.store), [])
        # Control: the same output delivered inline is trimmed and credited,
        # so persistedOutputPath alone is what holds the hook back.
        self.assertIsNotNone(self.run_hook(self.inline(self.persisted)))
        self.assertEqual([row["kind"] for row in ledger.load(self.store)], ["trim"])

    def test_persisted_test_runner_output_is_not_filtered_or_credited(self):
        payload = json.loads(json.dumps(self.persisted))
        payload["tool_input"]["command"] = "python3 -m pytest -v tests"
        payload["tool_response"]["stdout"] = ("".join("tests/test_x.py::test_%05d PASSED\n" % i for i in range(2000)))[:30000]
        self.assertIsNone(self.run_hook(payload))
        self.assertEqual(ledger.load(self.store), [])
        self.assertIsNotNone(self.run_hook(self.inline(payload)))
        self.assertIn("test_filter", [row["kind"] for row in ledger.load(self.store)])

    def test_persisted_output_drops_the_delta_base(self):
        name = delta.key("Bash", None, "/tmp/project", "git diff")
        delta.save(self.store, name, self.persisted["tool_response"]["stdout"][:10000], inline=True)
        self.assertIsNone(self.run_hook(self.persisted, env={"ORK_METER_DELTA": "on"}))
        self.assertIsNone(delta.load(self.store, name)[0], "the model never saw this output in full")
        self.assertEqual(ledger.load(self.store), [])

    def test_persisted_output_of_other_tools_is_left_alone(self):
        text = "".join("result line %05d with padding padding padding\n" % i for i in range(2000))
        for tool, tool_input, response in (
                ("WebFetch", {"url": "https://example.test/", "prompt": "summarize"}, {"result": text, "code": 200}),
                ("Grep", {"pattern": "x", "output_mode": "content"}, {"mode": "content", "content": text, "numLines": 2000}),
                ("Glob", {"pattern": "**/*"}, {"filenames": text.splitlines(), "numFiles": 2000})):
            with self.subTest(tool=tool):
                payload = dict(self.persisted, tool_name=tool, tool_input=tool_input,
                               tool_response=dict(response, persistedOutputPath=self.persisted["tool_response"]["persistedOutputPath"],
                                                  persistedOutputSize=len(text)))
                self.assertIsNone(self.run_hook(payload))
                self.assertEqual(ledger.load(self.store), [])
                self.assertEqual(self.files(), [])


READ = "".join("    value_%05d = compute(%d)  # padding padding padding\n" % (i, i) for i in range(4000))


class ReadSnapshotAfterGate(unittest.TestCase):
    """#190: dedupe-reads writes its snapshot only for an admitted reply."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name) / "scratch"
        self.store = session.Store.for_scratchpad(str(self.scratch))
        self.dedupe = load("hooks/dedupe-reads.py")
        token_gate._last_verdict = None

    def context(self, deadline=10):
        lines = READ.count("\n") + 1
        payload = {"hook_event_name": "PostToolUse", "session_id": "read-session", "scratchpad_dir": str(self.scratch),
                   "tool_use_id": "read", "tool_name": "Read", "tool_input": {"file_path": "/tmp/project/big.py"},
                   "tool_response": {"type": "text", "file": {"filePath": "/tmp/project/big.py", "content": READ,
                                                             "numLines": lines, "startLine": 1, "totalLines": lines}}}
        ctx = hook.Context({}, payload, "PostToolUse", time.monotonic() + deadline)
        ctx.started = time.monotonic()
        return ctx

    def files(self):
        directory = Path(self.store.path) / session.TOOL_OUTPUT_DIR
        return sorted(p.name for p in directory.iterdir()) if directory.is_dir() else []

    def test_admitted_trim_cites_a_snapshot_holding_the_file_once(self):
        ctx = self.context()
        reply = self.dedupe.handle(ctx)
        path = Path(reply["hookSpecificOutput"]["additionalContext"].rsplit("Full original Read snapshot: ", 1)[1])
        self.assertEqual(path.read_text(), READ)
        self.assertEqual(self.files(), [path.stem + ".json", path.name])
        self.assertNotIn("value_00001", path.with_suffix(".json").read_text())
        document = artifacts.load(self.store, path.stem)
        self.assertEqual(document["response"], ctx.payload["tool_response"])
        self.assertEqual(document["text"], READ)
        self.assertTrue(self.store.reads()["/tmp/project/big.py"]["partial"])

    def test_refused_trim_writes_nothing_and_is_indexed_as_delivered_whole(self):
        ctx = self.context()
        with patch.object(token_gate, "permits_reply", return_value=False):
            self.assertIsNone(self.dedupe.handle(ctx))
        self.assertEqual(self.files(), [])
        self.assertFalse(self.store.reads()["/tmp/project/big.py"]["partial"])
        self.assertEqual(ledger.load(self.store), [])

    def test_late_trim_writes_nothing(self):
        ctx = self.context(deadline=-1)
        ctx.started = time.monotonic() - 60
        self.assertIsNone(self.dedupe.handle(ctx))
        self.assertEqual(self.files(), [])

    def test_late_after_a_slow_index_update_writes_nothing_and_rolls_the_index_back(self):
        # The reads index can wait up to 0.5 s on its lock. Admission comes
        # after it, so a reply that turns late there writes no snapshot, and
        # the index does not say partial for a reply that was never sent.
        name = delta.key("Read", None, "/tmp/project/big.py")
        delta.save(self.store, name, "earlier base\n" * 400)
        ctx = self.context(deadline=0.2)
        update = session.Store.update_reads

        def slow(store, key, entry):
            time.sleep(0.3)
            return update(store, key, entry)
        with patch.object(hook, "EMIT_LIMIT", 0.25), patch.object(session.Store, "update_reads", slow):
            self.assertIsNone(self.dedupe.handle(ctx))
        self.assertEqual(self.files(), [])
        self.assertFalse(self.store.reads()["/tmp/project/big.py"]["partial"])
        self.assertEqual(ledger.load(self.store), [])
        # With the delta lever on, the file delivered whole becomes the base.
        ctx = self.context(deadline=0.2)
        ctx.env = {"ORK_METER_DELTA": "on"}
        with patch.object(hook, "EMIT_LIMIT", 0.25), patch.object(session.Store, "update_reads", slow):
            self.assertIsNone(self.dedupe.handle(ctx))
        self.assertEqual(self.files(), [])
        self.assertEqual(delta.load(self.store, name)[0], READ)

    def test_failed_write_passes_the_read_through_and_cites_nothing(self):
        real = session.write_private

        def failing(path, text):
            if session.TOOL_OUTPUT_DIR in Path(path).parts:
                raise OSError("disk full")
            return real(path, text)
        with patch.object(session, "write_private", failing):
            self.assertIsNone(self.dedupe.handle(self.context()))
        self.assertEqual(self.files(), [])
        self.assertFalse(self.store.reads()["/tmp/project/big.py"]["partial"])


class ArtifactLayouts(unittest.TestCase):
    """#190: one prepare/load for every snapshot; earlier layouts still load."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = session.Store.for_scratchpad(str(Path(temporary.name) / "scratch"))

    def legacy(self, document, text):
        raw = json.dumps(document, ensure_ascii=False, sort_keys=True)
        identity = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        session.write_private(self.store.tool_output_path(identity), text)
        session.write_private(self.store.tool_output_path(identity, ".json"), raw + "\n")
        return identity

    def test_text_is_stored_once_and_restored_where_it_was(self):
        cases = [("Bash", {"stdout": LARGE, "stderr": "", "isImage": False}, LARGE, ["stdout"]),
                 ("Bash", {"stdout": LARGE, "stderr": "warn"}, LARGE + "\n--- stderr ---\nwarn", None),
                 ("Read", {"type": "text", "file": {"filePath": "/x", "content": LARGE}}, LARGE, ["file", "content"]),
                 ("mcp__x__y", [{"type": "text", "text": LARGE}], LARGE, [0, "text"]),
                 ("Glob", {"filenames": ["a", "b"]}, "a\nb", None),
                 ("Bash", LARGE, LARGE, None)]
        for tool, response, text, at in cases:
            with self.subTest(tool=tool, at=at):
                original = json.loads(json.dumps(response))
                path = Path(artifacts.save(self.store, tool, response, text))
                self.assertEqual(response, original, "the caller's response is not changed")
                stored = json.loads(path.with_suffix(".json").read_text())
                self.assertEqual(stored["text_at"], at)
                self.assertNotIn("text", stored)
                if at is not None:
                    self.assertNotIn(LARGE, path.with_suffix(".json").read_text())
                document = artifacts.load(self.store, path.stem)
                self.assertEqual((document["response"], document["text"], document["tool"]), (original, text, tool))
                self.assertNotIn("text_stored_in", document)
                self.assertNotIn("text_at", document)

    def test_edited_text_or_document_fails_the_integrity_check(self):
        path = Path(artifacts.save(self.store, "Bash", {"stdout": LARGE}, LARGE))
        path.write_text(LARGE.replace("record_00001", "record_99999"))
        with self.assertRaisesRegex(ValueError, "integrity"):
            artifacts.load(self.store, path.stem)
        path = Path(artifacts.save(self.store, "Bash", {"stdout": "other\n" * 10}, "other\n" * 10))
        companion = path.with_suffix(".json")
        companion.write_text(companion.read_text().replace('"stdout"', '"stdin"'))
        with self.assertRaisesRegex(ValueError, "integrity"):
            artifacts.load(self.store, path.stem)

    def test_layouts_written_before_still_load(self):
        # Text in the JSON (the layout before #190), as artifacts.prepare wrote it.
        response = {"stdout": LARGE, "stderr": ""}
        identity = self.legacy({"schema_version": 1, "tool": "Bash",
                                "completeness": {"received_response": "complete", "before_hook": "unknown"},
                                "received_text_sha256": hashlib.sha256(LARGE.encode()).hexdigest(),
                                "response": response, "text": LARGE}, LARGE)
        document = artifacts.load(self.store, identity)
        self.assertEqual((document["response"], document["text"]), (response, LARGE))
        # A record handle's text in the .txt (#143), as record_handles.prepare wrote it.
        for response in ({"content": [{"type": "text", "text": "[1,2]", "annotations": {}}], "isError": False},
                         [{"type": "text", "text": "[1,2]"}]):
            stripped = json.loads(json.dumps(response))
            (stripped["content"] if isinstance(stripped, dict) else stripped)[0]["text"] = None
            identity = self.legacy({"schema_version": 1, "tool": "mcp__x__y",
                                    "completeness": {"received_response": "complete", "before_hook": "unknown"},
                                    "received_text_sha256": hashlib.sha256(b"[1,2]").hexdigest(),
                                    "response": stripped, "text_stored_in": "txt"}, "[1,2]")
            document = artifacts.load(self.store, identity)
            self.assertEqual((document["response"], document["text"]), (response, "[1,2]"))
            self.assertNotIn("text_stored_in", document)

    def test_malformed_layouts_are_refused(self):
        base = {"schema_version": 1, "tool": "Bash", "received_text_sha256": hashlib.sha256(b"t").hexdigest(),
                "text_stored_in": "txt"}
        for extra in ({"response": {"stdout": "t"}, "text_at": ["stdout"]},       # slot not empty
                      {"response": {"stdout": None}, "text_at": ["stderr"]},      # no such slot
                      {"response": {"stdout": None}, "text_at": []},
                      {"response": {"stdout": None}, "text_at": "stdout"},
                      {"response": [None], "text_at": [True]},
                      {"response": {"stdout": None}},                             # no text_at: record handle only
                      {"response": {"stdout": None}, "text_at": ["stdout"], "text_stored_in": "elsewhere"}):
            with self.subTest(extra=extra):
                identity = self.legacy(dict(base, **extra), "t")
                with self.assertRaisesRegex(ValueError, "layout"):
                    artifacts.load(self.store, identity)


class ReadGuardSingleRead(unittest.TestCase):
    """#190: read-guard reads a file of up to 2 MiB once; its text is what text mode read."""

    def test_decode_matches_text_mode(self):
        guard = load("hooks/read-guard.py")
        with tempfile.TemporaryDirectory() as directory:
            for raw in (b"a\r\nb\rc\nd\r", b"\xef\xbb\xbfx\r\n\r\r\n", "ü€😀\u2028\r\n".encode() * 50, b"", b"\r"):
                path = Path(directory) / "sample.txt"
                path.write_bytes(raw)
                with open(path, encoding="utf-8") as stream:
                    self.assertEqual(guard.decode(raw), stream.read())
            with self.assertRaises(UnicodeError):
                guard.decode(b"caf\xe9")


if __name__ == "__main__":
    unittest.main()
