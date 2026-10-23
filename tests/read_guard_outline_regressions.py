"""The Python outline depends on the file, not on runner speed (#140).

read-guard used to skip the AST outline whenever less than 0.2 s of its
budget was left. Parsing a module that is mostly comments costs about a
millisecond, so on a slow or briefly stalled Windows runner the outline
vanished for reasons unrelated to the file. A module with little code is
now outlined whatever is left of the budget; a code-heavy one still needs
time.
"""
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
import hook  # noqa: E402

guard = load("hooks/read-guard.py")


def comment_heavy(path, lines=1500):
    """The host qualification's fixture: two functions around 1500 comment lines."""
    path.write_text("def first():\n    return 1\n" + "".join("# line %d %s\n" % (i, "x" * 100) for i in range(lines))
                    + "def distant():\n    return 2\n", encoding="utf-8")
    return path


def code_heavy(path, functions=3, statements=700):
    """Well over CHEAP_CODE_BYTES of real code, in a few long functions so the
    outline itself stays small."""
    body = "".join("    v_%d = [x * %d + 1 for x in range(%d) if x %% 3 == 0]\n" % (i, i, i) for i in range(statements))
    path.write_text("".join("def f_%d(a, b):\n%s    return a\n\n" % (n, body) for n in range(functions)),
                    encoding="utf-8")
    return path


class Outline(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def invoke(self, path, left=None):
        """read-guard's reply for a Read of ``path``. With ``left``, the budget
        is cut to ``left`` seconds once the lines are counted, so the outline
        decision is what sees a stalled runner, however slow the setup before
        it was (on Windows that alone can exceed a short whole-hook budget)."""
        payload = {"hook_event_name": "PreToolUse", "session_id": "outline", "tool_name": "Read",
                   "scratchpad_dir": str(self.root / "scratch"), "tool_input": {"file_path": str(path)}}
        contexts = []

        def handle(ctx):
            contexts.append(ctx)
            return guard.handle(ctx)

        def count_lines(*args, **kwargs):
            counted = original(*args, **kwargs)
            if left is not None:
                contexts[-1].deadline = time.monotonic() + left
            return counted

        original = guard.count_lines
        with patch.object(guard, "count_lines", count_lines):
            return run_hook(payload, handle, guard.EVENTS, budget=hook.DEFAULT_BUDGET, started=time.monotonic())

    def context(self, reply):
        return (reply or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    # A runner that stalled: under 0.2 s of the budget left when read-guard
    # reaches the outline, as in the Windows failures (the page itself still
    # arrived, so the budget was nearly, not entirely, spent).
    ALMOST_SPENT = 0.1

    def test_mostly_comments_is_outlined_with_the_budget_nearly_spent(self):
        path = comment_heavy(self.root / "large.py")
        self.assertLessEqual(guard.code_bytes(path.read_bytes()), guard.CHEAP_CODE_BYTES)
        reply = self.invoke(path, left=self.ALMOST_SPENT)
        self.assertIn("Python AST outline", self.context(reply), "a slow runner must not lose the outline")
        self.assertIn("distant", self.context(reply))

    def test_code_heavy_module_needs_time_left(self):
        path = code_heavy(self.root / "big.py")
        self.assertGreater(guard.code_bytes(path.read_bytes()), guard.CHEAP_CODE_BYTES)
        self.assertNotIn("Python AST outline", self.context(self.invoke(path, left=self.ALMOST_SPENT)))
        self.assertIn("Python AST outline", self.context(self.invoke(path)))

    def test_code_bytes_ignores_blank_and_comment_lines(self):
        self.assertEqual(guard.code_bytes(b"# c\n\n   # indented\nx = 1\n  y = 2\n"), len(b"x = 1") + len(b"  y = 2"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
