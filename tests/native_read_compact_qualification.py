"""Read compaction through the installed Claude CLI, localhost synthetic inference only.

A whole-file Read of a Python module over the token threshold is let
through whole by the read guard and compacted on its result
(hooks/dedupe-reads.py, hooks/lib/read_compact.py). The next model request
must hold the compacted result, with every shown line at its original
number in the CLI's own rendering. Then, in the same session:

* an Edit of a shown line succeeds;
* an Edit of a line inside an elided range, with that line's exact text,
  succeeds too: the CLI matches old_string against the file on disk, and
  its read-before-edit state holds the whole file it read, not the result
  the hook replaced;
* an Edit whose old_string is an elision marker (text that is not in the
  file) fails safely, "String to replace not found", and changes nothing;
* a ranged Read of the elided range returns its exact lines;
* a Write rebuilt from the compacted view is refused by the Write guard
  (hooks/write-guard.py) and leaves the file unchanged; a plain whole-file
  re-read is answered by the CLI as unchanged, so it can't restore the
  view; after a ranged Read covering every elided range, the Write goes
  through.

Exactly one reducer acts on the Read: one ``read_compact`` ledger row, no
``trim`` or ``read_guard`` row. Compaction is opt-in (#316): these cases set
``ORK_METER_READ_COMPACT=on``; by default the read guard pages the same Read
before it runs, as before. A text file that
compaction can't bring under the threshold is paged on its result, with
the guard's note naming the compacted size.
"""
import json
import os
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

SHOWN = "def distant_helper(value, scale=3):"
ELIDED = "    total_30_19 = value * 30 + 19"
MARKER_PREFIX = "… lines "


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


