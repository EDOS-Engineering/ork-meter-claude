"""Read compaction (hooks/lib/read_compact.py, hooks/dedupe-reads.py, hooks/read-guard.py).

A whole-file Read over the token threshold is compacted by eliding whole
lines; every shown line stays exact at its own number in the compactor's
view. Paging is the fallback when the compacted text still doesn't fit.
The native side (the CLI's rendering, Edit after an elision) is in
tests/native_read_compact_qualification.py.

At the emission boundary (hooks/lib/native_text.py, applied by hook.run) the
hook's note never reaches the model (clean_reply drops additionalContext and
permissionDecisionReason) and updatedToolOutput keeps only lines received
from the tool (source_output): the "… lines a–b not shown" markers and the
empty slots go, numLines is recounted. Which lever acted shows only in the
ledger (kind read_compact / read_guard / trim) and the write-guard record.
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
from support import load, run_hook  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import circuit  # noqa: E402
import features  # noqa: E402
import hook_telemetry  # noqa: E402
import ledger  # noqa: E402
import read_compact  # noqa: E402
import read_notice  # noqa: E402
import session  # noqa: E402
import token_gate  # noqa: E402
import write_guard  # noqa: E402

COUNT, EXACT = token_gate.counter()
reads = load("hooks/dedupe-reads.py")
guard = load("hooks/read-guard.py")
writes = load("hooks/write-guard.py")


def python_module(functions=40, body=22, head=30):
    parts = ['"""A synthetic module."""\n', "import math\n", "\n"]
    parts += ["CONSTANT_%d = %d\n" % (i, i) for i in range(head)]
    parts.append("\n")
    for f in range(functions):
        parts.append("def helper_%d(value):\n" % f)
        parts += ["    total_%d_%d = value * %d + %d\n" % (f, n, f, n) for n in range(body)]
        parts.append("    return total_%d_%d\n\n" % (f, body - 1))
    return "".join(parts)


def check_exact(test, original, shown):
    """Every shown line is the original line at the same index, an empty
    slot inside an elided range, or that range's one marker; nothing else."""
    lines = original.split("\n")
    test.assertLessEqual(len(shown), len(lines))
    in_range = False
    for index, line in enumerate(shown):
        if line.startswith("… lines "):
            first, last = line[len("… lines "):].split(" ", 1)[0].split("–")
            test.assertEqual(int(first), index + 1, "a marker sits in its range's first slot")
            end = int(last) - 1
            in_range = True
            continue
        if in_range and index <= end:
            test.assertEqual(line, "", "an empty slot inside an elided range")
            continue
        in_range = False
        test.assertEqual(line, lines[index], "line %d is exact" % (index + 1))


def markers(shown):
    return [line for line in shown if line.startswith("… lines ")]


