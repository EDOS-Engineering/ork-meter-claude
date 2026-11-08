"""Read compaction through the installed Claude CLI, localhost synthetic inference only.

A whole-file Read of a Python module over the token threshold is let
through whole by the read guard and compacted on its result
(hooks/dedupe-reads.py, hooks/lib/read_compact.py). At the emission
boundary (hooks/lib/native_text.py, applied by hook.run) the compactor's
fixed notice reaches the model beside the result (hooks/lib/read_notice.py)
and the replaced result keeps the lines the tool returned plus the
"… lines A–B not shown; Read with offset=A" markers and their empty slots,
so the CLI numbers every shown line with its real line number. Then, in
the same session:

* an Edit of a shown line succeeds;
* an Edit of a line the model never received, with that line's exact text,
  succeeds too: the CLI matches old_string against the file on disk, and
  its read-before-edit state holds the whole file it read, not the result
  the hook replaced;
* an Edit whose old_string is not in the file (the marker text the model
  no longer sees) fails safely, "String to replace not found", and changes
  nothing;
* a ranged Read of an elided range returns its exact lines;
* a Write rebuilt from the received view is refused by the Write guard
  (hooks/write-guard.py) and leaves the file unchanged. The refusal's
  reason is dropped too (clean_reply keeps only permissionDecision), so the
  CLI reports a bare block ("Blocked by hook"; 2.1.280: "Hook PreToolUse:Write
  denied this tool"); which lines are still unread is in
  the write-guard record (hooks/lib/write_guard.py), the oracle these tests
  read. A plain whole-file re-read can't restore the view or clear the
  record, however the host answers it: with its own "file unchanged" stub
  (Claude Code before 2.1.317) or with the content again (2.1.317 removed
  the stub), which the hook compacts again into the same view, and the
  next Write rebuilt from it is still refused; after a ranged Read covering
  every elided range, the Write goes through.

Exactly one reducer acts on the Read: one ``read_compact`` ledger row, no
``trim`` or ``read_guard`` row, charged for the emitted text
(native_text.reconcile_records), and one dedupe-reads telemetry row
(docs/hook-telemetry.md) that says a reply was emitted. Compaction is
opt-in (#316): these cases set ``ORK_METER_READ_COMPACT=on``; by default the
read guard pages the same Read before it runs, as before. A text file that
compaction can't bring under the threshold is paged on its result; the
guard's note naming the compacted size is not emitted, the ledger row is.
"""
import json
import os
import re
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import host_retry  # noqa: E402
from native_host import (Fixture, Host, answer, assert_hooks_fail_open, count_tokens, hook_evidence,  # noqa: E402
                         latest_tool_results, result_text, stream_session_ids, tool_results)
sys.path.insert(0, str(ROOT / "hooks/lib"))
import session  # noqa: E402
import write_guard  # noqa: E402

SHOWN = "def distant_helper(value, scale=3):"
ELIDED = "    total_30_19 = value * 30 + 19"
MARKER_PREFIX = "… lines "
# What the CLI tells the model about a Write the hook denied without a reason (hook.denied's
# permissionDecisionReason is dropped by native_text.clean_reply): "Blocked by hook" (Claude Code
# 2.1.311, 2.1.312), "Hook PreToolUse:Write denied this tool" (2.1.280). Either is the bare block.
BLOCKED = ("Blocked by hook", "denied this tool")


def blocked(text):
    """``text`` is the CLI's bare block of a denied tool call, in any installed version's words."""
    return any(marker in text for marker in BLOCKED)


def module_text():
    """About 1,000 lines of Python: a short head, then many functions whose
    bodies are long enough to elide."""
    parts = ['"""Synthetic module for the Read compaction qualification."""\n', "import math\n", "\n"]
    parts += ["CONSTANT_%d = %d\n" % (i, i) for i in range(30)]
    parts.append("\n")
    for f in range(40):
        parts.append("def helper_%d(value):\n" % f)
        parts += ["    total_%d_%d = value * %d + %d\n" % (f, n, f, n) for n in range(22)]
        parts.append("    return total_%d_21\n\n" % f)
    parts.append(SHOWN + "\n")
    parts += ["    step_%d = math.sqrt(value + %d) * scale\n" % (n, n) for n in range(12)]
    parts.append("    return step_11\n")
    return "".join(parts)