@unittest.skipUnless(os.environ.get("RUN_NATIVE_CLAUDE_TESTS") == "1", "opt in to installed Claude + localhost fixture")
@host_retry.retry_once
class NativeReadCompaction(unittest.TestCase):
    def run_session(self, host, calls, env):
        Fixture.calls, Fixture.requests, Fixture.arrivals = calls, [], []
        result = host.run("Read and edit the fixture module.", env=env, system="Use the requested local files.")
        host.assert_no_external_traffic()
        assert_hooks_fail_open(self, result.stdout)
        return result

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
            shown_number = line_number(original, SHOWN)
            elided_number = line_number(original, ELIDED)
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
            self.assertIn("Ork Meter compacted this read", delivered, hook_evidence(result.stdout, Fixture.arrivals))
            self.assertNotIn("Ork Meter read guard", delivered)
            self.assertNotIn("Ork Meter trimmed", delivered)
            rendered = read.split("\n")
            # The CLI's own numbering: every shown line at its original number.
            self.assertIn("%d\t%s" % (shown_number, SHOWN), rendered)
            for number, line in enumerate(original.split("\n")[:40], 1):
                self.assertEqual(rendered[number - 1], "%d\t%s" % (number, line))
            self.assertNotIn(ELIDED, read)
            markers = [line for line in rendered if line.split("\t", 1)[1].startswith(MARKER_PREFIX)]
            self.assertTrue(markers)
            for line in rendered:
                number, text = line.split("\t", 1)
                source = original.split("\n")[int(number) - 1]
                self.assertTrue(text == source or text == "" or text.startswith(MARKER_PREFIX), line)
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
            self.assertNotIn(str(target), json.dumps(rows))

    def test_a_write_rebuilt_from_the_compacted_view_is_refused_until_the_ranges_are_read(self):
        with Host(self, "ork-plugin-compact-write-", "Read,Write", inference=Fixture) as host:
            host.work = Path(os.path.realpath(host.work))   # see the Edit test: one path form for the CLI
            target = host.work / "module.py"
            original = module_text()
            target.write_text(original, encoding="utf-8", newline="")
            total = original.count("\n") + 1

            def rebuilt(body):
                # What a model rebuilding the file from the compacted view would write: every shown line,
                # the markers dropped and the empty slots kept, so the elided code is gone.
                view = result_text(tool_results(body)["native_1"]).split("\n")
                kept = [line.split("\t", 1)[1] for line in view]
                return {"file_path": str(target), "content": "\n".join(line for line in kept
                                                                       if not line.startswith(MARKER_PREFIX))}
            calls = [("Read", {"file_path": str(target)}),
                     ("Write", rebuilt),
                     ("Read", {"file_path": str(target)}),
                     ("Read", {"file_path": str(target), "offset": 1, "limit": total}),
                     ("Write", rebuilt)]
            result = self.run_session(host, calls, host.env("reduce", ORK_METER_READ_COMPACT="on"))
            self.assertEqual(len(Fixture.requests), 6, hook_evidence(result.stdout, Fixture.arrivals))
            outcomes = [result_text(tool_results(Fixture.requests[n])["native_%d" % n]) for n in (2, 3, 4, 5)]
            self.assertIn("Ork Meter refused this Write", outcomes[0])
            self.assertIn("would delete lines of it you have not seen", outcomes[0])
            # A plain whole-file re-read is answered by the CLI itself, so it can't restore the elided lines.
            self.assertIn("unchanged", outcomes[1].lower())
            self.assertNotIn(ELIDED, outcomes[1])
            self.assertIn(ELIDED, outcomes[2], "a ranged Read returns the elided lines")
            self.assertNotIn("refused", outcomes[3], "every elided range read: the Write is allowed")
            written = rebuilt(Fixture.requests[2])["content"]
            self.assertEqual(target.read_text(encoding="utf-8"), written)
            self.assertLess(len(written), len(original))

    def test_the_refused_write_leaves_the_file_unchanged(self):
        with Host(self, "ork-plugin-compact-keep-", "Read,Write", inference=Fixture) as host:
            host.work = Path(os.path.realpath(host.work))
            target = host.work / "module.py"
            original = module_text()
            target.write_text(original, encoding="utf-8", newline="")
            calls = [("Read", {"file_path": str(target)}),
                     ("Write", lambda body: {"file_path": str(target), "content": "\n".join(
                         line.split("\t", 1)[1] for line in result_text(tool_results(body)["native_1"]).split("\n")
                         if not line.split("\t", 1)[1].startswith(MARKER_PREFIX))})]
            self.run_session(host, calls, host.env("reduce", ORK_METER_READ_COMPACT="on"))
            self.assertIn("Ork Meter refused this Write", result_text(tool_results(Fixture.requests[2])["native_2"]))
            self.assertEqual(target.read_text(encoding="utf-8"), original, "the file is unchanged")

    def test_compaction_off_by_default_pages_before_the_read_as_before(self):
        with Host(self, "ork-plugin-compact-off-", "Read", inference=Fixture) as host:
            target = host.work / "module.py"
            target.write_text(module_text(), encoding="utf-8")
            result = self.run_session(host, [("Read", {"file_path": str(target)})],
                                      host.env("reduce"))
            delivered = json.dumps(Fixture.requests[1]["messages"][-2:])
            self.assertIn("Ork Meter read guard", delivered, hook_evidence(result.stdout, Fixture.arrivals))
            self.assertNotIn("Ork Meter compacted", delivered)
            self.assertEqual([row["kind"] for row in ledger_rows(host.scratch)], ["read_guard"])

    def test_text_that_does_not_compact_is_paged_on_its_result_with_the_compacted_size(self):
        with Host(self, "ork-plugin-compact-page-", "Read", inference=Fixture) as host:
            target = host.work / "notes.txt"
            # Plain text has nothing compaction may elide. 600 lines, about 60 KB: over the 6,000-token
            # threshold and a 32 KiB guard threshold, within the deferral ceilings (20,000 tokens, 80,000
            # bytes), so the CLI delivers it whole (its estimate: 15,000 tokens).
            target.write_text("".join("note %04d %s\n" % (i, "configuration " * 7) for i in range(600)),
                              encoding="utf-8")
            result = self.run_session(host, [("Read", {"file_path": str(target)})],
                                      host.env("reduce", ORK_METER_READ_COMPACT="on", ORK_METER_READ_GUARD_BYTES="32k"))
            read = result_text(tool_results(Fixture.requests[1])["native_1"])
            delivered = json.dumps(Fixture.requests[1]["messages"][-2:])
            self.assertIn("Ork Meter read guard", delivered, hook_evidence(result.stdout, Fixture.arrivals))
            self.assertIn("compacted, it is still about", delivered)
            self.assertIn("300\tnote 0299", read)
            self.assertNotIn("note 0300", read)
            self.assertEqual([row["kind"] for row in ledger_rows(host.scratch)], ["read_guard"])


