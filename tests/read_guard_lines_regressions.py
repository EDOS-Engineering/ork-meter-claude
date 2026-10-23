"""ORK_METER_READ_GUARD_LINES: the read guard's page length and minimum line count.

The medium-source benchmark file (16,974 bytes, 308 lines) is the motivating
case. With the default 300-line page the page is nearly the whole file, so the
offline token gate refuses it; a shorter page lets the guard act.
"""
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import load, run_hook  # noqa: E402
import entitled  # noqa: E402
entitled.install()
import features  # noqa: E402

guard = load("hooks/read-guard.py")


def medium_like(path):
    """308 lines, about 17 KB, not Python (so only the plain page applies)."""
    lines = ["line %03d: " % i + "configuration value with an explanatory note " for i in range(308)]
    path.write_text("\n".join(lines) + "\n")
    return path


class ReadGuardLines(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.file = medium_like(Path(self.temporary.name) / "settings.txt")

    def invoke(self, env):
        payload = {"hook_event_name": "PreToolUse", "session_id": "lines", "tool_name": "Read",
                   "scratchpad_dir": str(Path(self.temporary.name) / "scratch"),
                   "tool_input": {"file_path": str(self.file)}}
        # started=, as run.py passes it: a reply that is ready past the budget
        # (exact token counting is slow on a cold Windows runner) still prints.
        return run_hook(payload, guard.handle, guard.EVENTS, env=env, started=time.monotonic())

    def test_tunable_declared_with_default_300_and_floor_50(self):
        self.assertEqual(features.value({}, "read_guard", "lines"), 300)
        self.assertEqual(features.value({"ORK_METER_READ_GUARD_LINES": "150"}, "read_guard", "lines"), 150)
        self.assertEqual(features.value({"ORK_METER_READ_GUARD_LINES": "10"}, "read_guard", "lines"), 50)
        self.assertEqual(features.value({"ORK_METER_READ_GUARD_LINES": "lots"}, "read_guard", "lines"), 300)
        self.assertEqual(features.policy_settings({})["ORK_METER_READ_GUARD_LINES"], "300")

    def test_default_leaves_a_308_line_file_alone_even_over_the_byte_threshold(self):
        self.assertGreater(self.file.stat().st_size, 12 * 1024)
        self.assertIsNone(self.invoke({"ORK_METER_READ_GUARD_BYTES": "12288"}))

    def test_shorter_page_fires_and_pages_to_that_many_lines(self):
        reply = self.invoke({"ORK_METER_READ_GUARD_BYTES": "12288", "ORK_METER_READ_GUARD_LINES": "150"})
        out = reply["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["limit"], 150)
        self.assertIn("first 150 lines (limit=150)", out["additionalContext"])
        self.assertIn("offset=151", out["additionalContext"])

    def test_page_at_least_the_file_length_is_left_alone(self):
        self.assertIsNone(self.invoke({"ORK_METER_READ_GUARD_BYTES": "12288", "ORK_METER_READ_GUARD_LINES": "400"}))

    def test_ceiling_1500_keeps_a_page_the_token_gate_can_admit(self):
        # #145: from about 1950 lines up a page is as long as Read's native 2000-line
        # page, so the token gate refused every Read and the guard was silently off.
        value = lambda raw: features.value({"ORK_METER_READ_GUARD_LINES": raw}, "read_guard", "lines")
        self.assertEqual((value("1500"), value("1950"), value("5000"), value("9k")), (1500, 1500, 1500, 1500))
        self.assertEqual(features.policy_settings({"ORK_METER_READ_GUARD_LINES": "1950"})["ORK_METER_READ_GUARD_LINES"], "1500")
        lines = ["line %04d: " % i + "configuration value with an explanatory note " for i in range(2100)]
        self.file.write_text("\n".join(lines) + "\n")
        reply = self.invoke({"ORK_METER_READ_GUARD_LINES": "1950"})
        self.assertEqual(reply["hookSpecificOutput"]["updatedInput"]["limit"], 1500)

    def test_under_the_byte_threshold_lines_alone_never_fire(self):
        self.assertIsNone(self.invoke({"ORK_METER_READ_GUARD_BYTES": "24576", "ORK_METER_READ_GUARD_LINES": "100"}))


if __name__ == "__main__":
    unittest.main(verbosity=1)