def line_number(text, line):
    return text.split("\n").index(line) + 1


def ledger_rows(scratch):
    rows = []
    for path in Path(session.fs_path(scratch)).rglob(session.LEDGER_NAME):
        rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def telemetry_rows(scratch, hook=None, event=None):
    """The finished hook_telemetry rows (docs/hook-telemetry.md) of a host's
    scratchpad, as ``(decision, reason)`` with the row, optionally one hook's
    and one event's."""
    rows = []
    for path in Path(session.fs_path(scratch)).rglob("hook-invocations.jsonl"):
        rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("phase") == "finished"
            and (hook is None or row.get("hook") == hook) and (event is None or row.get("event") == event)]


def decisions(rows):
    return [(row["decision"], row["reason"]) for row in rows]


def guard_env(host):
    """Where hooks/lib/write_guard.py keeps its records for a host (features.state_dir)."""
    return {"XDG_STATE_HOME": str(host.base / "state"), "HOME": str(host.home)}


def guard_record(host, path):
    """The write-guard record of ``path``: None without one, the dict otherwise
    (a tombstone included). The refusal's reason is not emitted, so the
    record is what says which lines the view still has unread."""
    env = guard_env(host)
    name = write_guard.locate(env, str(path))
    return None if name is None else write_guard._read(write_guard._path(env, name, ".json"))


def unread_lines(host, path, session_id):
    """The 1-based lines of ``path`` the session's view holds unread, with their labels."""
    record = guard_record(host, path)
    if record is None or record.get("tombstone"):
        return {}
    view = write_guard.view_key({"session_id": session_id})
    return {index + 1: label for index, label in write_guard.unread_for(record, view).items()}


