"""The test filter drops pytest -rA short-summary PASSED lines (verbose-suite task)."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_*, puts hooks/lib on sys.path
import trimmer  # noqa: E402


class PytestShortSummary(unittest.TestCase):
    def test_rA_passed_summary_lines_are_dropped_and_failures_kept(self):
        lines = ["tests/test_x.py::test_ok[%d] PASSED [ 50%%]" % i for i in range(300)]
        lines += ["=" * 20 + " PASSES " + "=" * 20, "=" * 10 + " short test summary info " + "=" * 10]
        lines += ["PASSED tests/test_x.py::test_ok[%d]" % i for i in range(300)]
        lines += ["FAILED tests/test_x.py::test_bad - AssertionError: assert 1 == 2", "1 failed, 300 passed in 0.1s"]
        text, omitted = trimmer.filter_test_output("\n".join(lines))
        self.assertEqual(omitted, 600)
        self.assertNotIn("PASSED tests/test_x.py::test_ok[7]", text)
        self.assertIn("FAILED tests/test_x.py::test_bad - AssertionError", text)
        self.assertIn("1 failed, 300 passed", text)

    def test_a_small_confirming_set_keeps_its_names(self):
        lines = ["PASSED tests/test_x.py::test_fix", "PASSED tests/test_x.py::test_other", "2 passed in 0.01s"]
        text, _ = trimmer.filter_test_output("\n".join(lines))
        self.assertIn("PASSED tests/test_x.py::test_fix", text)

    def test_unrelated_lines_starting_with_passed_are_kept(self):
        text, omitted = trimmer.filter_test_output("PASSED the build step\nPASSED: 3 checks\n" * 3)
        self.assertEqual(omitted, 0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
