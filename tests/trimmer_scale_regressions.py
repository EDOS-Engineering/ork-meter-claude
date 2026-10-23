"""The trimmer scales to multi-megabyte outputs without changing its output (#130).

``tests/fixtures/trimmer_reference.py`` is hooks/lib/trimmer.py as it was
before the single-pass diagnostic scan, verbatim. It checked every line with
two case-insensitive regex searches, three times over, which took about
1.5 s on a 6 MB build log and missed the hook's 1.5 s reply budget. The
current trimmer must give byte-identical results on a varied corpus. When
trimmer behaviour changes on purpose, refresh the reference in the same PR.
"""
from pathlib import Path
import random
import re
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import load  # noqa: E402
import trimmer  # noqa: E402

reference = load("tests/fixtures/trimmer_reference.py")

# Fragments that sit on every edge of the diagnostic patterns: case, word
# boundaries, near misses, the code points re.IGNORECASE folds differently
# from str.lower(), ANSI, pytest and go test lines, carriage returns.
FRAGMENTS = [
    "error", "ERROR", "Error:", "fail", "FAILED", "xfail", "xfailed", "panic", "Traceback", "exception", "fatal",
    "denied", "timeout", "exit code 2", "EXIT CODE", "exit  code", "skip", "skipped", "Skipped: 0", "skips",
    "skipping", "skipper", "ignored", "xpass", "XPASSED", "pending", "todo", "TODO", "todos", "İgnored",
    "ıgnored", "ſkip", "Key", "errør", "faıl", "ok", "Compiling", "crate", "module_1",
    "→", "é", "ß", "Σ", "\x1b[31m", "\x1b[0m", "   ", "\t", "PASSED",
    "tests/test_a.py::test_x PASSED", "--- PASS: TestX (0.00s)", "=== RUN   TestX", "[build]", "x" * 50,
    "_error_", "error_", "9fail9", "\r",
]


def corpus(count=60, seed=130):
    rand = random.Random(seed)
    cap = trimmer.LINE_CAP
    for _ in range(count):
        lines = []
        for _ in range(rand.randint(0, 400)):
            roll = rand.random()
            if roll < 0.15 and lines:
                lines.extend([lines[-1]] * rand.randint(1, 6))
            elif roll < 0.18:
                # Long lines with a word at the LINE_CAP cut: capping can turn
                # "skipz" into "skip… [+n chars]", which is a diagnostic.
                word = rand.choice(["skip", "error", "todo", "fail", "plain", "İ", "ſkip"])
                lines.append("y" * (cap - len(word) + rand.randint(-3, 3)) + word
                             + rand.choice(["", "z", " ", "s"]) + "y" * rand.randint(0, 400))
            elif roll < 0.6:
                lines.append(" ".join(rand.choice(FRAGMENTS) for _ in range(rand.randint(0, 6))))
            else:
                lines.append("plain line %d" % rand.randint(0, 50))
        yield "\n".join(lines)


def test_runner_corpus(count=80, seed=190):
    """Test-runner output on every edge of the pass filter (#190): go test -v
    activity with and without a matching pass, pytest, cargo, jest, maven and
    gradle lines, skip words, ``Skipped: 0``, ANSI and blank runs."""
    rand = random.Random(seed)
    names = ["TestA", "TestB", "TestA/sub_1", "TestC/x"]
    lines_for = [
        lambda: "=== %s   %s" % (rand.choice(["RUN", "PAUSE", "CONT"]), rand.choice(names)),
        lambda: "    --- PASS: %s (0.%02ds)" % (rand.choice(names), rand.randrange(99)),
        lambda: "--- FAIL: %s (0.01s)" % rand.choice(names),
        lambda: "--- SKIP: %s (0.00s)" % rand.choice(names),
        lambda: "ok  \tgithub.com/x/pkg_%d\t%s" % (rand.randrange(9), rand.choice(["(cached)", "0.12s", "1.5s coverage: 80.0% of statements"])),
        lambda: "tests/test_%d.py::test_%s %s [ %d%%]" % (rand.randrange(5), rand.choice(["a", "b[1-2]", "c"]),
                                                     rand.choice(["PASSED", "FAILED", "SKIPPED", "XFAIL", "PASSED"]), rand.randrange(100)),
        lambda: "tests/test_%d.py %s" % (rand.randrange(5), rand.choice(["....", "..s.", "..F", "...  [ 40%]"])),
        lambda: "PASSED tests/test_%d.py::test_%d" % (rand.randrange(5), rand.randrange(20)),
        lambda: "test mod::case_%d ... %s" % (rand.randrange(50), rand.choice(["ok", "ok", "ignored", "FAILED"])),
        lambda: "%s should work %d" % (rand.choice(["✓", "✔", "√", "✕"]), rand.randrange(9)),
        lambda: "PASS src/app_%d.test.js (%d ms)" % (rand.randrange(9), rand.randrange(900)),
        lambda: "com.x.Suite > case_%d PASSED" % rand.randrange(9),
        lambda: "[INFO] Tests run: %d, Failures: 0, Errors: 0, Skipped: %s%s" % (
            rand.randrange(1, 50), rand.choice(["0", "0", "1", "00"]), rand.choice(["", ", Time elapsed: 0.1 s", " todo"])),
        lambda: rand.choice(["", "  ", "\x1b[32mok\x1b[0m", "skipped 2 tests", "pending: 1", "TODO later", "panic: boom",
                             "    main_test.go:12: expected 1, got 2", "FAIL", "PASS", "=== RUN", "=== RUN   ",
                             "✓ ſkipped one", "PASS İGNORED", "--- PASS: TestA (0.00s) Skipped: 0", "✓ x-Skipped: 0-todo",
                             "✓ Skipped: 0", "✓ Skipped: 01", "✓ aSkipped: 0 b"]),
    ]
    for _ in range(count):
        lines = [rand.choice(lines_for)() for _ in range(rand.randint(0, 300))]
        if rand.random() < 0.2 and lines:
            lines[0] = rand.choice(["{\"Action\":\"run\"}", "  [build] start", "<xml>", "plain"])
        yield "\n".join(lines)