def rebuilt_from(view_text, path, rename=None):
    """A Write a model would make from the compacted view: every shown line, markers dropped."""
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
    refused, and the file left unchanged."""

    def session(self, host, calls, *extra, persist=False):
        Fixture.calls, Fixture.requests, Fixture.arrivals = calls, [], []
        result = host.run("Read and edit the fixture module.", *extra, env=host.env("reduce", ORK_METER_READ_COMPACT="on"),
                          system="Use the requested local files.", persist=persist)
        host.assert_no_external_traffic()
        assert_hooks_fail_open(self, result.stdout)
        return result

    def setup_module(self, host):
        host.work = Path(os.path.realpath(host.work))   # one path form for the CLI (see the Edit test)
        target = host.work / "module.py"
        original = module_text()
        target.write_text(original, encoding="utf-8", newline="")
        return target, original

    def refused(self, number):
        text = result_text(tool_results(Fixture.requests[number])["native_%d" % number])
        self.assertIn("Ork Meter refused this Write", text)
        return text

    def test_1_an_edit_inside_an_elided_range_then_a_rebuilt_write(self):
        with Host(self, "ork-plugin-bypass-edit-", "Read,Edit,Write", inference=Fixture) as host:
            target, original = self.setup_module(host)
            calls = [("Read", {"file_path": str(target)}),
                     ("Edit", {"file_path": str(target), "old_string": ELIDED, "new_string": ELIDED + "  # tuned"}),
                     ("Write", lambda body: rebuilt_from(result_text(tool_results(body)["native_1"]), target))]
            self.session(host, calls)
            self.assertIn("updated successfully", result_text(tool_results(Fixture.requests[2])["native_2"]))
            self.refused(3)
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
            self.assertEqual(target.read_text(encoding="utf-8"), original.replace("value", "amount"))

    def test_2_a_stale_offset_read_after_a_shift_does_not_clear_the_range(self):
        with Host(self, "ork-plugin-bypass-shift-", "Read,Edit,Write", inference=Fixture) as host:
            target, original = self.setup_module(host)
            first_line = original.split("\n")[0]

            def stale_read(body):
                # The model rereads the elided range by the marker's offsets, which an Edit has made stale.
                view = result_text(tool_results(body)["native_1"]).split("\n")
                marker = [line for line in view if line.split("\t", 1)[1].startswith(MARKER_PREFIX)][-2]
                first, last = (int(n) for n in marker.split("\t", 1)[1].split(" ")[2].split("\u2013"))
                return {"file_path": str(target), "offset": first, "limit": last - first + 1}

            calls = [("Read", {"file_path": str(target)}),
                     ("Edit", {"file_path": str(target), "old_string": first_line,
                               "new_string": "\n".join("# inserted %d" % i for i in range(5)) + "\n" + first_line}),
                     ("Read", stale_read),
                     ("Write", lambda body: rebuilt_from(result_text(tool_results(body)["native_1"]), target))]
            self.session(host, calls)
            self.refused(4)
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
            self.assertIn("Ork Meter refused this Write", result_text(tool_results(Fixture.requests[1])["fork_1"]))
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
            else:
                # A case-sensitive disk: MODULE.PY is a new file, so the Write can't delete module.py's lines.
                self.assertNotIn("refused", result_text(tool_results(Fixture.requests[2])["native_2"]))
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
            self.assertIn("record was unreadable", self.refused(3))
            self.assertIn("record was unreadable", self.refused(4), "the retry is refused too (a tombstone)")
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_5_a_tombstone_survives_the_models_own_edit(self):
        # Grakk's repro on 70605d6: a damaged record, the Write refused, the retry refused, a one-line Edit
        # (as the refusal suggests), then a Write from the view, which must still be refused.
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
            self.assertIn("record was unreadable", self.refused(5), "the model's own Edit doesn't clear the tombstone")
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
            self.assertIn("function second body", self.refused(2))
            self.assertEqual(target.read_text(encoding="utf-8"), original)

if __name__ == "__main__":
    unittest.main()