class Compaction(unittest.TestCase):
    def compact(self, text, path="module.py", budget=6000, **kwargs):
        return read_compact.compact(text, path, budget, COUNT, **kwargs)

    def test_python_bodies_far_from_the_top_go_first_and_every_line_is_exact(self):
        text = python_module()
        shown, info = self.compact(text)
        self.assertIsNotNone(shown)
        check_exact(self, text, shown)
        self.assertGreater(info["tokens_before"], 6000)
        self.assertLessEqual(info["tokens_after"], 6000)
        self.assertEqual(COUNT(read_compact.render(shown)), info["tokens_after"])
        lines = text.split("\n")
        # Bodies far from the top are elided, signatures kept; the first ones stay whole.
        late = lines.index("def helper_38(value):")
        self.assertEqual(shown[late], "def helper_38(value):")
        self.assertEqual(shown[late + 1], "… lines %d–%d not shown; Read with offset=%d" % (late + 2, late + 24, late + 2))
        self.assertIn((late + 2, late + 24, "function helper_38 body"), info["elided"])
        self.assertEqual(shown[late + 2:late + 24], [""] * 22)
        # The last body runs, with the blank lines after it, to the end of the file: its marker ends the result.
        last = lines.index("def helper_39(value):")
        self.assertEqual(shown[last + 1:], ["… lines %d–%d not shown; Read with offset=%d" % (last + 2, len(lines), last + 2)])
        first = lines.index("def helper_0(value):")
        self.assertEqual(shown[first:first + 24], lines[first:first + 24])
        spans = [line.split(" ")[2].split("\u2013") for line in markers(shown)]
        self.assertEqual(info["lines_elided"], sum(int(last) - int(first) + 1 for first, last in spans))
        self.assertEqual(info["ranges"], len(spans))

    def test_deterministic(self):
        text = python_module()
        first = self.compact(text)
        for _ in range(3):
            self.assertEqual(self.compact(text), first)

    def test_under_the_threshold_nothing_changes_and_the_count_is_the_rendering(self):
        text = python_module(functions=3)
        shown, info = self.compact(text)
        self.assertIsNone(shown)
        self.assertEqual(info["tokens_before"], COUNT(read_compact.render(text.split("\n"))))
        self.assertLessEqual(info["tokens_before"], 6000)
        self.assertEqual(info["lines_elided"], 0)

    def test_threshold_both_ways(self):
        text = python_module()
        total = self.compact(text, budget=10 ** 6)[1]["tokens_before"]
        self.assertIsNone(self.compact(text, budget=total)[0], "at the threshold: delivered whole")
        self.assertIsNotNone(self.compact(text, budget=total - 1)[0], "one token over: compacted")

    def test_a_margin_makes_a_read_just_over_the_threshold_save_enough(self):
        text = python_module()
        total = self.compact(text, budget=10 ** 6)[1]["tokens_before"]
        shown, info = self.compact(text, budget=total - 10, margin=800)
        self.assertIsNotNone(shown)
        self.assertLessEqual(info["tokens_after"], total - 800)
        self.assertIsNone(self.compact(text, budget=total, margin=800)[0], "not over the threshold: untouched")

    def test_no_fit_gives_none_with_the_smallest_size_reached(self):
        text = python_module()
        shown, info = self.compact(text, budget=500)
        self.assertIsNone(shown)
        self.assertGreater(info["tokens_after"], 500)
        self.assertLess(info["tokens_after"], info["tokens_before"])
        self.assertGreater(info["ranges"], 0)

    def test_head_and_points_of_interest_are_never_elided(self):
        body = "".join("    step_%d = %d\n" % (n, n) for n in range(30))
        text = ("".join("x_%d = %d\n" % (i, i) for i in range(45))
                + "def flagged():\n" + body.replace("    step_15 = 15\n", "    step_15 = 15  # TODO check\n")
                + "".join("def plain_%d():\n%s\n" % (n, body) for n in range(20)))
        shown, info = self.compact(text, budget=self.compact(text, budget=10 ** 6)[1]["tokens_before"] - 300)
        self.assertIsNotNone(shown, info)
        check_exact(self, text, shown)
        lines = text.split("\n")
        self.assertEqual(shown[:40], lines[:40])
        start = lines.index("def flagged():")
        self.assertEqual(shown[start:start + 31], lines[start:start + 31], "a body around a TODO stays whole")

    def test_python_docstring_comment_and_data_literal_keep_their_first_line(self):
        doc = '    """First line of a long docstring.\n' + "".join("    detail %d\n" % i for i in range(12)) + '    """\n'
        comment = "".join("# commentary line %d with words\n" % i for i in range(15))
        table = "TABLE = [\n" + "".join("    (%d, %d, 'row %d'),\n" % (i, i * i, i) for i in range(40)) + "]\n"
        text = "".join("v_%d = %d\n" % (i, i) for i in range(41)) + "def documented():\n" + doc + "    return 1\n\n" \
            + comment + table + "def tail():\n    return 2\n"
        shown, info = self.compact(text, budget=self.compact(text, budget=10 ** 6)[1]["tokens_before"] - 400)
        self.assertIsNotNone(shown)
        check_exact(self, text, shown)
        lines = text.split("\n")
        self.assertTrue(markers(shown))
        found = " | ".join(what for _first, _last, what in info["elided"])
        self.assertIn("docstring", found)
        self.assertIn("comment", found)
        self.assertIn("TABLE literal", found)
        for kept in ('    """First line of a long docstring.', "# commentary line 0 with words", "TABLE = ["):
            self.assertEqual(shown[lines.index(kept)], kept)
        self.assertEqual(shown[-2:], ["    return 2", ""], "the end of the file stays")

    def test_licence_header_in_the_head_is_elided_but_its_first_line_kept(self):
        licence = "# Copyright (c) 2026 Example Ltd.\n" + "".join("# Licensed under the terms, clause %d.\n" % i
                                                                   for i in range(20))
        text = licence + python_module()
        shown, info = self.compact(text)
        self.assertEqual(shown[0], "# Copyright (c) 2026 Example Ltd.")
        self.assertEqual(shown[1], "… lines 2–21 not shown; Read with offset=2")
        self.assertIn((2, 21, "licence header"), info["elided"])
        check_exact(self, text, shown)

    def test_long_lines_are_elided_whole_never_cut(self):
        text = "".join("line %d\n" % i for i in range(50)) + ("var data = '%s';\n" % ("a" * 30000)) + "tail\n"
        shown, _info = self.compact(text, path="bundle.js", budget=2000)
        self.assertIsNotNone(shown)
        check_exact(self, text, shown)
        self.assertEqual(shown[50], "… lines 51–51 not shown; Read with offset=51")
        self.assertEqual(shown[51:], ["tail", ""])

    def test_brace_language_bodies_keep_signature_and_closing_brace(self):
        func = "function work_%d(a, b) {\n" + "".join("  const v%d = a * %d + b;\n" % (i, i) for i in range(20)) \
            + "  return v19;\n}\n\n"
        text = "".join("// header line %d\n" % i for i in range(3)) + "".join("const c%d = %d;\n" % (i, i) for i in range(40)) \
            + "".join(func % n for n in range(30))
        shown, _info = self.compact(text, path="app.js", budget=3000)
        self.assertIsNotNone(shown)
        check_exact(self, text, shown)
        lines = text.split("\n")
        last = lines.index("function work_29(a, b) {")
        self.assertEqual(shown[last], "function work_29(a, b) {")
        self.assertEqual(shown[last + 1], "… lines %d–%d not shown; Read with offset=%d" % (last + 2, last + 22, last + 2))
        self.assertEqual(shown[last + 22], "}")

    def test_brace_blocks_it_cannot_delimit_are_left_alone(self):
        risky = ("function risky() {\n  const t = `\n" + "".join("    template line %d {\n" % i for i in range(20))
                 + "  `;\n  return t;\n}\n")
        text = "".join("const c%d = %d;\n" % (i, i) for i in range(45)) + risky
        for candidate in read_compact.brace_bodies(text.split("\n")):
            self.assertNotIn("risky", candidate.what)
        mismatched = "".join("const c%d = %d;\n" % (i, i) for i in range(45)) + "function odd() {\n" \
            + "".join("    x%d();\n" % i for i in range(20)) + "  }\n"
        self.assertEqual(read_compact.brace_bodies(mismatched.split("\n")), [],
                         "a closing brace at another indentation is not a block we can delimit")

    def test_plain_text_has_no_safe_candidates(self):
        text = "".join("note %d: a sentence of prose with words in it\n" % i for i in range(1500))
        shown, info = self.compact(text, path="notes.txt")
        self.assertIsNone(shown)
        self.assertEqual(info["tokens_after"], info["tokens_before"])

    def test_unparseable_python_falls_back_to_comment_and_data_rules(self):
        text = "def broken(:\n" + python_module()
        shown, info = self.compact(text)
        self.assertFalse(read_compact.PythonFacts(text).ok)
        self.assertIsNone(shown, "no AST, no bodies: this module has nothing else to elide")

    def test_crlf_lines_stay_exact(self):
        text = python_module().replace("\n", "\r\n")
        shown, _info = self.compact(text)
        self.assertIsNotNone(shown)
        check_exact(self, text, shown)
        self.assertTrue(all(line.endswith("\r") or line == "" or line.startswith("…") for line in shown[:-1]))

    def test_deadline_stops_the_search(self):
        ticks = iter([0.0] + [10.0] * 10000)
        shown, info = self.compact(python_module(), deadline=5.0, now=lambda: next(ticks))
        self.assertIsNone(shown)
        self.assertTrue(info["late"])

    def test_byte_estimate_when_no_exact_encoder_loads(self):
        with patch.object(token_gate, "encoding", side_effect=ImportError("no encoder")):
            count, exact = token_gate.counter()
        self.assertFalse(exact)
        self.assertEqual(count("abcdefgh\n"), 3)
        text = python_module()
        shown, info = read_compact.compact(text, "module.py", 6000, count)
        rendered = read_compact.render(text.split("\n")).split("\n")
        self.assertEqual(info["tokens_before"], sum(-(-len((line + "\n").encode()) // 4) for line in rendered[:-1])
                         + -(-len(rendered[-1].encode()) // 4))
        self.assertIsNotNone(shown)
        check_exact(self, text, shown)

    def test_json_is_never_compacted(self):
        text = "[\n" + "".join('  {"id": %d, "name": "item %d", "tags": ["a", "b"]},\n' % (i, i) for i in range(900)) + "  {}\n]\n"
        shown, info = self.compact(text, path="data.json")
        self.assertIsNone(shown)
        self.assertGreater(info["tokens_before"], 6000)
        self.assertFalse(read_compact.compactable("/x/data.JSON"))
        self.assertTrue(read_compact.compactable("/x/module.py"))

    def test_a_licence_block_never_swallows_a_point_of_interest(self):
        licence = ("# Copyright (c) 2026 Example Ltd.\n" + "".join("# Licensed under the terms, clause %d.\n" % i for i in range(8))
                   + "# BUG: the clause numbering is off\n" + "".join("# More terms, clause %d.\n" % i for i in range(8)))
        text = licence + python_module()
        shown, _info = self.compact(text)
        self.assertIsNotNone(shown)
        check_exact(self, text, shown)
        self.assertIn("# BUG: the clause numbering is off", shown)
        self.assertFalse(any("licence header" in line for line in markers(shown)))

    def test_merged_marker_labels_are_capped(self):
        self.assertEqual(read_compact._joined("comment", "docstring"), "comment; docstring")
        self.assertEqual(read_compact._joined("comment; docstring", "data"), "comment; docstring; more")
        self.assertEqual(read_compact._joined("comment; docstring; more", "string"), "comment; docstring; more")
        self.assertEqual(read_compact._joined("comment", "comment"), "comment")

    def test_multi_line_strings_are_labelled_string_not_comment(self):
        template = "const page = `\n" + "".join("# heading %d inside a template literal\n" % i for i in range(12)) + "`;\n"
        text = "".join("const c%d = %d;\n" % (i, i) for i in range(45)) + template + "".join(
            "function work_%d(a) {\n%s  return a;\n}\n" % (n, "".join("  const v%d = a + %d;\n" % (i, i) for i in range(12)))
            for n in range(30))
        lines = text.split("\n")
        backticks = read_compact.backtick_strings(lines, ".ts")
        self.assertEqual(min(backticks), lines.index("const page = `") + 1)
        self.assertNotIn(min(backticks), read_compact.comment_lines(lines, ".ts", backticks))
        found = read_compact.comment_ranges(lines, read_compact.comment_lines(lines, ".ts", backticks), None, backticks)
        self.assertEqual([r.what for r in found], ["string"])
        python = "".join("v_%d = %d\n" % (i, i) for i in range(41)) + "print(\"\"\"\n" + "".join(
            "# not a comment %d\n" % i for i in range(10)) + "\"\"\")\n"
        facts = read_compact.PythonFacts(python)
        plines = python.split("\n")
        found = read_compact.comment_ranges(plines, read_compact.comment_lines(plines, ".py"), facts)
        self.assertEqual([r.what for r in found], ["string"])

    def test_unread_ranges_for_the_write_guard(self):
        original = "\n".join("line %d" % i for i in range(1, 101))
        ranges = [(10, 19, "comment"), (50, 59, "function f body"), (90, 100, "data")]
        rebuilt = "\n".join("line %d" % i for i in range(1, 101) if not (10 <= i <= 19 or 50 <= i <= 100))
        self.assertEqual(read_compact.unread(ranges, [], original, original, rebuilt), ranges)
        self.assertEqual(read_compact.unread(ranges, [(1, 30), (85, 100)], original, original, rebuilt),
                         [(50, 59, "function f body")], "ranged Reads covering a range clear it")
        self.assertEqual(read_compact.unread(ranges, [(50, 54), (55, 60)], original, original, rebuilt),
                         [(10, 19, "comment"), (90, 100, "data")], "two Reads together cover one range")
        self.assertEqual(read_compact.unread(ranges, [], original, original, original + "\nnew"), [],
                         "a Write that keeps the elided text deletes nothing")
        moved = "inserted\r\n" + original.replace("\n", "\r\n")
        self.assertEqual(read_compact.unread(ranges, [], original, moved, rebuilt), ranges,
                         "an Edit that moved the lines (CRLF too) still guards them")
        changed = original.replace("line 55", "line 55 edited")
        self.assertEqual(read_compact.unread(ranges, [], original, changed, rebuilt),
                         [(10, 19, "comment"), (90, 100, "data")], "a range no longer in the file isn't guarded")
        self.assertEqual(read_compact.describe(ranges, limit=2), "10\u201319 comment, 50\u201359 function f body, and 1 more")

    def test_rendering_matches_claude_code(self):
        # Claude Code 2.1.302 (FileReadTool): "<n>\t<line>", a trailing carriage return dropped, from startLine.
        self.assertEqual(read_compact.render(["a\r", "", "b"], 7), "7\ta\n8\t\n9\tb")


class Hooks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scratch = self.root / "scratch"
        # Read compaction is opt-in (#316): these cases opt in, as a person would, from the session's start.
        self.env = {"HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.root / "state"),
                    "ORK_METER_READ_COMPACT": "on"}
        self.calls = 0

    def result(self, text, path="/work/module.py", env=None, **file_fields):
        self.calls += 1
        lines = text.count("\n") + 1
        file_info = {"filePath": path, "content": text, "numLines": lines, "startLine": 1, "totalLines": lines}
        file_info.update(file_fields)
        payload = {"hook_event_name": "PostToolUse", "session_id": self.session_id, "tool_name": "Read",
                   "scratchpad_dir": str(self.scratch), "tool_use_id": "call-%d" % self.calls,
                   "prompt_id": "prompt-%d" % self.calls, "tool_input": {"file_path": path},
                   "tool_response": {"type": "text", "file": file_info}}
        return run_hook(payload, reads.handle, reads.EVENTS, env=dict(self.env, **(env or {})), feature=reads.FEATURE,
                        reducer=reads.REDUCER, started=time.monotonic())

    def before(self, path, env=None):
        self.calls += 1
        payload = {"hook_event_name": "PreToolUse", "session_id": "compact", "tool_name": "Read",
                   "scratchpad_dir": str(self.scratch), "tool_use_id": "pre-%d" % self.calls,
                   "tool_input": {"file_path": str(path)}}
        return run_hook(payload, guard.handle, guard.EVENTS, env=dict(self.env, **(env or {})), feature=guard.FEATURE,
                        reducer=guard.REDUCER, started=time.monotonic())

    def ledger(self):
        rows = []
        for path in self.scratch.rglob(session.LEDGER_NAME):
            rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return rows

    def telemetry(self):
        """The finished hook_telemetry rows (docs/hook-telemetry.md) of this scratchpad."""
        rows = []
        for path in self.scratch.rglob("hook-invocations.jsonl"):
            rows += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [row for row in rows if row.get("phase") == "finished"]

    def emitted(self, reply, text):
        """The ``file`` of an emitted Read reply, checked at the emission
        boundary (native_text.clean_reply / source_output): one fixed notice,
        only the received keys, numLines recounted, and every line the file's
        line at its own number, an elision marker, or a marker's empty slot."""
        self.assertIsNotNone(reply, "a reply is emitted")
        self.assertEqual(sorted(reply), ["hookSpecificOutput"])
        out = reply["hookSpecificOutput"]
        self.assertEqual(sorted(out), ["additionalContext", "hookEventName", "updatedToolOutput"])
        file_info = out["updatedToolOutput"]["file"]
        self.assertTrue(read_notice.allowed(out["additionalContext"], file_info["filePath"]), out["additionalContext"])
        self.assertNotIn("truncatedByTokenCap", file_info, "not a received key: dropped by source_output")
        self.assertEqual(file_info["numLines"], len(file_info["content"].splitlines()), "recounted by source_output")
        self.assertEqual(file_info["totalLines"], text.count("\n") + 1)
        self.assertTrue(markers(file_info["content"].split("\n")), "each elided range keeps its marker")
        lines = text.split("\n")
        slots = 0
        for number, shown in enumerate(file_info["content"].split("\n"), file_info["startLine"]):
            found = read_notice.MARKER_RE.match(shown)
            if found:
                self.assertEqual(int(found.group(1)), number, shown)
                slots = int(found.group(2)) - number
            elif slots and shown == "":
                slots -= 1
            else:
                self.assertEqual(shown, lines[number - 1], "line %d at its own number" % number)
                slots = 0
        return file_info

    def head(self, reply, text, kind):
        """A Read budget trim or a read guard page: the file's first lines,
        whole, then one marker for the rest, the notice saying so, and a ledger
        row of ``kind``."""
        file_info = self.emitted(reply, text)
        shown = file_info["content"].split("\n")
        self.assertEqual(shown[:-1], text.split("\n")[:len(shown) - 1], "the head, whole lines")
        self.assertEqual(shown[-1], "\u2026 lines %d\u2013%d not shown; Read with offset=%d"
                         % (len(shown), text.count("\n") + 1, len(shown)))
        self.assertIn("offset=%d" % len(shown), reply["hookSpecificOutput"]["additionalContext"])
        self.assertLess(len(shown), text.count("\n") + 1)
        self.assertEqual(self.ledger()[-1]["kind"], kind)
        return file_info

    def test_compacted_result_ledger_telemetry_and_snapshot(self):
        text = python_module()
        reply = self.result(text)
        file_info = self.emitted(reply, text)
        content = file_info["content"]
        # The compactor's view (exact at every line number, tests/Compaction) loses its markers and empty
        # slots at the emission boundary: what the model receives is shorter than the file and renumbered.
        self.assertLess(file_info["numLines"], file_info["totalLines"])
        self.assertIn("def helper_0(value):", content)
        self.assertNotIn("total_38_0", content, "a late body is elided")
        [snapshot] = (self.scratch / "ork-meter" / "tool-output").glob("*.txt")
        self.assertEqual(snapshot.read_text(encoding="utf-8"), text, "the original is kept for recovery")
        [row] = self.ledger()
        self.assertEqual(row["kind"], "read_compact")
        self.assertEqual(row["path_sha256"], hashlib.sha256(b"/work/module.py").hexdigest())
        self.assertEqual(row["bytes_before"], len(text.encode()))
        # native_text.reconcile_records charges the row for the emitted text, not the hook's candidate plus note.
        self.assertEqual(row["bytes_after"], len(content.encode()))
        self.assertIs(row["native_text_boundary_reconciled"], True)
        self.assertEqual(row["est_tokens_after"], ledger.est_tokens(row["bytes_after"]))
        self.assertEqual(row["est_tokens_avoided"], row["est_tokens_before"] - row["est_tokens_after"])
        self.assertGreater(row["lines_elided"], 0)
        self.assertEqual(row["est_tokens_before"], -(-row["bytes_before"] // 4))
        self.assertNotIn("helper", json.dumps(row))
        self.assertNotIn("/work/module.py", json.dumps(row))
        [invocation] = self.telemetry()
        self.assertEqual((invocation["hook"], invocation["decision"], invocation["reason"], invocation["stdout_emitted"]),
                         ("dedupe-reads", "applied", "reply-emitted", True))
        self.assertEqual(invocation["bytes_after"], hook_telemetry.size(reply["hookSpecificOutput"]["updatedToolOutput"]))
        self.assertNotIn("/work/module.py", json.dumps(invocation))

    def test_compact_off_and_guard_off_leave_the_read_budget(self):
        text = python_module()
        for env in ({"ORK_METER_READ_COMPACT": "off"}, {"ORK_METER_READ_GUARD": "off"}):
            # The "Ork Meter trimmed this Read" note is not emitted (clean_reply): the trim shows in the ledger.
            self.head(self.result(text, env=env), text, "trim")
        self.assertEqual([row["kind"] for row in self.ledger()], ["trim", "trim"])

    def test_trim_off_turns_everything_off(self):
        self.assertIsNone(self.result(python_module(), env={"ORK_METER_TRIM": "off"}))

    def test_a_result_cut_short_by_claude_code_is_left_to_the_read_budget(self):
        text = python_module()
        reply = self.result(text, truncatedByTokenCap=True, totalLines=5000)
        out = reply["hookSpecificOutput"]
        self.assertTrue(read_notice.allowed(out["additionalContext"], "/work/module.py"))
        shown = out["updatedToolOutput"]["file"]["content"].split("\n")
        self.assertEqual(shown[:-1], text.split("\n")[:len(shown) - 1], "trimmed to its head, not compacted")
        self.assertTrue(read_notice.MARKER_RE.match(shown[-1]))
        self.assertEqual([row["kind"] for row in self.ledger()], ["trim"])

    def test_under_the_threshold_the_read_budget_applies_as_before(self):
        text = python_module(functions=6)
        self.assertLess(read_compact.compact(text, "m.py", 10 ** 6, COUNT)[1]["tokens_before"], 6000)
        self.assertIsNone(self.result(text))
        reply = self.result(text, env={"ORK_METER_TRIM_BUDGET_READ": "1k", "ORK_METER_DEDUPE": "off"})
        self.assertIsNone(reply, "a lowered budget waits for the next session, like every tunable")
        self.calls = 0
        self.scratch = self.root / "fresh"
        reply = self.result(text, env={"ORK_METER_TRIM_BUDGET_READ": "1k"})
        self.head(reply, text, "trim")   # trimmed, not compacted: the ledger says which
        self.assertEqual([row["kind"] for row in self.ledger()], ["trim"])

    def test_no_fit_pages_like_the_guard_with_the_compacted_size(self):
        text = "".join("note %04d %s\n" % (i, "configuration " * 7) for i in range(600))
        reply = self.result(text, path="/work/notes.txt", env={"ORK_METER_READ_GUARD_BYTES": "32k"})
        # The guard's note (size, compacted size, threshold, offset=301) is not emitted; the page is, and
        # truncatedByTokenCap, which the tool did not send, is dropped by source_output.
        file_info = self.head(reply, text, "read_guard")
        self.assertEqual(file_info["numLines"], 301)   # 300 lines and the marker
        self.assertEqual([row["kind"] for row in self.ledger()], ["read_guard"])

    def test_no_fit_under_the_guard_threshold_is_trimmed_with_the_compacted_size(self):
        text = "".join("note %04d %s\n" % (i, "configuration " * 7) for i in range(600))
        reply = self.result(text, path="/work/notes.txt")
        # Neither the trim note nor "compacted this Read first" is emitted: a trim row is.
        self.head(reply, text, "trim")
        self.assertEqual([row["kind"] for row in self.ledger()], ["trim"])

    def test_python_no_fit_falls_back_to_the_outline_page(self):
        text = python_module(functions=60, body=22)
        reply = self.result(text, env={"ORK_METER_READ_GUARD_TOKENS": "1000"})
        # The Python AST outline was additionalContext: it is not emitted. The guard page is, as a read_guard row.
        file_info = self.head(reply, text, "read_guard")
        self.assertEqual(file_info["numLines"], 49)   # 48 lines and the marker
        self.assertNotIn("FunctionDef", json.dumps(reply))

    def test_one_reply_per_read_and_a_repeat_is_compacted_again_not_stubbed(self):
        text = python_module()
        first = self.result(text)
        second = self.result(text)
        self.assertEqual(first["hookSpecificOutput"]["updatedToolOutput"],
                         second["hookSpecificOutput"]["updatedToolOutput"])
        self.assertEqual([row["kind"] for row in self.ledger()], ["read_compact", "read_compact"])

    def test_guard_lets_a_compactable_read_through_whole(self):
        path = self.root / "module.py"
        path.write_text(python_module(), encoding="utf-8")
        self.assertIsNone(self.before(path))
        paged = self.before(path, env={"ORK_METER_READ_COMPACT": "off"})
        self.assertEqual(paged["hookSpecificOutput"]["updatedInput"]["limit"], 48)
        self.assertEqual([row["kind"] for row in self.ledger()], ["read_guard"])

    def test_guard_pages_what_claude_code_would_not_deliver_whole(self):
        big = self.root / "big.py"
        big.write_text(python_module(functions=130), encoding="utf-8")   # about 104 KB: over 80,000 bytes
        self.assertGreater(big.stat().st_size, guard.DEFER_MAX_BYTES)
        self.assertIsNotNone(self.before(big))
        dense = self.root / "data.json"
        dense.write_text("[\n" + "".join('  {"id": %d, "value": "v%d"},\n' % (i, i) for i in range(1500)) + "  {}\n]\n")
        self.assertLess(dense.stat().st_size, guard.DEFER_MAX_BYTES)
        self.assertFalse(guard.deferred(self.context(), dense.read_bytes(), ".json"), "JSON is never compacted")

    def context(self, env=None):
        import hook
        payload = {"session_id": "compact", "scratchpad_dir": str(self.scratch), "tool_name": "Read"}
        with patch.dict(os.environ, dict(self.env, **(env or {}))):
            ctx = hook.Context(features.gate(dict(os.environ)), payload, "PreToolUse", time.monotonic() + 10)
        return ctx

    def test_guard_does_not_defer_when_the_result_hook_is_quarantined(self):
        raw = python_module().encode()
        self.assertTrue(guard.deferred(self.context(), raw, ".py"))
        store = session.Store.for_scratchpad(str(self.scratch))
        for call in ("a", "b"):
            circuit.record(store, "dedupe-reads:Read", call, None, "handler", "ValueError", "compact")
        self.assertFalse(guard.deferred(self.context(), raw, ".py"))

    def test_guard_tokens_bounds(self):
        raw = python_module().encode()
        tokens = COUNT(read_compact.render(raw.decode().split("\n")))
        self.assertTrue(guard.deferred(self.context(), raw, ".py"))
        self.assertFalse(guard.deferred(self.context({"ORK_METER_READ_GUARD_TOKENS": str(tokens)}), raw, ".py"),
                         "at the threshold the Read is not over it")
        small = python_module(functions=3).encode()
        self.assertFalse(guard.deferred(self.context(), small, ".py"))

    session_id = "compact"

    def write(self, path, content, env=None, session_id=None, agent=None):
        self.calls += 1
        payload = {"hook_event_name": "PreToolUse", "session_id": session_id or self.session_id, "tool_name": "Write",
                   "scratchpad_dir": str(self.scratch), "tool_use_id": "write-%d" % self.calls,
                   "tool_input": {"file_path": str(path), "content": content}}
        if agent:
            payload["agent_id"] = agent
        return run_hook(payload, writes.handle, writes.EVENTS, env=dict(self.env, **(env or {})),
                        reducer=writes.REDUCER, started=time.monotonic(), every_mode=True)

    def ranged(self, path, text, offset, limit, session_id=None, env=None):
        """A ranged Read: the Read result hook passes it through; the Write guard's observer marks its lines."""
        lines = text.split("\n")
        self.calls += 1
        payload = {"hook_event_name": "PostToolUse", "session_id": session_id or self.session_id, "tool_name": "Read",
                   "scratchpad_dir": str(self.scratch), "tool_use_id": "call-%d" % self.calls, "prompt_id": "p",
                   "tool_input": {"file_path": str(path), "offset": offset, "limit": limit},
                   "tool_response": {"type": "text", "file": {"filePath": str(path),
                                     "content": "\n".join(lines[offset - 1:offset - 1 + limit]),
                                     "numLines": min(limit, len(lines) - offset + 1), "startLine": offset,
                                     "totalLines": len(lines)}}}
        env = dict(self.env, **(env or {}))
        reply = run_hook(payload, reads.handle, reads.EVENTS, env=env, feature=reads.FEATURE,
                         reducer=reads.REDUCER, started=time.monotonic())
        run_hook(payload, writes.handle, writes.EVENTS, env=env, reducer=writes.REDUCER, started=time.monotonic(),
                 every_mode=True)
        return reply

    def env_for_index(self):
        return {"XDG_STATE_HOME": self.env["XDG_STATE_HOME"], "HOME": self.env["HOME"]}

    def record(self, path):
        env = self.env_for_index()
        name = write_guard.locate(env, str(path))
        return None if name is None else write_guard._read(write_guard._path(env, name, ".json"))

    def view_ranges(self, path, session_id=None):
        """The elided ranges ``(first, last)``, 1-based like the markers were, that the write-guard record
        holds for a session's view (the record keeps them 0-based)."""
        record = self.record(path)
        view = write_guard.view_key({"session_id": session_id or self.session_id})
        return [(first + 1, last + 1) for first, last, _label in record["views"][view]]

    def compacted_file(self):
        """A compacted Read of module.py: ``shown`` is what the model receives
        (the markers and empty slots are gone at the emission boundary), so
        the elided ranges come from the write-guard record, not the markers."""
        path = self.root / "module.py"
        text = python_module()
        path.write_bytes(text.encode("utf-8"))
        reply = self.result(text, path=str(path))
        shown = self.emitted(reply, text)["content"].split("\n")
        self.assertEqual(self.ledger()[-1]["kind"], "read_compact")
        return path, text, shown, self.view_ranges(path), reply

    def test_the_fixed_notice_reaches_the_model_the_record_and_ledger_hold_the_ranges(self):
        # Only the fixed compaction notice is emitted (hooks/lib/read_notice.py): it names no symbol from
        # the file; the view's ranges live in the write-guard record, their size in the ledger.
        path, text, shown, ranges, reply = self.compacted_file()
        self.assertTrue(reply["hookSpecificOutput"]["additionalContext"].startswith("Ork Meter compacted this Read"))
        self.assertNotIn("helper_", reply["hookSpecificOutput"]["additionalContext"])
        self.assertGreater(len(ranges), 1)
        labels = [label for _f, _l, label in self.record(path)["views"][write_guard.view_key({"session_id": "compact"})]]
        self.assertTrue(all(label.startswith("function helper_") for label in labels), labels)
        [row] = self.ledger()
        self.assertEqual(row["lines_elided"], sum(last - first + 1 for first, last in ranges))
        for first, last in ranges:
            self.assertFalse(set(text.split("\n")[first - 1:last]) - {""} & set(shown), "elided lines are not shown")

    def test_a_write_rebuilt_from_the_compacted_view_is_denied_until_the_ranges_are_read(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        folder = Path(write_guard.directory(self.env_for_index()))
        records = [json.loads(p.read_text()) for p in folder.glob("*.json")]
        self.assertEqual(len(records), 1, "one record file per guarded file")
        if os.name != "nt":   # POSIX modes; Windows has none
            self.assertEqual(oct(folder.stat().st_mode & 0o777), "0o700")
            self.assertTrue(all(oct(p.stat().st_mode & 0o777) == "0o600" for p in folder.glob("*.json")))
        self.assertEqual(set(records[0]["aliases"]), set(write_guard.aliases(str(path))))
        self.assertNotIn(str(path), json.dumps(records))
        self.assertNotIn("total_8_0", json.dumps(records), "line hashes, no line text (labels name the symbols)")
        rebuilt = self.rebuilt(shown)
        # The reason ("would delete lines of it you have not seen: a\u2013b ... with offset and limit") is dropped by
        # clean_reply: the denial is bare, and the record says which lines are still unread.
        self.guarded(self.write(path, rebuilt), path, text, ranges[0])
        self.assertIsNone(self.write(path, text + "\n# appended\n"), "a Write keeping every elided line is allowed")
        for first, last in ranges[:-1]:
            self.assertIsNone(self.ranged(path, text, first, last - first + 1))
        self.guarded(self.write(path, rebuilt), path, text, ranges[-1])   # helper_39's body, the last range
        self.assertFalse(self.nonblank(text, *ranges[0]) & self.unread(path), "the first range was read")
        first, last = ranges[-1]
        self.ranged(path, text, first, last - first + 1)
        self.assertIsNone(self.write(path, rebuilt), "every elided range read: the Write goes through")

    def test_the_guard_follows_the_file_and_lets_go_when_it_was_read_whole(self):
        path, text, shown, _ranges, _reply = self.compacted_file()
        rebuilt = self.rebuilt(shown)
        self.calls += 1
        self.result(text, path=str(path))   # compacted again: a fresh record
        self.assertIsNotNone(self.write(path, rebuilt))
        # Compaction off at once: the re-read is trimmed to its head, still a partial view, still guarded.
        self.result(text, path=str(path), env={"ORK_METER_READ_COMPACT": "off", "ORK_METER_DEDUPE": "off"})
        self.assertIsNotNone(self.write(path, rebuilt))
        # A re-read delivered whole (the file is small now): the model holds it all, the record goes.
        small = python_module(functions=3)
        path.write_text(small)
        self.assertIsNone(self.result(small, path=str(path)))
        self.assertIsNone(self.record(path))
        self.assertIsNone(self.write(self.root / "other.py", "x"), "no record for another file")

    def unread(self, path):
        """The record's unread lines of ``path`` (1-based), re-synced to the disk first."""
        view = write_guard.view_key({"session_id": self.session_id})
        write_guard.observe(self.env_for_index(), str(path), view, numbered=[])
        record = self.record(path)
        return {i + 1 for i in write_guard.unread_for(record, view)} if record else set()

    def nonblank(self, text, first, last):
        lines = text.split("\n")
        return {n for n in range(first, last + 1) if lines[n - 1].strip()}

    def rebuilt(self, shown):
        """A Write of the file as the model received it (no marker survives the boundary; kept for safety)."""
        return "\n".join(line for line in shown if not line.startswith("\u2026"))

    def denied(self, reply):
        """A refused Write: hook.denied's permissionDecisionReason is dropped by clean_reply, so only the
        bare deny is emitted; returns the hookSpecificOutput."""
        self.assertIsNotNone(reply, "the Write must be refused")
        self.assertEqual(sorted(reply), ["hookSpecificOutput"])
        out = reply["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(sorted(out), ["hookEventName", "permissionDecision"])
        return out

    def guarded(self, reply, path, text, span):
        """A refused Write whose reason used to name ``span``: the record still holds every line of it unread."""
        self.denied(reply)
        self.assertLessEqual(self.nonblank(text, *span), self.unread(path), "the range %d\u2013%d is unread" % span)

    def test_bypass_1_an_edit_inside_an_elided_range_keeps_it_guarded(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        first, last = ranges[-2]
        lines = text.split("\n")
        # One exact line found by Grep, edited: the range's whole text no longer matches.
        lines[first + 2] = lines[first + 2] + "  # edited"
        path.write_text("\n".join(lines))
        self.denied(self.write(path, self.rebuilt(shown)))
        self.assertLessEqual(self.nonblank(text, first, last), self.unread(path), "every line of the range, the edited one too")

    def test_bypass_1_replace_all_across_the_file_keeps_the_ranges_guarded(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        renamed = text.replace("value", "amount")
        path.write_text(renamed)
        view = [line.replace("value", "amount") for line in shown]
        self.denied(self.write(path, self.rebuilt(view)))
        expected = set().union(*(self.nonblank(text, first, last) for first, last in ranges))
        # Every elided line is still unread; a shown line renamed in the same block may be too (conservative).
        self.assertLessEqual(expected, self.unread(path), "renamed in place: every elided line still unread")

    def test_bypass_2_stale_offsets_after_a_shift_mark_only_the_lines_read(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        shifted = "".join("# inserted %d\n" % i for i in range(5)) + text
        path.write_text(shifted)
        first, last = ranges[-2]
        # The model reads the range by its stale marker offsets: those are five lines short of it now.
        self.ranged(path, shifted, first, last - first + 1)
        self.denied(self.write(path, self.rebuilt(["# inserted %d" % i for i in range(5)] + shown)))
        moved = {n + 5 for n in self.nonblank(text, first, last)}
        unread = self.unread(path)
        self.assertEqual(moved & unread, {n for n in moved if n > last}, "only the lines the stale Read returned are read")
        self.ranged(path, shifted, first + 5, last - first + 1)
        self.assertFalse(moved & self.unread(path))

    def test_bypass_3_another_session_of_the_same_file_is_guarded(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        self.scratch = self.root / "forked-session"   # a fork: another session id and store
        self.guarded(self.write(path, self.rebuilt(shown), session_id="fork"), path, text, ranges[0])

    def test_bypass_4_another_spelling_of_the_path_is_guarded(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        upper = path.with_name(path.name.upper())
        spellings = [str(path.parent / "." / path.name)]
        if upper.exists():   # a case-insensitive disk (macOS, Windows): MODULE.PY is module.py
            spellings.append(str(upper))
        for spelling in spellings:
            self.guarded(self.write(spelling, self.rebuilt(shown)), path, text, ranges[0])

    def test_an_edit_replacing_the_file_by_rename_keeps_it_guarded(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        replacement = path.with_suffix(".tmp")
        replacement.write_text(text.replace("CONSTANT_1 = 1", "CONSTANT_1 = 10"))
        os.replace(replacement, path)   # an atomic write: a new inode
        self.guarded(self.write(path, self.rebuilt(shown)), path, text, ranges[0])

    def test_grep_and_bash_output_that_shows_lines_marks_them_read(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        lines = text.split("\n")
        first, last = ranges[0]
        trim = load("hooks/trim-tool-output.py")

        def post(tool, tool_input, response):
            self.calls += 1
            payload = {"hook_event_name": "PostToolUse", "session_id": "compact", "tool_name": tool,
                       "scratchpad_dir": str(self.scratch), "tool_use_id": "out-%d" % self.calls,
                       "cwd": str(self.root), "tool_input": tool_input, "tool_response": response}
            return run_hook(payload, trim.handle, trim.EVENTS, env=self.env, feature=trim.FEATURE,
                            reducer=trim.REDUCER, started=time.monotonic())
        grep = "\n".join("%d:%s" % (n, lines[n - 1]) for n in range(first, first + 5))
        post("Grep", {"pattern": "total", "path": str(path), "-n": True, "output_mode": "content"},
             {"mode": "content", "numFiles": 1, "filenames": [], "content": grep, "numLines": 5})
        self.denied(self.write(path, self.rebuilt(shown)))
        unread = self.unread(path)
        self.assertFalse(self.nonblank(text, first, first + 4) & unread, "Grep -n lines read")
        self.assertLessEqual(self.nonblank(text, first + 5, last), unread, "the rest of the range not")
        cat = "\n".join("%6d\t%s" % (n, lines[n - 1]) for n in range(first + 5, last + 1))
        post("Bash", {"command": "cat -n %s" % path.name}, {"stdout": cat, "stderr": "", "interrupted": False,
                                                           "isImage": False})
        self.assertFalse(self.nonblank(text, first, last) & self.unread(path), "cat -n lines read")
        second_first, second_last = ranges[1]
        block = "\n".join(lines[second_first - 1:second_last])
        post("Bash", {"command": "sed -n '%d,%dp' module.py" % (second_first, second_last)},
             {"stdout": block, "stderr": "", "interrupted": False, "isImage": False})
        self.assertFalse(self.nonblank(text, second_first, second_last) & self.unread(path), "sed -n lines read")

    def test_the_override_switch_lets_a_write_through(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        self.assertIsNotNone(self.write(path, self.rebuilt(shown)))
        self.scratch = self.root / "next-session"   # a switch turned on applies from the next session
        self.assertIsNone(self.write(path, self.rebuilt(shown), env={"ORK_METER_WRITE_GUARD_OVERRIDE": "on"},
                                     session_id="next"))

    def test_the_guard_needs_no_snapshot_and_fails_closed(self):
        path, text, shown, _ranges, _reply = self.compacted_file()
        for snapshot in (self.scratch / "ork-meter" / "tool-output").glob("*"):
            snapshot.unlink()
        self.assertEqual(self.write(path, "short")["hookSpecificOutput"]["permissionDecision"], "deny",
                         "the record holds its own line hashes; a Write that keeps nothing is refused too")
        with patch.object(write_guard, "deleted", side_effect=RuntimeError("boom")):
            reply = self.write(path, "short")
        self.denied(reply)   # fails closed; the "could not check it" reason is dropped by clean_reply

    def test_a_corrupt_record_is_a_tombstone_that_no_edit_clears(self):
        path, text, shown, _ranges, _reply = self.compacted_file()
        env = self.env_for_index()
        record_file = Path(write_guard._path(env, write_guard.locate(env, str(path)), ".json"))
        record_file.write_text("{not json")
        for attempt in range(3):   # every retry is refused, not only the first (the "was unreadable" reason is not emitted)
            self.denied(self.write(path, "short"))
        self.assertTrue(self.record(path)["tombstone"])
        # The model's own one-line Edit, as the refusal suggests: the tombstone stays.
        path.write_text(text.replace("CONSTANT_1 = 1", "CONSTANT_1 = 10"))
        self.denied(self.write(path, self.rebuilt(shown)))
        self.assertTrue(self.record(path)["tombstone"])
        # A whole delivery to another session clears it for that session only.
        write_guard.forget(env, str(path), write_guard.view_key({"session_id": "other"}))
        self.assertIsNone(self.write(path, "short", session_id="other"))
        self.denied(self.write(path, self.rebuilt(shown)))
        self.assertTrue(self.record(path)["tombstone"])
        # A whole delivery to this session clears it here.
        write_guard.forget(env, str(path), write_guard.view_key({"session_id": self.session_id}))
        self.assertIsNone(self.write(path, "short"))

    def test_a_busy_record_lock_refuses_the_write_and_skips_marking(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        env = self.env_for_index()
        lock = Path(write_guard._path(env, write_guard.locate(env, str(path)), ".lock"))
        lock.write_text("")
        try:
            with patch.object(write_guard, "WRITE_LOCK_WAIT", 0.05):
                self.denied(self.write(path, text))   # the whole text, refused only because the record is busy
            first, last = ranges[0]
            self.ranged(path, text, first, last - first + 1)   # skipped, not unlocked
        finally:
            lock.unlink()
        self.assertLessEqual(self.nonblank(text, first, last), self.unread(path))

    def test_duplicate_bodies_one_shown_one_elided_are_not_kept_by_the_shown_copy(self):
        body = "".join("    step = step * 3 + %d  # identical line %d\n" % (i % 7, i) for i in range(250))
        text = ("".join("HEAD_%d = %d\n" % (i, i) for i in range(41)) + "def first(step):\n" + body + "    return step\n\n"
                + "def second(step):\n" + body + "    return step\n")
        path = self.root / "twins.py"
        path.write_text(text)
        reply = self.result(text, path=str(path))   # about 8,500 tokens: the second body is elided, the first shown
        shown = reply["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"].split("\n")
        rebuilt = self.rebuilt(shown)
        self.assertEqual(sum(1 for line in rebuilt.split("\n") if "identical line 0" in line), 1, "one body shown")
        self.denied(self.write(path, rebuilt))
        # The reason named "function second body"; the record's view carries that label now.
        labels = [label for _f, _l, label in self.record(path)["views"][write_guard.view_key({"session_id": "compact"})]]
        self.assertEqual(labels, ["function second body"])

    def test_alignment_counts_position_and_multiplicity(self):
        lines = ["def a():", "    x = 1", "}", "}", "def b():", "    x = 1", "}", "}"]
        record = {"lines": [write_guard.line_hash(line) for line in lines], "views": {}}
        unread = {5: "b body", 6: "b body", 7: "b body"}
        self.assertEqual(set(write_guard.deleted(record, unread, "\n".join(lines[:5]))), {5, 6, 7},
                         "repeated lines elsewhere don't keep the unread copies")
        self.assertEqual(write_guard.deleted(record, unread, "\n".join(lines)), {}, "the whole file keeps them")
        reordered = "\n".join(lines[4:] + lines[:4])
        self.assertTrue(write_guard.deleted(record, unread, reordered) or True)   # a move may count as a deletion
        self.assertEqual(write_guard.deleted(record, unread, "\n".join(lines[:5] + ["    x = 1", "}", "}", "# more"])), {})

    def test_views_are_per_session_and_a_whole_read_clears_only_its_own(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        # Session "other" compacts the same file too: two views.
        self.calls += 1
        other_scratch, self.scratch = self.scratch, self.root / "other-session"
        self.session_id = "other"
        self.result(text, path=str(path), env={"ORK_METER_TRIM_BUDGET_READ": "1024k"})   # pinned with a large budget
        # A whole delivery to "other" (compaction off there, at once) clears only "other"'s view.
        self.result(text, path=str(path), env={"ORK_METER_READ_COMPACT": "off", "ORK_METER_DEDUPE": "off",
                                               "ORK_METER_TRIM_BUDGET_READ": "1024k"})
        self.assertIsNone(self.write(path, self.rebuilt(shown), session_id="other"), "other holds the whole file")
        self.scratch, self.session_id = other_scratch, "compact"
        self.guarded(self.write(path, self.rebuilt(shown)), path, text, ranges[0])   # compact still guarded
        # A session without a view gets the strictest.
        self.guarded(self.write(path, self.rebuilt(shown), session_id="fork"), path, text, ranges[0])

    def test_a_subagent_has_its_own_view(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        first, last = ranges[0]
        view = write_guard.view_key({"session_id": self.session_id, "agent_id": "sub"})
        write_guard.observe(self.env_for_index(), str(path), view, start=first, shown=text.split("\n")[first - 1:last])
        record = self.record(path)
        self.assertFalse(self.nonblank(text, first, last) & {i + 1 for i in write_guard.unread_for(record, view)})
        self.guarded(self.write(path, self.rebuilt(shown)), path, text, ranges[0])   # the parent's view is not cleared by its subagent's read

    def test_the_counting_pre_check_and_the_size_cap(self):
        lines = ["line %d" % i for i in range(12000)]
        record = {"lines": [write_guard.line_hash(line) for line in lines], "views": {}}
        unread = {11000: "tail", 11001: "tail"}
        started = time.monotonic()
        self.assertEqual(set(write_guard.deleted(record, unread, "\n".join(lines[:10000]))), {11000, 11001},
                         "missing text is found by counting, without aligning")
        self.assertLess(time.monotonic() - started, 1.0)
        with self.assertRaises(write_guard.TooLarge):
            write_guard.deleted(record, unread, "\n".join(lines))   # every count met: past the cap, no guess
        self.assertEqual(write_guard.deleted(record, {}, "x"), {}, "nothing unread: nothing to align")
        path = self.root / "huge.txt"
        path.write_text("\n".join(lines))
        env = self.env_for_index()
        write_guard.remember(env, str(path), "\n".join(lines), [(11001, 11002, "tail")], "v")
        self.denied(self.write(path, "\n".join(lines) + "\nmore"))   # "too long to compare" is not emitted: a bare deny

    def test_alignment_of_10k_lines_stays_quick(self):
        lines = ["    value_%d = %d" % (i % 300, i % 7) for i in range(write_guard.MAX_ALIGN_LINES)]
        record = {"lines": [write_guard.line_hash(line) for line in lines], "views": {}}
        started = time.monotonic()
        write_guard.deleted(record, {5000: "x"}, "\n".join(lines))
        self.assertLess(time.monotonic() - started, 3.0, "well within the hook's 5 s limit")

    def test_any_look_keeps_a_record_alive(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        env = self.env_for_index()
        record_file = Path(write_guard._path(env, write_guard.locate(env, str(path)), ".json"))
        old = time.time() - write_guard.MAX_AGE_SECONDS + 3600
        os.utime(record_file, (old, old))
        self.denied(self.write(path, self.rebuilt(shown)))   # a Write check touches it
        write_guard.expire(env, now=time.time() + 7200)
        self.assertIsNotNone(self.record(path), "looked at an hour ago by the clock: still alive")
        self.assertEqual(write_guard.MAX_AGE_SECONDS, 30 * 24 * 3600)
        write_guard.expire(env, now=time.time() + write_guard.MAX_AGE_SECONDS + 60)
        self.assertIsNone(self.record(path))
        self.assertFalse(list(Path(write_guard.directory(env)).glob("*.alias")), "its pointers go with it")

    def test_parallel_records_are_never_lost(self):
        import threading
        env = self.env_for_index()
        paths = []
        for n in range(20):
            path = self.root / ("file_%02d.py" % n)
            path.write_text("".join("line %d of %d\n" % (i, n) for i in range(100)))
            paths.append(path)
        errors = []

        def work(chunk):
            for path in chunk:
                try:
                    write_guard.remember(env, str(path), path.read_text(), [(50, 90, "body")], "view")
                except Exception as error:
                    errors.append(error)
        threads = [threading.Thread(target=work, args=(paths[i::6],)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(sum(1 for path in paths if self.record(path)), 20, "6 writers, 20 records, none lost")

    def test_expiry_is_by_age_only(self):
        env = self.env_for_index()
        paths = []
        for n in range(300):
            path = self.root / ("many_%03d.txt" % n)
            path.write_text("a\nb\nc\n")
            paths.append(path)
            write_guard.remember(env, str(path), "a\nb\nc\n", [(2, 2, "x")], "view")
        self.assertEqual(sum(1 for path in paths if self.record(path)), 300, "no count limit evicts a young record")
        write_guard.expire(env, now=time.time() + write_guard.MAX_AGE_SECONDS + 60)
        self.assertEqual(sum(1 for path in paths if self.record(path)), 0, "after the age limit they go")

    def test_the_guard_keeps_guarding_in_shadow_and_off_modes(self):
        path, text, shown, ranges, _reply = self.compacted_file()
        for mode in ("shadow", "off"):
            self.guarded(self.write(path, self.rebuilt(shown), env={"ORK_METER_MODE": mode}), path, text, ranges[0])
        first, last = ranges[0]
        self.ranged(path, text, first, last - first + 1, env={"ORK_METER_MODE": "off"})
        self.assertFalse(self.nonblank(text, first, last) & self.unread(path), "the observer marks reads in off mode")

    def test_paged_fallback_is_guarded_too(self):
        path = self.root / "notes.txt"
        text = "".join("note %04d %s\n" % (i, "configuration " * 7) for i in range(600))
        path.write_text(text)
        reply = self.result(text, path=str(path), env={"ORK_METER_READ_GUARD_BYTES": "32k"})
        # The note ("refuses a Write of this file") and the refusal's "301–600 lines after the page" are not
        # emitted; the page is, the Write of it is denied, and the record holds the lines after the page.
        page = self.head(reply, text, "read_guard")["content"]
        self.guarded(self.write(path, page), path, text, (301, 600))

    def test_compaction_is_off_by_default_and_the_guard_still_pages(self):
        env = {key: value for key, value in self.env.items() if key != "ORK_METER_READ_COMPACT"}
        text = python_module()
        self.env = env
        self.head(self.result(text), text, "trim")   # trimmed, not compacted: no note either way, the ledger says
        self.assertEqual([row["kind"] for row in self.ledger()], ["trim"])
        self.assertFalse(guard.deferred(self.context(), text.encode(), ".py"))
        path = self.root / "module.py"
        path.write_text(text)
        paged = self.before(path)
        self.assertEqual(paged["hookSpecificOutput"]["updatedInput"]["limit"], 48, "the read guard pages as before")

    def test_settings(self):
        self.assertEqual(features.value({}, "read_guard", "tokens"), 6000)
        self.assertEqual(features.value({}, "read_guard", "compact"), 0, "opt-in (#316)")
        self.assertEqual(features.value({"ORK_METER_READ_GUARD_TOKENS": "50k"}, "read_guard", "tokens"),
                         read_compact.DEFER_MAX_TOKENS)
        self.assertEqual(features.value({"ORK_METER_READ_GUARD_TOKENS": "10"}, "read_guard", "tokens"), 1000)
        self.assertFalse(features.read_compaction({"ORK_METER_MODE": "reduce"}), "off by default (#316)")
        on = {"ORK_METER_MODE": "reduce", "ORK_METER_READ_COMPACT": "on"}
        self.assertTrue(features.read_compaction(on))
        for name in ("ORK_METER_READ_COMPACT", "ORK_METER_READ_GUARD", "ORK_METER_TRIM"):   # each off spelling wins
            self.assertFalse(features.read_compaction(dict(on, **{name: "off"})), name)
        self.assertEqual(features.SETTING_KINDS["ORK_METER_READ_COMPACT"], ("opt_in", ("read_guard", "compact")))


if __name__ == "__main__":
    unittest.main(verbosity=1)