def build_log(size, seed=7):
    """A compiler log with a few diagnostics, like the #130 measurements."""
    rand = random.Random(seed)
    out, total, i = [], 0, 0
    while total < size:
        roll = rand.random()
        if roll < 0.0002:
            line = "error[E0308]: mismatched types in crate_%d/src/mod_%d.rs:%d" % (i % 50, i % 300, i)
        elif roll < 0.0003:
            line = "test crate_%d::slow_case ... ignored" % (i % 50)
        elif roll < 0.01:
            line = "warning: unused variable `x_%d`" % i
        elif roll < 0.05:
            line = "   Compiling crate_%d v0.%d.%d (/build/crate_%d)" % (i % 500, i % 10, i % 7, i % 500)
        elif roll < 0.07:
            line = "\x1b[32m  [build]\x1b[0m step %d/%d done   " % (i, i + 9)
        else:
            line = "cc -O2 -Wall -c src/module_%05d.c -o obj/module_%05d.o -Iinclude -DVERSION=%d" % (i, i, i % 97)
        out.append(line)
        total += len(line) + 1
        i += 1
    return "\n".join(out)


class SameOutput(unittest.TestCase):
    def test_varied_corpus_matches_the_reference(self):
        for n, text in enumerate(corpus()):
            with self.subTest(text=n):
                self.assertEqual(trimmer._normalise(text)[0], reference.normalise(text))
                for budget in (512, 2048, 12288):
                    self.assertEqual(trimmer.trim_text(text, budget), reference.trim_text(text, budget))
                self.assertEqual(trimmer.trim_head(text, 1024, 7), reference.trim_head(text, 1024, 7))
                self.assertEqual(trimmer.filter_test_output(text), reference.filter_test_output(text))

    def test_test_runner_corpus_matches_the_reference(self):
        omitted = 0
        for n, text in enumerate(test_runner_corpus()):
            with self.subTest(text=n):
                filtered = trimmer.filter_test_output(text)
                self.assertEqual(filtered, reference.filter_test_output(text))
                omitted += filtered[1]
                for budget in (512, 2048):
                    self.assertEqual(trimmer.trim_text(filtered[0], budget), reference.trim_text(filtered[0], budget))
        self.assertGreater(omitted, 1000)   # the corpus exercises the filter, not only its guard

    def test_checked_text_skips_only_the_guard(self):
        # #190: a caller that has checked the format passes checked=True.
        for text in list(corpus(count=30, seed=11)) + list(test_runner_corpus(count=30, seed=12)):
            if trimmer.unsupported_line_format(text):
                continue
            self.assertEqual(trimmer.filter_test_output(text, checked=True), trimmer.filter_test_output(text))
            for budget in (512, 4096):
                self.assertEqual(trimmer.trim_text(text, budget, checked=True), trimmer.trim_text(text, budget))

    def test_build_log_matches_the_reference(self):
        text = build_log(512 * 1024)
        for budget in (1024, 12288):
            self.assertEqual(trimmer.trim_text(text, budget), reference.trim_text(text, budget))
        self.assertEqual(trimmer.trim_head(text, 12288), reference.trim_head(text, 12288))

    def test_mandatory_indices_match_the_per_line_check(self):
        for text in corpus(count=60, seed=7):
            lines = text.split("\n")
            self.assertEqual(trimmer._mandatory_indices(lines),
                             [i for i, line in enumerate(lines) if trimmer.mandatory_line(line)])


