"""host_retry: one retry on an assertion failure, never on errors (#140)."""
from pathlib import Path
import contextlib
import io
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own
import host_retry  # noqa: E402


def run(case_cls):
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(case_cls)
    result = unittest.TestResult()
    with contextlib.redirect_stderr(io.StringIO()) as err:
        suite.run(result)
    return result, err.getvalue()


class Retry(unittest.TestCase):
    def test_fails_once_then_passes(self):
        calls = []

        @host_retry.retry_once
        class Case(unittest.TestCase):
            def test_x(self):
                calls.append(1)
                self.assertGreater(len(calls), 1)
        result, err = run(Case)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(len(calls), 2)
        self.assertIn("RETRY Case.test_x once", err)

    def test_fails_twice_fails(self):
        calls = []

        @host_retry.retry_once
        class Case(unittest.TestCase):
            def test_x(self):
                calls.append(1)
                self.fail("always")
        result, _ = run(Case)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(len(calls), 2)

    def test_failed_subtest_is_retried_once(self):
        # unittest records a subTest failure in the result instead of raising
        # it: without the retry, mode='reduce' of a native suite failed on one
        # late hook reply (#140).
        calls = []

        @host_retry.retry_once
        class Case(unittest.TestCase):
            def test_x(self):
                calls.append(1)
                for mode in ("off", "reduce"):
                    with self.subTest(mode=mode):
                        self.assertTrue(mode == "off" or len(calls) > 1)
        result, err = run(Case)
        self.assertTrue(result.wasSuccessful(), result.failures)
        self.assertEqual(len(calls), 2)
        self.assertIn("RETRY Case.test_x once", err)
        self.assertIn("subTest mode='reduce'", err)

    def test_failed_subtest_twice_fails_and_reports_the_subtest(self):
        calls, seen = [], []

        @host_retry.retry_once
        class Case(unittest.TestCase):
            def test_x(self):
                calls.append(1)
                for mode in ("off", "reduce"):
                    with self.subTest(mode=mode):
                        seen.append(mode)
                        self.assertEqual(mode, "off")
        result, _ = run(Case)
        self.assertEqual(len(calls), 2)
        self.assertEqual(seen, ["off", "reduce"] * 2, "a failed subtest does not stop the others")
        self.assertEqual(len(result.failures), 1)
        self.assertIn("mode='reduce'", str(result.failures[0][0]))

    def test_errors_in_subtests_are_not_retried(self):
        calls = []

        @host_retry.retry_once
        class Case(unittest.TestCase):
            def test_x(self):
                calls.append(1)
                with self.subTest(mode="reduce"):
                    raise RuntimeError("broken")
        result, err = run(Case)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("RETRY", err)

    def test_errors_are_not_retried(self):
        calls = []

        @host_retry.retry_once
        class Case(unittest.TestCase):
            def test_x(self):
                calls.append(1)
                raise RuntimeError("broken")
        result, _ = run(Case)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(calls), 1)

    def test_expected_failures_are_not_retried(self):
        calls = []

        @host_retry.retry_once
        class Case(unittest.TestCase):
            @unittest.expectedFailure
            def test_x(self):
                calls.append(1)
                self.fail("a known gap")
        result, err = run(Case)
        self.assertEqual(len(result.expectedFailures), 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("RETRY", err)

    def test_every_host_suite_is_decorated(self):
        root = Path(__file__).resolve().parent
        for path in sorted(root.glob("native_*qualification.py")):
            text = path.read_text(encoding="utf-8")
            if "RUN_NATIVE_CLAUDE_TESTS" in text:
                self.assertIn("@host_retry.retry_once", text, path.name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