def received_only(test, rendered, original):
    """Every line the CLI rendered is the file's line at that number, an
    elision marker at its own first line, or one of the marker's empty
    slots; returns the texts, which is the content the hook emitted."""
    lines = original.split("\n")
    texts = []
    slots = 0
    for expected, line in enumerate(rendered, 1):
        number, text = line.split("\t", 1)
        test.assertEqual(int(number), expected, "the CLI numbers the emitted content from 1")
        found = re.match(r"^\u2026 lines (\d+)\u2013(\d+) not shown; Read with offset=(\d+)$", text)
        if found:
            test.assertEqual((int(found.group(1)), int(found.group(3))), (expected, expected), line)
            slots = int(found.group(2)) - expected
        elif slots and text == "":
            slots -= 1
        else:
            test.assertEqual(text, lines[expected - 1], "line %d at its own number" % expected)
            slots = 0
        texts.append(text)
    return texts


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "opt in to installed Claude + localhost fixture")
@host_retry.retry_once
class NativeReadCompaction(unittest.TestCase):
    def run_session(self, host, calls, env):
        Fixture.calls, Fixture.requests, Fixture.arrivals = calls, [], []
        host.warm_hooks(env)   # #140: a cold bytecode cache cancelled SessionStart's trim-session.py on Windows CI
        result = host.run("Read and edit the fixture module.", env=env, system="Use the requested local files.")
        host.assert_no_external_traffic()
        assert_hooks_fail_open(self, result.stdout)
        return result

    def assert_blocked(self, text):
        """A refused Write as the model sees it: the CLI's bare block, no reason, no plugin prose."""
        self.assertTrue(blocked(text), text[:200])
        self.assertNotIn("Ork Meter", text, "hook.denied's reason is dropped by clean_reply")

    def test_compacted_read_reaches_the_model_and_edits_behave(self):
        with Host(self, "ork-plugin-compact-", "Read,Edit", inference=Fixture) as host:
            # Run from the resolved work directory: a Windows runner's TEMP is an 8.3 short path
            # (RUNNER~1). From the short path, --restricted accepts the Read but the don't-ask mode
            # refuses the Edits (the CLI resolves the file for its edit check); edited by the long path
            # from the short cwd, --restricted refuses the Read (CI, Claude Code 2.1.280 and 2.1.305).
            host.work = Path(os.path.realpath(host.work))
            target = host.work / "module.py"
            original = module_text()
            target.write_text(original, encoding="utf-8")
            elided_number = line_number(original, ELIDED)
            total = original.count("\n") + 1
            calls = [("Read", {"file_path": str(target)}),
                     ("Edit", {"file_path": str(target), "old_string": SHOWN,
                               "new_string": "def distant_helper(value, scale=4):"}),
                     ("Edit", {"file_path": str(target), "old_string": ELIDED,
                               "new_string": "    total_30_19 = value * 30 + 190"}),
                     ("Edit", {"file_path": str(target), "old_string": MARKER_PREFIX + "%d" % elided_number,
                               "new_string": "anything"}),
                     ("Read", {"file_path": str(target), "offset": elided_number - 2, "limit": 5})]
            result = self.run_session(host, calls, host.env("reduce", ORK_METER_READ_COMPACT="on"))
            self.assertEqual(len(Fixture.requests), 6, hook_evidence(result.stdout, Fixture.arrivals))

            read = result_text(tool_results(Fixture.requests[1])["native_1"])
            delivered = json.dumps(Fixture.requests[1]["messages"][-2:])
            # The fixed "Ork Meter compacted this Read" notice reaches the model beside the result.
            self.assertIn("Ork Meter compacted this Read of", delivered, hook_evidence(result.stdout, Fixture.arrivals))
            rendered = read.split("\n")
            texts = received_only(self, rendered, original)
            # Every shown line is at its own number, SHOWN among them; the elided bodies are markers and slots.
            for number, line in enumerate(original.split("\n")[:40], 1):
                self.assertEqual(rendered[number - 1], "%d\t%s" % (number, line))
            self.assertIn("%d\t%s" % (line_number(original, SHOWN), SHOWN), rendered)
            self.assertIn("def helper_0(value):", texts)
            self.assertNotIn(ELIDED, read)
            self.assertTrue(any(text.startswith(MARKER_PREFIX) for text in texts))
            self.assertLess(len(read), len(original) // 2)

            outcomes = [result_text(tool_results(Fixture.requests[n])["native_%d" % n]) for n in (2, 3, 4, 5)]
            self.assertIn("has been updated successfully", outcomes[0], "an Edit of a shown line")
            self.assertIn("has been updated successfully", outcomes[1],
                          "an Edit of an elided line with its exact text: matched against the file on disk")
            self.assertIn("String to replace not found", outcomes[2], "an Edit of marker text fails safely")
            self.assertNotIn("has not been read", " ".join(outcomes), "read-before-edit state allows the edits")
            self.assertIn("%d\t    total_30_19 = value * 30 + 190" % elided_number, outcomes[3],
                          "a ranged Read returns the elided range's exact, current lines")
            edited = target.read_text(encoding="utf-8")
            self.assertEqual(edited, original.replace("scale=3", "scale=4").replace(ELIDED, "    total_30_19 = value * 30 + 190"))

            rows = ledger_rows(host.scratch)
            kinds = [row["kind"] for row in rows]
            self.assertEqual(kinds, ["read_compact"], "one reducer acts on the Read, once")
            self.assertGreater(rows[0]["lines_elided"], 0)
            # reconcile_records charges the row for the emitted text.
            self.assertIs(rows[0]["native_text_boundary_reconciled"], True)
            self.assertEqual(rows[0]["bytes_after"], len("\n".join(texts).encode("utf-8")))
            self.assertNotIn(str(target), json.dumps(rows))
            self.assertEqual(decisions(telemetry_rows(host.scratch, "dedupe-reads.py", "PostToolUse"))[:1],
                             [("applied", "reply-emitted")], "the compacted result was emitted")
            self.assertEqual(decisions(telemetry_rows(host.scratch, "read-guard.py", "PreToolUse"))[:1],
                             [("skipped", "handler-no-reply")],
                             "the guard let the compactable Read through whole")

    def test_a_write_rebuilt_from_the_compacted_view_is_refused_until_the_ranges_are_read(self):
        with Host(self, "ork-plugin-compact-write-", "Read,Write", inference=Fixture) as host:
            host.work = Path(os.path.realpath(host.work))   # see the Edit test: one path form for the CLI
            target = host.work / "module.py"
            original = module_text()
            target.write_text(original, encoding="utf-8", newline="")
            total = original.count("\n") + 1

            def rebuilt(body):
                # What a model rebuilding the file from the received view would write: every line it was
                # given (no marker reaches it any more), so the elided code is gone.
                return rebuilt_from(result_text(tool_results(body)["native_1"]), target)
            after_refusal, after_reread, on_disk = {}, {}, []

            def unread_now():
                [view] = guard_record(host, target)["views"].values()
                return {index + 1: label for first, last, label in view for index in range(first, last + 1)}

            def reread(body):
                # Request 3 arrives after the Write was refused and before anything is read again: the
                # record's one view at that moment is what the refusal was about.
                after_refusal.update(unread_now())
                return {"file_path": str(target)}

            def rebuilt_after_reread(body):
                # Request 4 arrives after the plain re-read: what it left of the record, and of the file.
                after_reread.update(unread_now())
                on_disk.append(target.read_text(encoding="utf-8"))
                return rebuilt(body)
            calls = [("Read", {"file_path": str(target)}),
                     ("Write", rebuilt),
                     ("Read", reread),
                     ("Write", rebuilt_after_reread),
                     ("Read", {"file_path": str(target), "offset": 1, "limit": total}),
                     ("Write", rebuilt)]
            result = self.run_session(host, calls, host.env("reduce", ORK_METER_READ_COMPACT="on"))
            self.assertEqual(len(Fixture.requests), 7, hook_evidence(result.stdout, Fixture.arrivals))
            [session_id] = stream_session_ids(result.stdout)
            view = result_text(tool_results(Fixture.requests[1])["native_1"])
            outcomes = [result_text(tool_results(Fixture.requests[n])["native_%d" % n]) for n in (2, 3, 4, 5, 6)]
            # "Ork Meter refused this Write ... would delete lines of it you have not seen" is the dropped
            # reason: the model sees the bare block; the record names the unread lines.
            self.assert_blocked(outcomes[0])
            self.assertIn(line_number(original, ELIDED), after_refusal, "the record held the elided lines unread")
            self.assertTrue(all(label.startswith("function ") for label in after_refusal.values()), set(after_refusal.values()))
            # A plain whole-file re-read after the partial view is sent from the first elided line
            # (hooks/lib/read_state.py), so the host returns the lines the model had not seen, at their
            # real numbers, never its own "file unchanged" answer; that clears the record.
            reread_text = outcomes[1]
            first_elided = min(after_refusal)
            self.assertNotIn("unchanged", reread_text.lower())
            self.assertTrue(reread_text.startswith("%d\t" % first_elided), reread_text[:200])
            self.assertIn("%d\t%s" % (line_number(original, ELIDED), ELIDED), reread_text)
            self.assertEqual(after_reread, {}, "the re-read showed every line the view left out")
            self.assertEqual(on_disk, [original], "the refused Write left the file alone")
            # Every line has now been shown, so the Write guard no longer refuses.
            self.assertFalse(blocked(outcomes[2]), outcomes[2][:200])
            self.assertEqual(decisions(telemetry_rows(host.scratch, "write-guard.py", "PreToolUse"))[:1],
                             [("applied", "reply-emitted")], "the Write before the re-read was refused")

    def test_the_refused_write_leaves_the_file_unchanged(self):
        with Host(self, "ork-plugin-compact-keep-", "Read,Write", inference=Fixture) as host:
            host.work = Path(os.path.realpath(host.work))
            target = host.work / "module.py"
            original = module_text()
            target.write_text(original, encoding="utf-8", newline="")
            calls = [("Read", {"file_path": str(target)}),
                     ("Write", lambda body: rebuilt_from(result_text(tool_results(body)["native_1"]), target))]
            result = self.run_session(host, calls, host.env("reduce", ORK_METER_READ_COMPACT="on"))
            [session_id] = stream_session_ids(result.stdout)
            self.assert_blocked(result_text(tool_results(Fixture.requests[2])["native_2"]))
            self.assertGreater(len(unread_lines(host, target, session_id)), 0, "the record still guards the view")
            self.assertEqual(target.read_text(encoding="utf-8"), original, "the file is unchanged")

    def test_compaction_off_by_default_pages_before_the_read_as_before(self):
        with Host(self, "ork-plugin-compact-off-", "Read", inference=Fixture) as host:
            target = host.work / "module.py"
            original = module_text()
            target.write_text(original, encoding="utf-8")
            result = self.run_session(host, [("Read", {"file_path": str(target)})],
                                      host.env("reduce"))
            delivered = json.dumps(Fixture.requests[1]["messages"][-2:])
            # The guard's updatedInput pages the Read (48 lines, hooks/read-guard.py); the Read result hook
            # ends the page with the marker at line 49 and the fixed "shortened" notice.
            self.assertIn("Ork Meter shortened this Read of", delivered, hook_evidence(result.stdout, Fixture.arrivals))
            rendered = result_text(tool_results(Fixture.requests[1])["native_1"]).split("\n")
            total = original.count("\n") + 1
            self.assertEqual(rendered[:48], ["%d\t%s" % (number, line)
                                             for number, line in enumerate(original.split("\n")[:48], 1)])
            self.assertEqual(rendered[48], "49\t\u2026 lines 49\u2013%d not shown; Read with offset=49" % total)
            self.assertEqual(len(rendered), 49)
            self.assertEqual([row["kind"] for row in ledger_rows(host.scratch)], ["read_guard"])
            guards = telemetry_rows(host.scratch, "read-guard.py", "PreToolUse")
            self.assertEqual([(guard["decision"], guard["reason"], guard["effects"].get("input_updated")) for guard in guards],
                             [("applied", "reply-emitted", True)], "the guard paged the Read before it ran")

    def test_text_that_does_not_compact_is_paged_on_its_result_with_the_compacted_size(self):
        with Host(self, "ork-plugin-compact-page-", "Read", inference=Fixture) as host:
            target = host.work / "notes.txt"
            # Plain text has nothing compaction may elide. 600 lines, about 60 KB: over the 6,000-token
            # threshold and a 32 KiB guard threshold, within the deferral ceilings (20,000 tokens, 80,000
            # bytes), so the CLI delivers it whole (its estimate: 15,000 tokens).
            text = "".join("note %04d %s\n" % (i, "configuration " * 7) for i in range(600))
            target.write_text(text, encoding="utf-8")
            result = self.run_session(host, [("Read", {"file_path": str(target)})],
                                      host.env("reduce", ORK_METER_READ_COMPACT="on", ORK_METER_READ_GUARD_BYTES="32k"))
            read = result_text(tool_results(Fixture.requests[1])["native_1"])
            delivered = json.dumps(Fixture.requests[1]["messages"][-2:])
            # The page is as the guard would page it (300 lines), then the marker at line 301 and the fixed
            # "shortened" notice; the read_guard row is charged for what was emitted.
            self.assertIn("Ork Meter shortened this Read of", delivered, hook_evidence(result.stdout, Fixture.arrivals))
            # The host counts the empty line after the final newline too (totalLines 601).
            page = text.split("\n")[:300] + ["\u2026 lines 301\u2013%d not shown; Read with offset=301"
                                              % (text.count("\n") + 1)]
            self.assertEqual([line.rstrip() for line in read.split("\n")],
                             ["%d\t%s" % (number, line.rstrip()) for number, line in enumerate(page, 1)])
            self.assertNotIn("note 0300", read)
            [row] = ledger_rows(host.scratch)
            self.assertEqual(row["kind"], "read_guard")
            self.assertIs(row["native_text_boundary_reconciled"], True)
            self.assertEqual(row["bytes_after"], len("\n".join(page).encode("utf-8")))
            self.assertEqual(row["bytes_before"], len(text.encode("utf-8")))
            self.assertEqual(decisions(telemetry_rows(host.scratch, "dedupe-reads.py", "PostToolUse"))[:1],
                             [("applied", "reply-emitted")], "paged on its result")
            self.assertEqual(decisions(telemetry_rows(host.scratch, "read-guard.py", "PreToolUse"))[:1],
                             [("skipped", "handler-no-reply")], "let through whole by the guard")


def rebuilt_from(view_text, path, rename=None):
    """A Write a model would make from the received view: every line it was given (markers, which no
    longer reach it, dropped for safety)."""
    kept = [line.split("\t", 1)[1] for line in view_text.split("\n")]
    content = "\n".join(line for line in kept if not line.startswith(MARKER_PREFIX))
    if rename:
        content = content.replace(*rename)
    return {"file_path": str(path), "content": content}


class ForkFixture(Fixture):
    """``Fixture`` whose tool ids carry ``prefix``, so a forked process's calls don't reuse the first one's."""
    prefix = "native"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if count_tokens(self):
            return
        self.requests.append(body)
        self.arrivals.append(time.monotonic())
        number = len(self.requests)
        if number <= len(self.calls):
            name, input_ = self.calls[number - 1]
            block = ("tool", name, input_(body) if callable(input_) else input_, "%s_%d" % (self.prefix, number))
        else:
            block = ("text", "QUALIFIED")
        answer(self, body, "msg_%s_%d" % (self.prefix, number), [block])


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "opt in to installed Claude + localhost fixture")
@host_retry.retry_once
class NativeWriteGuardBypasses(unittest.TestCase):
    """#319: the five bypasses Grakk's post-merge review found, each with the real CLI. Every one must now be
    refused, and the file left unchanged. The refusal reaches the model as the CLI's bare "Blocked by hook"
    (the reason is dropped at the emission boundary); the write-guard record is the oracle for why."""

    def session(self, host, calls, *extra, persist=False):
        Fixture.calls, Fixture.requests, Fixture.arrivals = calls, [], []
        env = host.env("reduce", ORK_METER_READ_COMPACT="on")
        host.warm_hooks(env)   # #140: a cold bytecode cache cancelled SessionStart's trim-session.py on Windows CI
        result = host.run("Read and edit the fixture module.", *extra, env=env,
                          system="Use the requested local files.", persist=persist)
        host.assert_no_external_traffic()
        assert_hooks_fail_open(self, result.stdout)
        self.session_id = stream_session_ids(result.stdout)[-1]
        return result

    def setup_module(self, host):
        host.work = Path(os.path.realpath(host.work))   # one path form for the CLI (see the Edit test)
        target = host.work / "module.py"
        original = module_text()
        target.write_text(original, encoding="utf-8", newline="")
        return target, original

    def refused(self, number, prefix="native"):
        """Request ``number``'s Write was refused: the bare block, no plugin prose; returns the text."""
        text = result_text(tool_results(Fixture.requests[number])["%s_%d" % (prefix, number)])
        self.assertTrue(blocked(text), text[:200])
        self.assertNotIn("Ork Meter", text, "hook.denied's reason is dropped by clean_reply")
        return text

    def tombstoned(self, host, target):
        """The record of ``target`` is a tombstone: the "record was unreadable" reason is not emitted."""
        record = guard_record(host, target)
        self.assertIsNotNone(record)
        self.assertTrue(record.get("tombstone"), "a damaged record becomes a tombstone")
        self.assertNotIn(write_guard.view_key({"session_id": self.session_id}), record.get("cleared") or [])

    def unread(self, host, target):
        return unread_lines(host, target, self.session_id)

    def test_1_an_edit_inside_an_elided_range_then_a_rebuilt_write(self):
        with Host(self, "ork-plugin-bypass-edit-", "Read,Edit,Write", inference=Fixture) as host:
            target, original = self.setup_module(host)
            calls = [("Read", {"file_path": str(target)}),
                     ("Edit", {"file_path": str(target), "old_string": ELIDED, "new_string": ELIDED + "  # tuned"}),
                     ("Write", lambda body: rebuilt_from(result_text(tool_results(body)["native_1"]), target))]
            self.session(host, calls)
            self.assertIn("updated successfully", result_text(tool_results(Fixture.requests[2])["native_2"]))
            self.refused(3)
            self.assertIn(line_number(original, ELIDED), self.unread(host, target), "the edited range is still unread")
            self.assertEqual(target.read_text(encoding="utf-8"), original.replace(ELIDED, ELIDED + "  # tuned"))

    def test_1_replace_all_across_elided_lines_then_a_rebuilt_write(self):
        with Host(self, "ork-plugin-bypass-all-", "Read,Edit,Write", inference=Fixture) as host:
            target, original = self.setup_module(host)
            calls = [("Read", {"file_path": str(target)}),
                     ("Edit", {"file_path": str(target), "old_string": "value", "new_string": "amount", "replace_all": True}),
                     ("Write", lambda body: rebuilt_from(result_text(tool_results(body)["native_1"]), target,
                                                         rename=("value", "amount")))]
            self.session(host, calls)
            self.refused(3)
            self.assertIn(line_number(original, ELIDED), self.unread(host, target), "renamed in place: still unread")
            self.assertEqual(target.read_text(encoding="utf-8"), original.replace("value", "amount"))

    def test_2_a_stale_offset_read_after_a_shift_does_not_clear_the_range(self):
        with Host(self, "ork-plugin-bypass-shift-", "Read,Edit,Write", inference=Fixture) as host:
            target, original = self.setup_module(host)
            first_line = original.split("\n")[0]
            stale = {}

            def stale_read(body):
                # The model rereads an elided range by the offsets it was told at the Read, which an Edit has
                # made stale. No marker reaches it now: the range comes from the write-guard record, as the
                # compactor stored it (0-based), which is the same span the marker named.
                [view] = guard_record(host, target)["views"].values()
                first, last, _label = view[-2]
                stale.update(first=first + 1, last=last + 1)
                return {"file_path": str(target), "offset": first + 1, "limit": last - first + 1}

            calls = [("Read", {"file_path": str(target)}),
                     ("Edit", {"file_path": str(target), "old_string": first_line,
                               "new_string": "\n".join("# inserted %d" % i for i in range(5)) + "\n" + first_line}),
                     ("Read", stale_read),
                     ("Write", lambda body: rebuilt_from(result_text(tool_results(body)["native_1"]), target))]
            self.session(host, calls)
            self.refused(4)
            # Only the lines the stale Read returned are read: the range's last five, shifted past it, are not.
            lines = original.split("\n")
            moved = {n + 5 for n in range(stale["first"], stale["last"] + 1) if lines[n - 1].strip()}
            self.assertEqual(moved & set(self.unread(host, target)), {n for n in moved if n > stale["last"]})
            self.assertEqual(target.read_text(encoding="utf-8").count("\n"), original.count("\n") + 5)

    def test_3_a_forked_session_is_still_guarded(self):
        with Host(self, "ork-plugin-bypass-fork-", "Read,Write", inference=ForkFixture) as host:
            target, original = self.setup_module(host)
            ForkFixture.prefix = "native"
            first = self.session(host, [("Read", {"file_path": str(target)})], persist=True)
            view = result_text(tool_results(Fixture.requests[1])["native_1"])
            [session_id] = stream_session_ids(first.stdout)
            ForkFixture.prefix = "fork"
            try:
                forked = self.session(host, [("Write", rebuilt_from(view, target))], "--resume", session_id,
                                      "--fork-session", persist=True)
            finally:
                ForkFixture.prefix = "native"
            self.assertNotIn(session_id, stream_session_ids(forked.stdout), "a fork has a new session id")
            self.refused(1, prefix="fork")
            # The fork has no view of its own: it is checked against the first session's (the strictest).
            self.assertIn(line_number(original, ELIDED), self.unread(host, target))
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_4_another_case_of_the_path_on_a_case_insensitive_disk(self):
        with Host(self, "ork-plugin-bypass-case-", "Read,Write", inference=Fixture) as host:
            target, original = self.setup_module(host)
            upper = target.with_name(target.name.upper())
            insensitive = upper.exists()   # macOS and Windows: MODULE.PY is module.py
            calls = [("Read", {"file_path": str(target)}),
                     ("Write", lambda body: rebuilt_from(result_text(tool_results(body)["native_1"]), upper))]
            self.session(host, calls)
            if insensitive:
                self.refused(2)
                self.assertIsNotNone(guard_record(host, upper), "the record is found by the other spelling")
            else:
                # A case-sensitive disk: MODULE.PY is a new file, so the Write can't delete module.py's lines.
                self.assertFalse(blocked(result_text(tool_results(Fixture.requests[2])["native_2"])))
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_5_no_snapshot_and_a_corrupt_record_still_refuse_every_time(self):
        with Host(self, "ork-plugin-bypass-closed-", "Read,Write", inference=Fixture) as host:
            target, original = self.setup_module(host)

            def without_snapshots(body):
                for snapshot in Path(host.scratch).rglob("tool-output/*"):
                    snapshot.unlink()
                return rebuilt_from(result_text(tool_results(body)["native_1"]), target)

            def corrupt_record(body):
                for record in (Path(host.base) / "state" / "ork-meter" / "read-guard").glob("*.json"):
                    record.write_text("{not json", encoding="utf-8")
                return rebuilt_from(result_text(tool_results(body)["native_1"]), target)

            calls = [("Read", {"file_path": str(target)}), ("Write", without_snapshots), ("Write", corrupt_record),
                     ("Write", lambda body: rebuilt_from(result_text(tool_results(body)["native_1"]), target))]
            self.session(host, calls)
            self.refused(2)
            # "record was unreadable" was the reason of the next two refusals; it is not emitted. The record
            # on disk is the tombstone the corrupt one became, and the retry is refused by it too.
            self.refused(3)
            self.refused(4)
            self.tombstoned(host, target)
            self.assertEqual(decisions(telemetry_rows(host.scratch, "write-guard.py", "PreToolUse")),
                             [("applied", "reply-emitted")] * 3, "every Write refused, a deny emitted each time")
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_5_a_tombstone_survives_the_models_own_edit(self):
        # Grakk's repro on 70605d6: a damaged record, the Write refused, the retry refused, a one-line Edit
        # (as the refusal used to suggest), then a Write from the view, which must still be refused.
        with Host(self, "ork-plugin-bypass-tomb-", "Read,Edit,Write", inference=Fixture) as host:
            target, original = self.setup_module(host)

            def corrupt_record(body):
                for record in (Path(host.base) / "state" / "ork-meter" / "read-guard").glob("*.json"):
                    record.write_text("{not json", encoding="utf-8")
                return rebuilt_from(result_text(tool_results(body)["native_1"]), target)

            def rebuilt(body):
                return rebuilt_from(result_text(tool_results(body)["native_1"]), target)
            calls = [("Read", {"file_path": str(target)}), ("Write", corrupt_record), ("Write", rebuilt),
                     ("Edit", {"file_path": str(target), "old_string": SHOWN,
                               "new_string": "def distant_helper(value, scale=4):"}),
                     ("Write", rebuilt)]
            self.session(host, calls)
            self.refused(2)
            self.refused(3)
            self.assertIn("updated successfully", result_text(tool_results(Fixture.requests[4])["native_4"]))
            self.refused(5)   # the model's own Edit doesn't clear the tombstone
            self.tombstoned(host, target)
            self.assertEqual(target.read_text(encoding="utf-8"), original.replace("scale=3", "scale=4"))

    def test_6_duplicate_bodies_one_shown_one_elided(self):
        # Grakk's repro: two functions with identical 250-line bodies, the first shown and the second
        # elided. The shown copy must not count as keeping the elided one.
        with Host(self, "ork-plugin-bypass-twins-", "Read,Write", inference=Fixture) as host:
            host.work = Path(os.path.realpath(host.work))
            target = host.work / "twins.py"
            body = "".join("    step = step * 3 + %d  # identical line %d\n" % (i % 7, i) for i in range(250))
            original = ("".join("HEAD_%d = %d\n" % (i, i) for i in range(41)) + "def first(step):\n" + body
                        + "    return step\n\n" + "def second(step):\n" + body + "    return step\n")
            target.write_text(original, encoding="utf-8", newline="")
            calls = [("Read", {"file_path": str(target)}),
                     ("Write", lambda body: rebuilt_from(result_text(tool_results(body)["native_1"]), target))]
            self.session(host, calls)
            view = result_text(tool_results(Fixture.requests[1])["native_1"])
            self.assertEqual(view.count("identical line 0"), 1, "one body shown, one elided")
            self.refused(2)
            # The reason named "function second body"; the record's view carries that label.
            self.assertEqual(sorted(set(self.unread(host, target).values())), ["function second body"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

if __name__ == "__main__":
    unittest.main()