class Prefilter(unittest.TestCase):
    def test_words_cover_the_diagnostic_patterns(self):
        # _MANDATORY_WORDS is derived from these two patterns by hand. A
        # change to either must update the words, then this expectation.
        self.assertEqual(trimmer.ERROR_RE.pattern,
                         r"error|fail|panic|traceback|exception|fatal|denied|timeout|exit code")
        self.assertEqual(trimmer.SKIP_LINE_RE.pattern,
                         r"\b(?:skip(?:ped|s|ping)?|ignored|xfail(?:ed)?|xpass(?:ed)?|pending|todo)\b")
        self.assertEqual(trimmer.ERROR_RE.flags & re.IGNORECASE, re.IGNORECASE)
        self.assertEqual(trimmer.SKIP_LINE_RE.flags & re.IGNORECASE, re.IGNORECASE)

    def test_fold_table_is_complete(self):
        # Every non-ASCII character that re.IGNORECASE matches to a letter of
        # the words must lower() to that letter, or be in _FOLD.
        letters = sorted(set("".join(trimmer._MANDATORY_WORDS)) - {" "})
        any_letter = re.compile("[%s]" % "".join(letters), re.IGNORECASE)
        for code in range(128, sys.maxunicode + 1):
            char = chr(code)
            if not any_letter.fullmatch(char):
                continue
            for letter in letters:
                if re.fullmatch(letter, char, re.IGNORECASE):
                    folded = trimmer._FOLD.get(code, char).lower()
                    self.assertEqual(folded, letter, "U+%04X" % code)


def checked(module, text, head=True):
    """(mandatory_line calls, trim_text result) for one trim of ``text``,
    plus a trim_head with ``head``."""
    original, seen = module.mandatory_line, [0]

    def counting(line):
        seen[0] += 1
        return original(line)
    module.mandatory_line = counting
    try:
        result = module.trim_text(text, 12288)
        if head:
            module.trim_head(text, 12288)
    finally:
        module.mandatory_line = original
    return seen[0], result


class Scale(unittest.TestCase):
    def test_per_line_checks_are_limited_to_candidate_lines(self):
        # The reference calls mandatory_line on every line, several times
        # over; the scan must only confirm lines that hold a diagnostic word.
        text = build_log(2 * 1024 * 1024)
        line_count = text.count("\n") + 1
        bound = line_count // 20
        self.assertGreater(checked(reference, text)[0], line_count)  # the test can tell the two apart
        self.assertLess(checked(trimmer, text)[0], bound)

    def test_large_log_matches_the_reference_with_far_fewer_line_checks(self):
        # This was a wall-clock race (3x faster than the reference), which a
        # loaded runner can lose (#193). The per-line checks are what made
        # the reference slow: it confirms every line three or more times at
        # any size, the trimmer under 1 line in 20, with the same output.
        for size in (512 * 1024, 2 * 1024 * 1024):
            with self.subTest(size=size):
                text = build_log(size)
                lines = text.count("\n") + 1
                old_checks, old_result = checked(reference, text, head=False)
                new_checks, new_result = checked(trimmer, text, head=False)
                self.assertEqual(new_result, old_result)
                self.assertGreaterEqual(old_checks, 3 * lines)
                self.assertLess(new_checks * 20, lines)

    def test_trim_time_grows_linearly_with_the_log(self):
        # The line counts above miss a slowdown that is not per line, such as
        # a quadratic join or copy of the text. Four times the log must take
        # well under eight times as long (linear measures about 4.1; quadratic
        # would be about 16). Each size is the best of three runs, so a
        # stalled runner has to stall all three to fail it.
        def best(text):
            times = []
            for _ in range(3):
                start = time.perf_counter()
                trimmer.trim_text(text, 12288)
                trimmer.trim_head(text, 12288)
                times.append(time.perf_counter() - start)
            return min(times)
        small, large = best(build_log(512 * 1024)), best(build_log(2 * 1024 * 1024))
        self.assertLess(large, 8 * small, "512 KiB: %.3f s, 2 MiB: %.3f s" % (small, large))


if __name__ == "__main__":
    unittest.main()
