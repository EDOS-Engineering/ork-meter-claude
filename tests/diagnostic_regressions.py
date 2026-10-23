"""Complete distinct diagnostics survive soft-budget overflow and normalization."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_*, puts hooks/lib on sys.path
import trimmer  # noqa: E402


class Diagnostics(unittest.TestCase):
    def test_multiline_diagnostic_context_survives_without_recovery(self):
        block = ["error E042: incompatible argument type", " --> engine.rs:28:5",
                 " 28 | initialize(config)", "expected Duration; received integer",
                 "warning: fallback unit conversion is active"]
        noise = ["Compiling module_%04d: objects linked successfully" % i for i in range(1600)]
        text = "\n".join(noise[:800] + block + noise[800:])
        output, info = trimmer.trim_text(text, 4096)
        self.assertIn("\n".join(block), output)
        self.assertLess(trimmer.nbytes(output), trimmer.nbytes(text))
        self.assertEqual(info["error_lines_kept"], 1)

    def test_long_repeated_neighbor_evidence_is_not_normalized_away(self):
        detail = "expected value: " + "a" * 3000
        noise = ["ordinary %04d" % i for i in range(1500)]
        block = ["ERROR: mismatch"] + [detail] * 3
        output, info = trimmer.trim_text("\n".join(noise + block + noise), 1024)
        self.assertIsNotNone(info)
        self.assertIn("\n".join(block), output)

    def test_skipped_test_survives_filter_then_small_budget_trim(self):
        skip = "tests/test_slug.py::test_critical_security_check SKIPPED (needs credentials)"
        passes = "\n".join("test_slug.py::test_%04d PASSED [ 50%%]" % i for i in range(300))
        noise = "\n".join("ordinary detail %04d" % i for i in range(120))
        text = passes + "\n" + noise + "\n" + skip + "\n" + noise + "\nFAILED test_slug.py::test_repeated_whitespace"
        filtered, count = trimmer.filter_test_output(text)
        self.assertEqual(count, 300)
        reduced, info = trimmer.trim_text(filtered, 1024)
        self.assertIsNotNone(info)
        self.assertIn(skip, reduced)
        self.assertIn("FAILED test_slug.py::test_repeated_whitespace", reduced)

    def test_long_repeated_skip_reasons_survive_normalization_and_soft_budget(self):
        skip = "test_security SKIPPED: " + "credentials unavailable " * 180 + "exact reason tail"
        text = "\n".join(["ordinary %04d" % i for i in range(800)] + [skip] * 3 + ["tail %04d" % i for i in range(800)])
        reduced, info = trimmer.trim_text(text, 1024)
        self.assertIsNotNone(info)
        self.assertEqual(reduced.count(skip), 3)
        self.assertEqual(info["error_lines_kept"], 3)
        _, read = trimmer.trim_head(text, 1024)
        self.assertTrue(all(skip in line for line in read["error_lines"]))

    def test_more_than_forty_distinct_errors_all_survive(self):
        errors = ["ERROR: issue_%03d expected %s got %s" % (i, "A" * 400, "B" * 400) for i in range(75)]
        text = "\n".join(["ordinary %04d" % i for i in range(3000)] + errors + ["last ordinary %04d" % i for i in range(3000)])
        output, info = trimmer.trim_text(text, 1024)
        self.assertIsNotNone(info)
        self.assertGreater(trimmer.nbytes(output), 1024, "complete diagnostics override a soft budget")
        for error in errors:
            self.assertIn(error, output)
        self.assertEqual(info["error_lines_kept"], len(errors))

    def test_all_diagnostic_output_passes_through_when_no_reduction_is_possible(self):
        text = "\n".join("fatal: different_%03d %s" % (i, "detail" * 100) for i in range(80))
        output, info = trimmer.trim_text(text, 1024)
        self.assertEqual(output, text)
        self.assertIsNone(info)

    def test_error_beyond_long_prefix_and_detail_beyond_excerpt_are_exact(self):
        error = "fatal: " + "details" * 700 + " exact-needed-tail"
        text = "\n".join(["ordinary %04d" % i for i in range(1000)] + [error] + ["tail %04d" % i for i in range(1000)])
        output, _ = trimmer.trim_text(text, 1024)
        self.assertIn(error, output)
        long_line = "x" * 20000 + " ERROR: needed suffix"
        self.assertEqual(trimmer.trim_text(long_line, 1024), (long_line, None))

    def test_identical_error_occurrence_count_is_preserved(self):
        error = "ERROR: exact repeated failure"
        text = "\n".join(["ordinary %04d" % i for i in range(1000)] + [error] * 70 + ["tail %04d" % i for i in range(1000)])
        output, info = trimmer.trim_text(text, 1024)
        self.assertEqual(output.count(error), 70)
        self.assertEqual(info["error_lines_kept"], 70)

    def test_omission_metadata_excludes_retained_diagnostics(self):
        text = "\n".join(["ordinary %04d" % i for i in range(1000)] + ["ERROR: required"] + ["tail %04d" % i for i in range(1000)])
        output, info = trimmer.trim_text(text, 1024)
        ordinary = [line for line in text.split("\n") if not trimmer.ERROR_RE.search(line)]
        missing = [line for line in ordinary if line not in output.split("\n")]
        self.assertEqual(info["lines_elided"], len(missing))
        self.assertEqual(info["bytes_elided"], sum(len(line.encode()) + 1 for line in missing))

    def test_read_omitted_range_retains_all_complete_numbered_diagnostics(self):
        errors = ["ERROR: issue_%03d %s" % (i, "detail" * 100) for i in range(75)]
        text = "\n".join(["ordinary"] * 1000 + errors)
        _, info = trimmer.trim_head(text, 1024, start_line=12)
        self.assertEqual(info["error_lines_kept"], 75)
        for index, error in enumerate(errors):
            self.assertIn("line %d: %s" % (1012 + index, error), info["error_lines"])


if __name__ == "__main__":
    unittest.main()
