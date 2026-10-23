"""Workspace search (#107): ranked exact snippets, honest bounds, and reads
confined to non-ignored, non-secret regular files inside the workspace."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, load  # noqa: E402
import recovery  # noqa: E402

source = load("scripts/source.py")


class SourceSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.workspace = self.directory / "task"
        self.workspace.mkdir()

    def save(self, path, raw):
        target = self.workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
        return target

    def search(self, query, **options):
        output = source.search_workspace(self.workspace, query, **options)
        self.assertLessEqual(len(source._serialized(output).encode("utf-8")), output["budget_bytes"])
        return output

    def paths(self, output):
        return [entry["path"] for entry in output["results"]]

    def symlink(self, link, target, directory=False):
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symlinks unavailable: %s" % exc)

    def test_literal_hit_has_hash_exact_context_and_counts(self):
        raw = b"one\ntwo token\nthree\nfour\n"
        self.save("a.txt", raw)
        output = self.search("token")
        self.assertTrue(output["complete"], output)
        self.assertEqual((output["files_scanned"], output["files_matched"], output["matches_total"],
                          output["matches_returned"], output["matches_omitted"]), (1, 1, 1, 1, 0))
        entry = output["results"][0]
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(entry["hits"], [2])
        self.assertEqual(entry["lines"], [{"line": 1, "text": "one\n"}, {"line": 2, "text": "two token\n"},
                                          {"line": 3, "text": "three\n"}])
        self.assertEqual(output["mode"], "literal")
        # Literal: regex metacharacters are plain text.
        self.save("b.txt", "a.c\nabc\n")
        self.assertEqual(self.search("a.c", context=0)["results"][0]["hits"], [1])

    def test_regex_and_ignore_case(self):
        self.save("a.txt", "Alpha 12\nbeta 7\nALPHA x\n")
        output = self.search(r"[a-z]+ \d+", regex=True, context=0)
        self.assertEqual(output["results"][0]["hits"], [1, 2])
        self.assertEqual(output["mode"], "regex")
        self.assertEqual(self.search("alpha", context=0)["files_matched"], 0)
        self.assertEqual(self.search("alpha", ignore_case=True, context=0)["results"][0]["hits"], [1, 3])
        with self.assertRaisesRegex(ValueError, "invalid regular expression"):
            self.search("(unclosed", regex=True)
        for query in ("", "x" * 257):
            with self.assertRaises(ValueError):
                self.search(query)

    def test_glob_filter_counts_excluded_files(self):
        self.save("src/app.py", "needle\n")
        self.save("src/deep/mod.py", "needle\n")
        self.save("docs/readme.md", "needle\n")
        self.assertEqual(sorted(self.paths(self.search("needle", globs=["*.py"]))), ["src/app.py", "src/deep/mod.py"])
        output = self.search("needle", globs=["src/**"])
        self.assertEqual(sorted(self.paths(output)), ["src/app.py", "src/deep/mod.py"])
        self.assertEqual(output["files_skipped"]["glob"], 1)
        self.assertEqual(self.paths(self.search("needle", globs=["*.md", "src/app.py"])), ["docs/readme.md", "src/app.py"])

    def test_overlapping_context_is_merged(self):
        self.save("a.txt", "".join("line %d%s\n" % (n, " hit" if n in (3, 5, 12) else "") for n in range(1, 16)))
        entry = self.search("hit", context=2)["results"][0]
        self.assertEqual([row["line"] for row in entry["lines"]], [1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 14])
        self.assertEqual(entry["hits"], [3, 5, 12])
        with self.assertRaises(ValueError):
            self.search("hit", context=6)

    def test_definition_outranks_mentions_and_ties_sort_by_path(self):
        self.save("z_def.py", "def frobnicate(x):\n    return x\n")
        self.save("a_uses.py", "frobnicate(1)\nfrobnicate(2)\nfrobnicate(3)\n")
        self.save("b_uses.py", "y = frobnicate(1)\n")
        self.save("c_uses.py", "y = frobnicate(1)\n")
        self.save("lib/frobnicate.txt", "about frobnicate\n")
        output = self.search("frobnicate")
        self.assertEqual(self.paths(output)[0], "z_def.py")
        self.assertEqual(output["results"][0]["definition_hits"], 1)
        self.assertEqual(self.paths(output)[1], "lib/frobnicate.txt", "path match beats mention count")
        self.assertEqual(self.paths(output)[2:], ["a_uses.py", "b_uses.py", "c_uses.py"])
        self.assertEqual(self.paths(output), self.paths(self.search("frobnicate")), "deterministic")
        for line in ("class Widget:", "  async def Widget(", "export default function Widget(", "pub(crate) fn Widget()",
                     "func (r *T) Widget() {", "const Widget = 1", "Widget: int = 3", "Widget := 4", "type Widget struct"):
            with self.subTest(line=line):
                match = re.search("Widget", line)
                self.assertTrue(source._definition(line, match))
        for line in ("Widget == 3", "call(Widget)", "def WidgetFactory():", "x = Widget", "Widget => 1"):
            with self.subTest(line=line):
                match = re.search("Widget", line)
                self.assertFalse(source._definition(line, match))

    def test_identical_snippets_are_shown_once_with_locations(self):
        for name in ("a.py", "b.py", "c.py"):
            self.save(name, "import shared_helper\n")
        self.save("d.py", "x = 1\nimport shared_helper\n")
        output = self.search("shared_helper", context=0)
        self.assertEqual(output["files_returned"], 4)
        first, *rest = output["results"]
        self.assertEqual(first["path"], "a.py")
        self.assertEqual(first["repeated"], [{"line": 1, "also_at": ["b.py:1", "c.py:1", "d.py:2"]}])
        for entry in rest:
            self.assertEqual(entry["lines"], [])
            self.assertEqual(entry["deduplicated_lines"], 1)
        self.assertEqual((output["matches_returned"], output["matches_deduplicated"], output["matches_omitted"]), (1, 3, 0))
        self.assertTrue(output["complete"])
        for index in range(source.MAX_ALSO_AT + 2):
            self.save("many/m%02d.py" % index, "import shared_helper\n")
        output = self.search("shared_helper", context=0, budget=64 * 1024)
        record = output["results"][0]["repeated"][0]
        self.assertEqual(len(record["also_at"]), source.MAX_ALSO_AT)
        self.assertEqual(record["more"], 3 + source.MAX_ALSO_AT + 2 - source.MAX_ALSO_AT)
        # Past the also_at cap each later file still names where its snippet is.
        for entry in output["results"][1:]:
            self.assertEqual(entry["same_as"], ["a.py:1"])

    def test_budget_withholds_whole_files_with_honest_counts(self):
        for index in range(40):
            self.save("f%02d.txt" % index, "needle %02d %s\n" % (index, "x" * 300))
        output = self.search("needle", budget=4096, context=0)
        self.assertFalse(output["complete"])
        self.assertIn("output_budget", output["incomplete_reasons"])
        returned = [entry for entry in output["results"] if entry["status"] == "ok"]
        stubs = [entry for entry in output["results"] if entry["status"] == "output_budget_exceeded"]
        self.assertTrue(returned and stubs)
        for entry in returned:
            self.assertEqual(entry["lines"][0]["text"], (self.workspace / entry["path"]).read_text(), "never cut")
        for stub in stubs:
            self.assertNotIn("lines", stub)
            self.assertEqual(len(stub["sha256"]), 64)
        self.assertEqual(output["files_returned"], len(returned))
        self.assertEqual(output["files_withheld"], len(stubs))
        self.assertEqual(output["files_returned"] + output["files_withheld"] + output["files_omitted"], 40)
        self.assertEqual(output["matches_returned"] + output["matches_omitted"], 40)
        with self.assertRaises(ValueError):
            self.search("needle", budget=512)
        with self.assertRaises(ValueError):
            self.search("needle", budget=64 * 1024 + 1)
        self.assertEqual(self.search("needle", budget=64 * 1024)["budget_bytes"], 64 * 1024)

    def test_per_file_cap_keeps_definitions_and_reports_omissions(self):
        self.save("a.py", "".join("use(item)\n" for _ in range(20)) + "item = 3\n")
        output = self.search("item", per_file=3, context=0)
        entry = output["results"][0]
        self.assertEqual(entry["hits"], [1, 2, 21])
        self.assertEqual((entry["matching_lines"], entry["returned_matching_lines"], entry["omitted_matching_lines"]),
                         (21, 3, 18))
        self.assertEqual(output["matches_omitted"], 18)
        self.assertIn("per_file_cap", output["incomplete_reasons"])
        self.assertFalse(output["complete"])

    def test_binary_oversized_and_undecodable_files_are_counted_not_searched(self):
        self.save("blob.dat", b"needle\0binary")
        self.save("big.txt", b"needle\n" + b"x" * source.MAX_SEARCH_FILE_BYTES)
        self.save("latin.txt", "needle caf\xe9\n".encode("latin-1"))
        self.save("ok.txt", "needle\n")
        output = self.search("needle")
        self.assertEqual(self.paths(output), ["ok.txt"])
        skipped = output["files_skipped"]
        self.assertEqual((skipped["binary"], skipped["oversized"], skipped["undecodable"]), (1, 1, 1))
        self.assertEqual(output["files_scanned"], 1)

    def test_ignored_vendored_and_secret_files_are_never_opened(self):
        self.save(".gitignore", "ignored_dir/\n*.log\n")
        self.save("ignored_dir/a.txt", "needle\n")
        self.save("debug.log", "needle\n")
        self.save("node_modules/pkg/index.js", "needle\n")
        self.save("vendor/lib.py", "needle\n")
        self.save(".env", "needle=secret\n")
        self.save("config/.env.local", "needle\n")
        self.save("keys/server.pem", "needle\n")
        self.save("my_credentials.json", "needle\n")
        self.save("app.py", "needle\n")
        opened = []
        real_open = os.open
        def recording_open(path, *args, **kwargs):
            opened.append(os.fspath(path))
            return real_open(path, *args, **kwargs)
        with patch("os.open", recording_open):
            output = self.search("needle")
        self.assertEqual(self.paths(output), ["app.py"])
        self.assertTrue([path for path in opened if path.endswith("app.py")], "the recorder sees reads")
        for name in ("a.txt", "debug.log", "index.js", "lib.py", ".env", ".env.local", "server.pem", "my_credentials.json"):
            self.assertFalse([path for path in opened if path.endswith(name)], name)
        skipped = output["files_skipped"]
        self.assertGreaterEqual(skipped["ignored"], 2)
        self.assertGreaterEqual(skipped["vendored_or_generated"], 2)
        self.assertEqual(skipped["secret_looking"], 4)

    def test_symlinks_are_not_followed(self):
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("needle outside\n")
        self.symlink(self.workspace / "linked.txt", outside / "secret.txt")
        self.symlink(self.workspace / "linked_dir", outside, directory=True)
        self.save("inside.txt", "needle inside\n")
        output = self.search("needle")
        self.assertEqual(self.paths(output), ["inside.txt"])
        self.assertEqual(output["files_skipped"]["symlinks"], 2)

    def test_workspace_is_resolved_and_must_be_a_directory(self):
        self.save("file.txt", "needle\n")
        with self.assertRaises(ValueError):
            source.search_workspace(self.workspace / "file.txt", "needle")
        with self.assertRaises(OSError):
            source.search_workspace(self.workspace / "missing", "needle")
        (self.directory / "sibling.txt").write_text("needle\n")
        output = source.search_workspace(self.workspace / "." / "..", "needle")
        self.assertEqual(output["workspace"], str(self.directory))
        # A file whose real path leaves the workspace is refused by the reader too.
        self.assertIsNone(source._workspace_rules().read_bytes(str(self.workspace), "../sibling.txt"))
        for entry in self.search("needle")["results"]:
            self.assertFalse(Path(entry["path"]).is_absolute())
            self.assertNotIn("..", Path(entry["path"]).parts)

    def test_clock_limit_stops_with_complete_false(self):
        for index in range(5):
            self.save("f%d.txt" % index, "needle\n")
        # Start, the first file's check and its one line; every later reading
        # is past the limit.
        ticks = iter([0.0, 0.0, 0.0])
        with patch.object(source, "_clock", lambda: next(ticks, 5.0)):
            output = self.search("needle")
        self.assertFalse(output["complete"])
        self.assertIn("time_limit", output["incomplete_reasons"])
        self.assertEqual(output["files_scanned"], 1)
        self.assertEqual(output["files_skipped"]["not_scanned"], 4)
        self.assertEqual(output["files_matched"], 1)
        self.assertIsNone(output["interrupted_file"], "f1.txt was never opened")

    def test_literal_scan_checks_the_clock_on_every_line(self):
        self.save("a.txt", "needle\n" * 50)
        ticks = iter([0.0, 0.0] + [0.0] * 10)
        with patch.object(source, "_clock", lambda: next(ticks, 5.0)):
            output = self.search("needle")
        self.assertEqual((output["files_scanned"], output["files_matched"]), (0, 0))
        self.assertEqual(output["interrupted_file"], "a.txt")
        self.assertEqual(output["files_skipped"]["not_scanned"], 1)
        text = source._pack_search_text(dict(output, files_skipped=dict(output["files_skipped"])), [], ["time_limit"], 4096)
        self.assertIn("incomplete: time_limit; stopped in a.txt;", text)

    def assert_worker_gone(self):
        child = source._last_worker
        self.assertIsNotNone(child)
        self.assertIsNotNone(child.poll(), "the regex child was reaped")
        if os.name != "nt":
            with self.assertRaises(ProcessLookupError):
                os.kill(child.pid, 0)

    def test_pathological_regexes_stop_at_the_deadline_in_a_killed_child(self):
        # Exponential and polynomial backtracking, and quadratic single
        # quantifiers on long lines (#220 reviews: 5 s to over 90 s each).
        self.save("a_fine.txt", "aaa!\n")
        cases = [(r"(a+)+$", "a" * 64 + "b"), (r"\w*\w*\w*\w*\w*\w*!", "a" * 80),
                 (r".*.*.*.*.*.*x", "a" * 80), (r"a*a*a*a*a*a*a*a*b", "a" * 40),
                 (r"\w{0,200}\w{0,200}\w{0,200}!", "a" * 2000), (r"\w+!", "a" * 100000)]
        for pattern, line in cases:
            with self.subTest(pattern=pattern):
                self.save("z_slow.txt", line + "\n")
                started = time.monotonic()
                output = self.search(pattern, regex=True, time_limit=1.0)
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertFalse(output["complete"])
                self.assertIn("time_limit", output["incomplete_reasons"])
                self.assertEqual(output["interrupted_file"], "z_slow.txt")
                self.assertEqual(output["files_skipped"]["not_scanned"], 1)
                self.assertEqual(output["files_scanned"], 1, "results before the deadline are kept")
                self.assert_worker_gone()

    def test_quadratic_regex_on_one_300_kb_json_line_stops_within_the_default_limit(self):
        self.save("data.json", '{"k": "' + "a" * 300000 + '"}\n')
        started = time.monotonic()
        text = self.text(r"\w+\(", regex=True)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0)
        self.assertGreater(elapsed, source.SEARCH_SECONDS - 0.5)
        self.assertTrue(text.startswith('search "\\\\w+\\\\(" regex: 0 files matched of 0 scanned, 0 matches'), text)
        self.assertIn("incomplete: time_limit; stopped in data.json;", text)
        self.assertIn("not_scanned 1", text)
        self.assert_worker_gone()

    def test_everyday_regexes_run_on_every_platform(self):
        self.save("a.txt", "foo then bar then baz\nabc 123\nfoo bar\n")
        output = self.search(r"foo.*bar.*baz", regex=True, context=0)
        self.assertTrue(output["complete"])
        self.assertEqual(output["results"][0]["hits"], [1])
        self.assertEqual(self.search(r"[a-z]+ \d+", regex=True, context=0)["results"][0]["hits"], [2])
        self.assert_worker_gone()
        # A failed child is reported, not mistaken for an empty result.
        with patch.object(source.sys, "executable", str(self.directory / "missing-python")):
            with self.assertRaises(OSError):
                self.search("foo", regex=True)

    def test_crlf_files_keep_exact_text_and_paths_are_posix(self):
        raw = b"first\r\nsecond needle\r\nthird\r\n"
        self.save(Path("sub") / "dir" / "win.txt", raw)
        entry = self.search("needle")["results"][0]
        self.assertEqual(entry["path"], "sub/dir/win.txt")
        self.assertEqual("".join(row["text"] for row in entry["lines"]), raw.decode())
        batch = source.batch([{"path": entry["path"], "start": 2, "limit": 1, "expected_hash": entry["sha256"]}],
                             self.workspace)
        self.assertEqual(batch["results"][0]["result"]["source"], "second needle\r\n")

    def test_cli_defaults_and_argument_rules(self):
        self.save("a.py", "def target():\n    pass\n")
        def run(*extra):
            class Stdout:
                buffer = io.BytesIO()
            with patch.object(sys, "stdout", Stdout):
                code = source.main(["--workspace", str(self.workspace), "--search", "target", "--glob", "*.py"] + list(extra))
            self.assertEqual(code, 0)
            return Stdout.buffer.getvalue().decode("utf-8")
        output = json.loads(run("--json"))
        self.assertEqual((output["budget_bytes"], output["context"], output["per_file"]), (12 * 1024, 1, 8))
        self.assertEqual(output["results"][0]["path"], "a.py")
        text = run()
        self.assertTrue(text.startswith('search "target" literal glob "*.py": 1 files matched of 1 scanned, 1 matches (1 shown) complete\n'))
        self.assertIn("a.py sha256=%s [def]\n1: def target():\n2-     pass\n" % output["results"][0]["sha256"], text)
        for argv in (["--search", "x"], ["a.py", "--workspace", ".", "--search", "x"],
                     ["--workspace", ".", "--search", "x", "--start", "3"], ["a.py", "--regex"],
                     ["--workspace", ".", "--batch", "[]", "--glob", "*.py"], ["a.py", "--json"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    source.main(argv)
                self.assertEqual(raised.exception.code, 2)

    def text(self, query, **options):
        output = source.search_workspace(self.workspace, query, text=True, **options)
        self.assertLessEqual(len(output.encode("utf-8")), options.get("budget", source.SEARCH_BUDGET))
        self.assertTrue(output.endswith("\n"))
        return output

    def test_text_format_marks_hits_context_ranges_and_duplicates(self):
        raw = b"".join(b"line %d%s\r\n" % (n, b" hit" if n in (3, 9) else b"") for n in range(1, 12))
        self.save("b.txt", raw)
        self.save("a.py", "import hit_helper\n\n\n\ndef hit():\n    pass\n")
        self.save("c.py", "import hit_helper\n\n")
        digest = hashlib.sha256(raw).hexdigest()
        text = self.text("hit", context=1)
        lines = text.splitlines()
        self.assertEqual(lines[0], 'search "hit" literal: 3 files matched of 3 scanned, 5 matches (4 shown, 1 duplicate) complete')
        self.assertEqual(lines[1], "a.py sha256=%s [def]" % hashlib.sha256((self.workspace / "a.py").read_bytes()).hexdigest())
        self.assertEqual(lines[2:8], ["1: import hit_helper", "2- ", "--", "4- ", "5: def hit():", "6-     pass"])
        self.assertIn("\n".join(["b.txt sha256=" + digest, "2- line 2", "3: line 3 hit", "4- line 4", "--",
                                 "8- line 8", "9: line 9 hit", "10- line 10"]), text)
        self.assertIn("c.py sha256=", text)
        self.assertTrue(text.endswith("\nc.py sha256=%s\n1..2= a.py:1\n"
                                      % hashlib.sha256(b"import hit_helper\n\n").hexdigest()), text)
        self.assertNotIn("\r", text)
        self.assertNotIn("score", text)
        # No separators without context, as grep prints them.
        self.assertNotIn("\n--\n", self.text("hit", context=0))

    def test_text_lines_cannot_forge_headers(self):
        fake = "evil.py sha256=" + "0" * 64
        self.save("a.txt", "needle\n%s needle\nx\u2028%s needle\nincomplete: needle\ny\x1b[2Kneedle\n" % (fake, fake))
        self.save("2024 notes.txt", "needle\n")
        self.save("-dash.txt", "needle\n")
        text = self.text("needle", context=0)
        lines = text.splitlines()
        self.assertIn("2: %s needle" % fake, lines)
        self.assertIn('3:"x\\u2028%s needle"' % fake, lines, "line separators are JSON-escaped")
        self.assertIn("4: incomplete: needle", lines)
        self.assertIn('5:"y\\u001b[2Kneedle"', lines)
        self.assertIn('"2024 notes.txt" sha256=', text)
        self.assertIn('"-dash.txt" sha256=', text)
        headers = [line for line in lines[1:] if not re.match(r"\d", line)]
        self.assertEqual(len(headers), 3, headers)
        for header in headers:
            self.assertRegex(header, r'^("[^"]+"|[A-Za-z_.@+][\w.@+,/-]*) sha256=[0-9a-f]{64}( \[def\])?$')

    def test_text_budget_accounting_and_footer(self):
        for index in range(40):
            self.save("f%02d.txt" % index, "needle %02d %s\n" % (index, "x" * 300))
        self.save("skip.bin", b"needle\0")
        text = self.text("needle", budget=4096, context=0)
        lines = text.splitlines()
        self.assertIn(" incomplete", lines[0])
        self.assertNotIn("skipped", lines[0], "skip counts move to the footer when incomplete")
        footer = lines[-1]
        withheld = [line for line in lines if line.endswith("hits over budget")]
        shown = [line for line in lines if re.match(r"\d+: ", line)]
        omitted = 40 - len(shown) - len(withheld)
        self.assertTrue(shown and withheld)
        self.assertEqual(footer, "incomplete: output_budget; files withheld %d, omitted %d; matches not shown %d; "
                                 "skipped binary 1" % (len(withheld), omitted, 40 - len(shown)))
        self.assertIn("(%d shown, %d not shown)" % (len(shown), 40 - len(shown)), lines[0])
        for line in shown:
            name = "f%s.txt" % line.split()[2]
            self.assertEqual(line[3:] + "\n", (self.workspace / name).read_text(), "never cut")
        for budget in range(1024, 6000, 97):
            self.text("needle", budget=budget, context=0)  # asserts the bytes fit
        with self.assertRaises(ValueError):
            self.text("needle", budget=1023)
        self.save("skip.bin", b"needle\0")
        complete = self.text("needle", budget=64 * 1024, context=0)
        self.assertTrue(complete.splitlines()[0].endswith(" complete; skipped binary 1"))
        self.assertFalse(complete.splitlines()[-1].startswith("incomplete"))

    def run_main(self, argv, cwd=None):
        class Stdout:
            buffer = io.BytesIO()

            @classmethod
            def write(cls, text):
                cls.buffer.write(text.encode("utf-8"))
        stderr = io.StringIO()
        previous = os.getcwd()
        os.chdir(cwd or self.workspace)
        try:
            with patch.object(sys, "stdout", Stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = source.main(argv)
                except SystemExit as exc:
                    code = exc.code
        finally:
            os.chdir(previous)
        return code, Stdout.buffer.getvalue().decode("utf-8"), stderr.getvalue()

    def test_workspace_cannot_be_given_twice_in_any_spelling(self):
        other = self.directory / "other"
        other.mkdir()
        (other / "x.txt").write_text("needle\n")
        self.save("a.txt", "needle\n")
        first = ["--workspace", str(self.workspace)]
        for action in (["--search", "needle"], ["--batch", '[{"path":"x.txt"}]']):
            for again in (["--workspace", str(other)], ["--workspace=" + str(other)], ["--work", str(other)],
                          ["--workspace=" + str(self.workspace)]):
                with self.subTest(action=action[0], again=again):
                    code, stdout, stderr = self.run_main(first + action + again)
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertTrue("only once" in stderr or "unrecognized" in stderr, stderr)
        for argv in (["--search", "a", "--search", "b"], ["--batch", "[]", "--batch", "[]"], ["--wor=" + str(other), "--search", "a"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_main(first + argv)[0], 2)
        code, stdout, _ = self.run_main(["--workspace=" + str(self.workspace), "--search", "needle"])
        self.assertEqual(code, 0)
        self.assertIn("a.txt sha256=", stdout)

    def test_batch_single_and_related_retrieval_refuse_secret_files(self):
        self.save(".ssh/id_rsa", "PRIVATE KEY\n")
        self.save(".SSH/config", "Host x\n")
        self.save(".env", "TOKEN=1\n")
        self.save("config/prod.kubeconfig", "token\n")
        self.save("deploy_key_rsa", "key\n")
        self.save("ok.py", "import credentials_store\n\ndef run():\n    return credentials_store.load()\n")
        self.save("credentials_store.py", "def load():\n    return 1\n")
        (self.workspace / "sub").mkdir()
        requests = [{"id": name, "path": name} for name in
                    (".ssh/id_rsa", ".SSH/config", ".env", "config/prod.kubeconfig", "deploy_key_rsa",
                     "sub/../.env", "ok.py")]
        output = source.batch(requests, self.workspace)
        by_id = {entry["id"]: entry for entry in output["results"]}
        self.assertEqual(by_id["ok.py"]["status"], "ok")
        for name in requests[:-1]:
            with self.subTest(path=name["path"]):
                entry = by_id[name["id"]]
                self.assertEqual(entry["status"], "error")
                self.assertIn("looks like a secret", entry["error"])
        self.assertNotIn("PRIVATE KEY", json.dumps(output))
        self.assertFalse(output["complete"])
        # A symlink with an innocent name to a secret file resolves to the secret.
        try:
            os.symlink(self.workspace / ".env", self.workspace / "notes.txt")
        except (OSError, NotImplementedError):
            pass
        else:
            entry = source.batch([{"path": "notes.txt"}], self.workspace)["results"][0]
            self.assertEqual(entry["status"], "error")
        for path in (".env", ".ssh/id_rsa"):
            with self.subTest(single=path):
                code, stdout, stderr = self.run_main([path])
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("looks like a secret", stderr)
        self.assertEqual(self.run_main(["ok.py", "--start", "1", "--limit", "1"])[0], 0)
        related = source.source_context.related("ok.py", "run", self.workspace, guard=source._refuse_secret)
        self.assertTrue(any("looks like a secret" in issue["reason"] for issue in related["issues"]))
        self.assertNotIn("return 1", json.dumps(related))

    def test_secret_names_and_directories(self):
        rules = source._workspace_rules()
        for name in ("kubeconfig", "prod.kubeconfig", ".vault-token", ".s3cfg", "auth.json", ".yarnrc.yml", ".envrc",
                     "my-serviceaccount-key.json", "id_ed25519_sk", "id_ecdsa_sk", "deploy_key", "deploy_key.pub",
                     "office.ovpn", "terraform.tfstate", "terraform.tfstate.backup", "token.txt", "a.p12", "b.pfx"):
            with self.subTest(name=name):
                self.assertTrue(rules.secret_looking(name))
        for rel in (".ssh/config", ".SSH/known_hosts", "home/.Aws/config", ".kube/cache/x"):
            with self.subTest(rel=rel):
                self.assertTrue(rules.secret_path(rel))
        for rel in ("src/app.py", "docs/ssh.md", "serviceaccount.yaml"):
            with self.subTest(rel=rel):
                self.assertFalse(rules.secret_path(rel))
        self.save(".AWS/config", "needle\n")
        self.save("app.txt", "needle\n")
        output = self.search("needle")
        self.assertEqual(self.paths(output), ["app.txt"])
        self.assertEqual(output["files_skipped"]["secret_looking"], 1)

    def test_env_git_and_tool_credential_files_are_refused_and_never_searched(self):
        names = [".env-production", ".env_local", "env.local", ".flaskenv", ".dev.vars", ".git/config",
                 ".GIT/HEAD", "sub/.git/config", ".claude.json", ".bash_history", ".zsh_history", "svc.keytab",
                 ".terraformrc", "local.settings.json", "wp-config.php", ".config/gh/hosts.yml"]
        for name in names:
            self.save(name, "needle TOKEN\n")
        self.save(".github/workflows/ci.yml", "needle\n")
        self.save("config/hosts.yml", "needle\n")
        rules = source._workspace_rules()
        for name in names:
            with self.subTest(path=name):
                self.assertTrue(rules.secret_path(name))
        for name in (".github/workflows/ci.yml", "config/hosts.yml", "environment.md", "src/env.py"):
            with self.subTest(path=name):
                self.assertFalse(rules.secret_path(name))
        output = source.batch([{"id": str(index), "path": name} for index, name in enumerate(names)], self.workspace)
        for entry in output["results"]:
            with self.subTest(path=names[int(entry["id"])]):
                self.assertEqual(entry["status"], "error")
                self.assertIn("looks like a secret", entry["error"])
        self.assertNotIn("TOKEN", json.dumps(output))
        code, stdout, stderr = self.run_main([".git/config"])
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("looks like a secret", stderr)
        opened = []
        real_open = os.open
        def recording_open(path, *args, **kwargs):
            opened.append(os.fspath(path).replace("\\", "/"))
            return real_open(path, *args, **kwargs)
        with patch("os.open", recording_open):
            searched = self.search("needle")
        self.assertEqual(sorted(self.paths(searched)), [".github/workflows/ci.yml", "config/hosts.yml"])
        for name in names:
            self.assertFalse([path for path in opened if path.endswith("/" + name)], name)

    def test_credential_directories_given_as_the_workspace_are_refused(self):
        stores = {".docker": "config.json", ".kube": "config", "repo/.git": "config"}
        for store, name in stores.items():
            with self.subTest(store=store):
                directory = self.directory / "home" / store
                directory.mkdir(parents=True)
                (directory / name).write_text("needle AUTH-BLOB\n")
                (directory / "plain.txt").write_text("needle\n")
                entry = source.batch([{"path": name}, {"path": "plain.txt"}], directory)["results"]
                for item in entry:
                    self.assertEqual(item["status"], "error")
                    self.assertIn("looks like a secret", item["error"])
                for regex in (False, True):
                    with self.assertRaisesRegex(ValueError, "credential or .git directory"):
                        source.search_workspace(directory, "needle", regex=regex)
                code, stdout, stderr = self.run_main([name], cwd=directory)
                self.assertEqual((code, stdout), (1, ""))
                self.assertIn("looks like a secret", stderr)
        # ~/.config/gh is not a credential store as a whole, but its
        # hosts.yml is refused from there and from any parent.
        gh = self.directory / "home/.config/gh"
        gh.mkdir(parents=True)
        (gh / "hosts.yml").write_text("needle AUTH-BLOB\n")
        (gh / "config.yml").write_text("needle\n")
        for workspace, rel in ((gh, "hosts.yml"), (self.directory / "home", ".config/gh/hosts.yml")):
            output = source.batch([{"path": rel}], workspace)
            self.assertEqual(output["results"][0]["status"], "error")
            self.assertNotIn("AUTH-BLOB", json.dumps(output))
        for regex in (False, True):
            output = source.search_workspace(gh, "needle", regex=regex)
            self.assertEqual(self.paths(output), ["config.yml"])
            self.assertEqual(output["files_skipped"]["secret_looking"], 1)
        code, stdout, _ = self.run_main(["hosts.yml"], cwd=gh)
        self.assertEqual((code, stdout), (1, ""))
        # A project merely named like a secret is still searchable.
        project = self.directory / "secret-santa"
        project.mkdir()
        (project / "app.py").write_text("needle\n")
        self.assertEqual(self.paths(source.search_workspace(project, "needle")), ["app.py"])
        self.assertEqual(source.batch([{"path": "app.py"}], project)["results"][0]["status"], "ok")
        (project / "mod.py").write_text("def run():\n    return 1\n")
        self.assertEqual(source.source_context.related("mod.py", "run", project, guard=source._refuse_secret)["issues"], [])

    def test_more_credential_names_and_windows_spellings(self):
        rules = source._workspace_rules()
        for rel in (".my.cnf", "_netrc", "_pgpass", "pgpass.conf", ".psql_history", ".mysql_history",
                    ".python_history", ".histfile", ".m2/settings.xml", "home/.gradle/gradle.properties",
                    ".vscode/sftp.json", "sops/keys.txt", ".config/age/keys.txt"):
            with self.subTest(rel=rel):
                self.assertTrue(rules.secret_path(rel))
        for rel in ("settings.xml", "gradle.properties", ".vscode/settings.json", "keys.txt", "docs/keys.txt"):
            with self.subTest(rel=rel):
                self.assertFalse(rules.secret_path(rel))
        tricks = (".env.", ".env ", ".env:stream", "id_rsa::$DATA", ".SSH./config", "Wp-Config.PHP. ")
        with patch.object(rules, "_WINDOWS_NAMES", True):
            for rel in tricks:
                with self.subTest(windows=rel):
                    self.assertTrue(rules.secret_path(rel))
            self.assertTrue(rules.secret_location("C:/Users/me/.docker/config.json"))
            self.assertFalse(rules.secret_path("C:/src/app.py"))
        with patch.object(rules, "_WINDOWS_NAMES", False):
            self.assertFalse(rules.secret_path("id_rsa::$DATA"), "POSIX names are taken literally")

    def worker(self, job, close=True, timeout=30):
        child = subprocess.Popen([sys.executable, "-I", "-B", str(ROOT / "scripts/source.py"), "--scan-worker"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        child.stdin.write(json.dumps(job).encode("ascii") + b"\n")
        child.stdin.flush()
        if close:
            child.stdin.close()
        return child

    def test_scan_worker_refuses_secrets_and_bounds_itself_like_batch(self):
        outside = self.directory / "outside"
        for rel, text in ((".ssh/id_rsa", "needle KEY"), (".env", "needle TOKEN"), ("ok.txt", "needle")):
            (outside / rel).parent.mkdir(parents=True, exist_ok=True)
            (outside / rel).write_text(text + "\n")
        (self.directory / "escape.txt").write_text("needle ESCAPED\n")
        job = {"root": str(outside), "query": "needle", "ignore_case": False, "per_file": 8, "context": 0,
               "files": [[".ssh/id_rsa", 11], [".env", 13], ["ok.txt", 7], ["../escape.txt", 15]]}
        child = self.worker(job, close=False)
        stdout = b""
        for line in child.stdout:
            stdout += line
            if line.startswith(b'{"done"'):
                break
        child.stdin.close()
        child.wait(timeout=30)
        self.assertEqual(child.returncode, 0)
        child.stdout.close()
        child.stderr.close()
        records = [json.loads(line) for line in stdout.splitlines()]
        outcomes = {record["path"]: record["outcome"] for record in records if "outcome" in record}
        self.assertEqual(outcomes, {".ssh/id_rsa": "secret_looking", ".env": "secret_looking", "ok.txt": "scanned",
                                    "../escape.txt": "unreadable"})
        self.assertNotIn(b"KEY", stdout)
        self.assertNotIn(b"TOKEN", stdout)
        self.assertNotIn(b"ESCAPED", stdout)
        self.assertEqual(records[-1], {"done": True})
        # A credential directory as the root is refused outright.
        docker = self.directory / ".docker"
        docker.mkdir()
        (docker / "config.json").write_text("needle\n")
        child = self.worker(dict(job, root=str(docker), files=[["config.json", 7]]), close=False)
        stdout, stderr = child.stdout.read(), child.stderr.read()
        child.wait(timeout=30)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
        self.assertEqual(child.returncode, 2)
        self.assertEqual(stdout, b"")
        self.assertIn(b"credential", stderr)

    def test_scan_worker_succeeds_when_the_parent_closes_stdin_right_after_done(self):
        # The race seen on Windows CI: the parent reads the done line and
        # closes stdin, and the EOF watchdog exits before the main thread
        # does. Forced here: stdin reaches EOF while the done line is written,
        # and the write returns only once the watchdog has exited.
        import threading
        self.save("ok.txt", "needle\n")
        job = {"root": str(self.workspace), "query": "needle", "ignore_case": False, "per_file": 8, "context": 0,
               "files": [["ok.txt", 7]]}
        eof, exits = threading.Event(), []

        class Stdin:
            def readline(self):
                return json.dumps(job).encode("ascii") + b"\n"

            def read(self):
                eof.wait(10)
                return b""

        class Stdout:
            def write(self, data):
                if data.startswith(b'{"done"'):
                    eof.set()
                    for _ in range(1000):
                        if exits:
                            break
                        time.sleep(0.01)

            def flush(self):
                pass

        class Exit(BaseException):
            pass

        def fake_exit(code):
            exits.append(code)
            if threading.current_thread() is threading.main_thread():
                raise Exit
        streams = [patch.object(sys, name, type("S", (), {"buffer": value})())
                   for name, value in (("stdin", Stdin()), ("stdout", Stdout()))]
        # No RLIMIT_CPU on this test process: the worker's `import resource` fails.
        with streams[0], streams[1], patch.object(source.os, "_exit", fake_exit), \
                patch.dict(sys.modules, {"resource": None}):
            with self.assertRaises(Exit):
                source._scan_worker()
        self.assertEqual(exits, [0, 0], "the watchdog's exit after done is a success")

    def test_scan_worker_exits_when_its_parent_lets_go(self):
        # Many moderately slow lines: the orphan thread runs between them.
        self.save("slow.txt", ("a" * 3000 + "\n") * 300)
        job = {"root": str(self.workspace), "query": r"\w+!", "ignore_case": False, "per_file": 8, "context": 0,
               "files": [["slow.txt", (self.workspace / "slow.txt").stat().st_size]]}
        child = self.worker(job, close=False)
        time.sleep(0.5)
        self.assertIsNone(child.poll(), "still scanning")
        started = time.monotonic()
        child.stdin.close()
        child.wait(timeout=10)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(child.returncode, 3)
        child.stdout.close()
        child.stderr.close()

    @unittest.skipIf(os.name == "nt", "RLIMIT_CPU is POSIX")
    def test_scan_worker_cpu_limit_ends_a_runaway_match(self):
        self.save("slow.txt", "a" * 64 + "b\n")
        job = {"root": str(self.workspace), "query": r"(a+)+$", "ignore_case": False, "per_file": 8, "context": 0,
               "files": [["slow.txt", 66]], "cpu_seconds": 1}
        child = self.worker(job, close=False)
        started = time.monotonic()
        child.wait(timeout=20)
        child.stdin.close()
        self.assertLess(time.monotonic() - started, 6)
        self.assertNotEqual(child.returncode, 0)
        for stream in (child.stdout, child.stderr):
            stream.close()

    def fake_worker(self, code):
        real = subprocess.Popen
        def popen(argv, **kwargs):
            return real([sys.executable, "-I", "-c", code], **kwargs)
        return patch.object(source.subprocess, "Popen", popen)

    def test_truncated_worker_output_is_reported_with_its_stderr(self):
        self.save("a.txt", "needle\n")
        code = ("import sys; sys.stdin.buffer.readline(); sys.stderr.write('x' * 500 + 'boom: worker died'); "
                "sys.stdout.write('{\"open\": \"a.txt\"}\\n{\"path\": \"a.tx'); sys.stdout.flush(); sys.exit(1)")
        with self.fake_worker(code):
            output = self.search("needle", regex=True)
            text = self.text("needle", regex=True)
        self.assertIn("worker_failed", output["incomplete_reasons"])
        self.assertEqual(output["interrupted_file"], "a.txt")
        self.assertTrue(output["worker_stderr"].endswith("boom: worker died"))
        self.assertEqual(len(output["worker_stderr"]), 200)
        self.assertIn('; worker stderr "', text)
        self.assert_worker_gone()

    def test_deadline_holds_when_the_job_write_blocks(self):
        # Larger than a pipe buffer, sent to a child that never reads stdin.
        for index in range(1500):
            self.save("d%02d/f%04d_%s.txt" % (index % 50, index, "x" * 40), "needle\n")
        with self.fake_worker("import time; time.sleep(60)"):
            started = time.monotonic()
            output = self.search("needle", regex=True, time_limit=1.0)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("time_limit", output["incomplete_reasons"])
        self.assertEqual(output["files_skipped"]["not_scanned"], 1500)
        self.assertIsNone(output["interrupted_file"])
        self.assert_worker_gone()

    def test_explore_skill_example_commands_still_run(self):
        self.save("src/units.py", "def millis(value):\n    return value * 1000\n")
        self.save("tests/test_units.py", "def test_millis():\n    assert millis(1) == 1000\n")
        text = (ROOT / "skills/explore/SKILL.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```sh\n(.*?)```", text, re.S)
        digest = hashlib.sha256((self.workspace / "src/units.py").read_bytes()).hexdigest()
        for block in blocks:
            argv = shlex.split(block.strip().replace("${CLAUDE_PROJECT_DIR}", str(self.workspace))
                               .replace("SHA256", digest).replace("QUERY", "millis"))
            self.assertEqual(argv[:4], ["bash", "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh", "--script", "scripts/source.py"])
            with self.subTest(command=argv[4:7]):
                code, stdout, stderr = self.run_main(argv[4:])
                self.assertEqual(code, 0, stderr)
                self.assertIn("millis", stdout)
        self.assertIn("src/units.py sha256=" + digest, self.run_main(shlex.split(
            blocks[0].strip().replace("${CLAUDE_PROJECT_DIR}", str(self.workspace)).replace("QUERY", "millis"))[4:])[1])

    def test_explore_skill_search_command_bypasses_the_trim_hook(self):
        text = (ROOT / "skills/explore/SKILL.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```sh\n(.*?)```", text, re.S)
        self.assertEqual(len(blocks), 2)
        env = {"CLAUDE_PLUGIN_ROOT": str(ROOT)}
        for block in blocks:
            command = block.strip().replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
            with self.subTest(command=command[:80]):
                self.assertIn('--workspace "${CLAUDE_PROJECT_DIR}"', command)
                self.assertTrue(recovery.is_source_command(command, env, str(self.workspace)))
                self.assertFalse(recovery.is_source_command(command + " | head", env, str(self.workspace)))
        self.assertIn("--search", blocks[0])
        self.assertIn("--batch", blocks[1])


if __name__ == "__main__":
    unittest.main()
