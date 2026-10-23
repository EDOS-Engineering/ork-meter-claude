"""Keep native recovery suffix checks strict across Windows host annotations."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support  # noqa: E402,F401  scrubs ORK_METER_* when this file runs on its own

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_result_assertions import assert_host_suffix  # noqa: E402


class HostSuffix(unittest.TestCase):
    def test_windows_notice_must_name_the_exact_workspace(self):
        work = Path("fixture-work")
        with patch("native_result_assertions.os.name", "nt"):
            assert_host_suffix(self, "\nShell cwd was reset to fixture-work\n", work)
            assert_host_suffix(self, "Shell cwd was reset to fixture-work\n<system-reminder>host</system-reminder>", work)
            for suffix in ("Shell cwd was reset to elsewhere", "Shell cwd was reset to fixture-work-extra",
                           "Shell cwd was reset to fixture-work\ncorrupted evidence", "unexpected output"):
                with self.assertRaises(AssertionError):
                    assert_host_suffix(self, suffix, work)

    def test_posix_does_not_accept_windows_notice(self):
        work = Path("fixture-work")
        with patch("native_result_assertions.os.name", "posix"):
            assert_host_suffix(self, "\n", work)
            with self.assertRaises(AssertionError):
                assert_host_suffix(self, "Shell cwd was reset to fixture-work", work)


if __name__ == "__main__":
    unittest.main()
