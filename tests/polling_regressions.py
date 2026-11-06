"""Regressions for completion-aware polling (hooks/lib/polling_runtime.py,
polling_shell_scan.py, polling_receipt.py), ported from the hooks/lib/polling.py
suite that commit d421a74 replaced.

The request clock is the deadline.json a UserPromptSubmit/PostToolBatch hook
writes (polling_runtime.remaining), not the transcript. A launch gets a
completion receipt appended (polling_receipt.py) and runs in the foreground for
the window. A poll of an owned log waits in the hook for that receipt instead
of sleeping; the model gets no note (native_text.clean_reply keeps no
additionalContext), so the record is the note-/wait-*.json rows under the state
directory. Dan's own suites (tests/polling_completion_regressions.py,
polling_http_regressions.py, polling_pipeline_regressions.py) and
tests/poll-collect.test.sh hold the launch/exit-status, cancellation, unowned
and tasks/<id>.output cases; nothing here repeats them.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import support  # noqa: E402  scrubs ORK_METER_* when this file runs on its own
from support import context, payload  # noqa: E402
import polling_receipt  # noqa: E402
import polling_runtime as runtime  # noqa: E402
import polling_shell  # noqa: E402
import polling_shell_scan  # noqa: E402
import session  # noqa: E402

BASH = polling_shell_scan.bash_binary()


def deadline(root, seconds_ago=0):
    """Age the request window's origin, as polling_runtime.remaining reads it."""
    polling_receipt.write(root / "deadline.json", dict(origin_monotonic=time.monotonic() - seconds_ago))


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "state"
        self.root.mkdir()
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()

    def bash(self, command, cwd):
        return payload(self.tmp, "Bash", dict(command=command), cwd=str(cwd), tool_use_id="poll")

    def owned(self, done=None, cancelled=False):
        """A task row owning work/run.log, as polling_runtime.launch writes it."""
        record = dict(receipt=str(self.root / "r.done"), nonce="b" * 32, tool_use_id="launch",
                      paths=[str(self.cwd / "run.log")], background_id="bg1", cancelled=cancelled)
        polling_receipt.write(self.root / "task-x.json", record)
        if done is not None:
            self.finish(record, done)
        return record

    def finish(self, record, code):
        polling_receipt.write(record["receipt"], dict(nonce=record["nonce"], exit_code=code,
                                                      completed_monotonic=time.monotonic()))

    def wait_row(self, tool_use_id="poll"):
        return runtime.read(self.root / ("wait-" + runtime.identity(tool_use_id) + ".json"))

    # --- the request clock -------------------------------------------------

    def test_window_is_closed_without_a_plausible_origin(self):
        # Was polling.budget over the transcript's api-request/user stamps; the clock is
        # now deadline.json (polling_runtime.remaining): 0 means no wait and no foreground run.
        self.assertEqual(runtime.remaining(self.root), 0, "no deadline.json")
        for origin, reason in ((None, "missing"), ("2026-10-26T12:00:00", "text"), (float("nan"), "nan")):
            polling_receipt.write(self.root / "deadline.json", dict(origin_monotonic=origin))
            self.assertEqual(runtime.remaining(self.root), 0, reason)
        deadline(self.root, -60)
        self.assertEqual(runtime.remaining(self.root), 0, "future")
        deadline(self.root, 400)
        self.assertEqual(runtime.remaining(self.root), 0, "stale")
        deadline(self.root, 70)
        self.assertAlmostEqual(runtime.remaining(self.root), runtime.WINDOW - 70 - 1, delta=2)

    # --- launches ----------------------------------------------------------

    @unittest.skipUnless(BASH, "admission validates the command with bash -n")
    def test_launch_grammar(self):
        # polling_shell.admission (polling_shell_scan.classify) replaced polling.launch_paths:
        # pipelines into tee, `&&`, `;`/newline lists of listed executables are admitted now.
        for command in ("python3 train.py > run.log 2>&1", "node build.js >> out.log",
                        "ffmpeg -i a.mp4 b.mp4 > f.log; echo done", "/usr/bin/python3 -c 'print(1); print(2)' > x.log",
                        "python3 a.py | tee log", "python3 a.py && echo ok", "python3 a.py; ls", "python3 a.py\nls"):
            self.assertIsNotNone(polling_shell.admission(command, self.cwd), command)
        for command in ("bash run.sh > log", "python3 $(which x)", "python3 a.py > log &", "f() { python3; }; f",
                        "python3 a.py | grep x", "", "ls > log"):
            self.assertIsNone(polling_shell.admission(command, self.cwd), command)
        self.assertEqual(polling_shell.admission("python3 a.py > run.log", self.cwd), [str(self.cwd / "run.log")])
        self.assertEqual(polling_shell.admission("python3 a.py > $OUT", self.cwd), [], "owned, but no literal path")
        self.assertEqual(polling_shell.admission("python3 a.py | tee t.log > r.log", self.cwd),
                         sorted([str(self.cwd / "t.log"), str(self.cwd / "r.log")]))

    @unittest.skipUnless(BASH, "admission validates the command with bash -n")
    def test_launch_runs_in_the_foreground_only_inside_the_window(self):
        # Was polling.launch(payload, root, budget, python) -> (updated, record); the window now
        # comes from deadline.json and a foreground launch is owned too (it was (None, None)).
        deadline(self.root)
        item = payload(self.tmp, "Bash", dict(command="python3 a.py > a.log", run_in_background=True),
                       cwd=str(self.cwd), tool_use_id="bg")
        updated = runtime.launch(item, self.root)
        self.assertIs(updated["run_in_background"], False)
        self.assertTrue(updated["command"].startswith("python3 a.py > a.log\n"))
        self.assertTrue(200000 < updated["timeout"] <= runtime.WINDOW * 1000, updated["timeout"])
        updated = runtime.launch(dict(item, tool_use_id="fg", tool_input=dict(command="python3 a.py > a.log")), self.root)
        self.assertIs(updated["run_in_background"], False, "a foreground launch is owned now")
        self.assertTrue(200000 < updated["timeout"] <= runtime.WINDOW * 1000, updated["timeout"])
        deadline(self.root, runtime.WINDOW - 1.5)   # half a second of window: the launch stays native
        updated = runtime.launch(dict(item, tool_use_id="late"), self.root)
        self.assertIs(updated["run_in_background"], True)
        self.assertNotIn("timeout", updated)
        self.assertEqual(len(runtime.tasks(self.root)), 3, "every admitted launch has a task row")

    # --- polls -------------------------------------------------------------

    def test_poll_input_keeps_the_read_and_suffix(self):
        for command, expected in [
            ("sleep 540; tail -5 /tmp/owned.log; ls -la output", "tail -5 /tmp/owned.log; ls -la output"),
            ('sleep 30; cat "/tmp/owned log"; printf "exact;value"', 'cat "/tmp/owned log"; printf "exact;value"'),
            ("sleep 2m && tail -n 20 run.log", "tail -n 20 run.log"),
        ]:
            self.assertEqual(runtime.poll_input(self.bash(command, self.cwd))[1], expected, command)
        self.assertEqual(runtime.poll_input(self.bash("sleep 2m && tail -n 20 run.log", self.cwd))[2], 120)
        for command in ("sleep 5; cat $(echo /tmp/log); ls", "sleep 5; tail -f /tmp/log; ls", "sleep 5; cat /tmp/log | grep x; ls",
                        "sleep 5; cat /tmp/log && ls", "sleep 5; cat /tmp/log;; ls", "sleep 5; cat /tmp/log;",
                        "sleep 5; cat a b"):
            self.assertIsNone(runtime.poll_input(self.bash(command, self.cwd)), command)
        # A read without a sleep is a poll too (polling_runtime.simple_poll): nothing to rewrite,
        # but the owned log still binds the read to its task's receipt.
        self.assertEqual(runtime.poll_input(self.bash("tail /tmp/log", self.cwd)), ("/tmp/log", None, None))

    def test_running_task_waits_in_the_hook_for_the_receipt(self):
        # Was: the rewritten shell command waited for the receipt. polling_runtime.poll waits
        # itself and hands back the bare read; the model sees no note (clean_reply), the wait row does.
        record = self.owned()
        deadline(self.root)
        timer = threading.Timer(0.5, self.finish, (record, 3))
        timer.start()
        started = time.monotonic()
        try:
            updated, note = runtime.poll(self.bash("sleep 540; cat run.log", self.cwd), self.root)
        finally:
            timer.join()
        waited = time.monotonic() - started
        self.assertLess(waited, 3, "the wait ends when the receipt appears")
        self.assertGreaterEqual(waited, 0.4)
        self.assertEqual(updated["command"], "cat run.log")
        self.assertIn("completed with exit 3", note)
        self.assertIn("original sleep was not executed", note)
        row = self.wait_row()
        self.assertEqual((row["source_tool_use_id"], row["completion_known"], row["sleep_removed"],
                          row["requested_sleep_seconds"], row["cancelled"]), ("launch", True, True, 540, False))
        self.assertGreaterEqual(row["waited_seconds"], 0.4)

    def test_wait_ends_at_the_deadline_without_a_receipt(self):
        self.owned()
        deadline(self.root, runtime.WINDOW - 1 - 1.2)   # 1.2 s of window left
        started = time.monotonic()
        updated, note = runtime.poll(self.bash("sleep 540; cat run.log", self.cwd), self.root)
        waited = time.monotonic() - started
        self.assertLess(waited, 4)
        self.assertGreaterEqual(waited, 0.8, "polling_runtime.poll waits while more than .2 s of window remain")
        self.assertEqual(updated["command"], "cat run.log")
        self.assertIn("unknown at the request wait deadline", note)
        self.assertFalse(self.wait_row()["completion_known"])

    def test_poll_needs_the_request_clock_and_its_own_session(self):
        # Was polling.collect(...) is None for "no clock" and "other session". The clock gate is
        # polling_runtime.handle (no deadline.json: nothing, not even a launch), and sessions are
        # separate state directories (identity(transcript_path or session_id)) rather than a
        # session_id on the task row.
        def prompt(session_id):
            item = payload(self.tmp, "Bash", {}, cwd=str(self.cwd), session_id=session_id, tool_use_id="prompt")
            self.assertIsNone(runtime.handle(context(item, event="UserPromptSubmit")))
            found = next(path for path in self.tmp.rglob("deadline.json")
                         if path.parent.name == runtime.identity(session_id)).parent
            # handle spells its root through session.fs_path (the \\?\ namespace on Windows) and
            # polling_runtime.tasks drops a row whose receipt parent is not that exact spelling.
            return Path(session.fs_path(str(found)))
        mine, other = prompt("s"), prompt("t")
        self.assertNotEqual(mine, other)
        self.root = mine
        (self.cwd / "run.log").write_text("epoch 9\n")
        self.owned(done=0)
        poll = payload(self.tmp, "Bash", dict(command="sleep 540; tail -1 run.log; echo after"),
                       cwd=str(self.cwd), session_id="s", tool_use_id="poll")
        self.assertIsNone(runtime.handle(context(dict(poll, session_id="t"), event="PreToolUse")), "other session")
        self.assertIsNone(runtime.handle(context(dict(poll, tool_use_id=""), event="PreToolUse")), "no tool id")
        (mine / "deadline.json").unlink()
        self.assertIsNone(runtime.handle(context(poll, event="PreToolUse")), "no clock: the sleep is kept")
        self.assertFalse(list(mine.glob("wait-*.json")))
        deadline(mine)
        reply = runtime.handle(context(poll, event="PreToolUse"))
        self.assertEqual(reply, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput":
                                                        dict(command="tail -1 run.log; echo after")}})
        self.assertTrue(self.wait_row()["completion_known"])
        # The note is kept plugin-side for PostToolUse (never in the reply: clean_reply).
        self.assertIn("completed with exit 0", runtime.read(mine / ("note-" + runtime.identity("poll") + ".json"))["text"])

    @unittest.skipUnless(BASH, "runs the rewritten read in a real shell")
    def test_finished_task_read_is_native(self):
        (self.cwd / "run.log").write_text("epoch 9\n")
        self.owned(done=0)
        deadline(self.root)
        updated, _ = runtime.poll(self.bash("sleep 540; tail -1 run.log; echo after", self.cwd), self.root)
        started = time.monotonic()
        result = subprocess.run([BASH, "--noprofile", "--norc", "-c", updated["command"]], cwd=self.cwd,
                                capture_output=True, text=True, timeout=10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "epoch 9\nafter\n", ""))

    def test_receipt_writer_keeps_the_exit_status(self):
        # Was polling_helper.py --finish <receipt> <code>; now polling_receipt.py <receipt> <nonce> "$?".
        receipt = self.tmp / "done.json"
        for code in (0, 1, 130):
            result = subprocess.run([sys.executable, "-I", polling_receipt.__file__, str(receipt), "a" * 32, str(code)],
                                    capture_output=True, timeout=10)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (code, b"", b""))
            row = json.loads(receipt.read_text())
            self.assertEqual((row["exit_code"], row["nonce"]), (code, "a" * 32))
            self.assertTrue(0 <= row["completed_monotonic"] <= time.monotonic())
            self.assertEqual(runtime.completed(dict(receipt=str(receipt), nonce="a" * 32))["exit_code"], code)


if __name__ == "__main__":
    unittest.main()
