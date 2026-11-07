"""Several processes append to one log: every row must survive, whole.

The Windows C runtime's O_APPEND seeks and then writes, so two hooks appending
at once could land on one offset. A shorter row left a longer one's tail as a
line of its own, and a longer one erased the shorter. This runs in
``unit (windows-latest)``, where that path is real (session.open_append).
"""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support
import hook_telemetry
import ledger
import session

LIB = str(support.ROOT / "hooks/lib")
WORKERS = 8
ROWS = 250

# argv: lib dir, writer, directory, worker number, rows. Prints "ready" when
# imported and set up, then waits for a line, so that all workers start at once.
# Row lengths vary by hundreds of bytes: equal rows would overwrite each other
# without a torn line to show for it.
WORKER = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import hook_telemetry, ledger, session
writer, directory, worker, rows = sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
store = session.Store(directory)
invocation = None
if writer == "telemetry":
    os.environ["ORK_HOOK_TELEMETRY_DIR"] = directory
    invocation = hook_telemetry.Invocation("worker", {"hook_event_name": "PostToolUse"}, time.monotonic())
print("ready", flush=True)
sys.stdin.readline()
for seq in range(rows):
    fields = {"worker": worker, "seq": seq, "padding": "x" * (seq % 5 * 150)}
    if writer == "telemetry":
        invocation.row.update(fields)
        invocation.write()
    elif writer == "ledger":
        ledger.append(store, dict(ledger.stamp(ledger.trimmed("Bash", 1000, 400), "s"), **fields))
    else:   # the code this replaced, on the same runner: see OldCodeControl
        fd = os.open(session.fs_path(store.ledger_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, (json.dumps(fields) + "\n").encode())
        os.close(fd)
"""


def race(writer, directory):
    """All workers appending at once, from the same starting line."""
    workers = [subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", WORKER, LIB, writer, directory, str(worker), str(ROWS)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for worker in range(WORKERS)]
    try:
        for worker in workers:
            ready = worker.stdout.readline().strip()
            if ready != "ready":
                raise AssertionError("worker did not start: %r %s" % (ready, worker.stderr.read()))
        for worker in workers:
            worker.stdin.write("go\n")
            worker.stdin.flush()
        for worker in workers:
            _, errors = worker.communicate(timeout=180)
            if worker.returncode:
                raise AssertionError("worker failed: %s" % errors)
    finally:
        for worker in workers:
            worker.kill()


def damage(path):
    """(unparseable lines, rows missing, rows repeated) in the log at ``path``."""
    seen = []
    bad = []
    with open(session.fs_path(path), "rb") as handle:
        for line in handle.read().split(b"\n"):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if "seq" in row:
                    seen.append((row["worker"], row["seq"]))
            except ValueError as exc:
                bad.append("%s: %r" % (exc, line[:60]))
    expected = {(worker, seq) for worker in range(WORKERS) for seq in range(ROWS)}
    return bad, sorted(expected - set(seen)), len(seen) - len(set(seen))


class ConcurrentAppend(unittest.TestCase):
    def assert_whole(self, path):
        bad, missing, repeated = damage(path)
        self.assertEqual(bad[:5], [], "%d lines unparseable" % len(bad))
        self.assertEqual(missing[:5], [], "%d rows lost" % len(missing))
        self.assertEqual(repeated, 0)

    def test_ledger_rows_are_whole_and_none_are_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            race("ledger", directory)
            store = session.Store(directory)
            self.assert_whole(store.ledger_path)
            scan = ledger.scan(store)
            self.assertEqual(scan["status"], "ok", scan["issues"][:5])
            self.assertEqual(len(scan["rows"]), WORKERS * ROWS)

    def test_telemetry_rows_are_whole_and_none_are_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            race("telemetry", directory)
            path = os.path.join(directory, "hook-invocations.jsonl")
            self.assert_whole(path)
            # Each worker's Invocation wrote one row of its own before the race.
            self.assertEqual(len(Path(path).read_bytes().splitlines()), WORKERS * (ROWS + 1))


@unittest.skipUnless(os.name == "nt", "POSIX O_APPEND is atomic; this is the Windows C runtime's")
class OldCodeControl(unittest.TestCase):
    def test_report_what_the_replaced_code_does_on_this_runner(self):
        """Evidence, not a gate: the race is timing, so it can't be asserted.
        If the old code stays whole here, the tests above prove little on this
        runner, and the log line below says so."""
        with tempfile.TemporaryDirectory() as directory:
            race("old", directory)
            bad, missing, _ = damage(session.Store(directory).ledger_path)
        print("\nold os.open(O_APPEND) + os.write: %d of %d rows lost, %d lines unparseable"
              % (len(missing), WORKERS * ROWS, len(bad)), flush=True)


class OpenAppend(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.path = os.path.join(self.directory, "log.jsonl")

    def test_the_os_picks_the_offset_not_the_caller(self):
        """Deterministic form of the race: a descriptor at a stale offset still
        appends. Neither the file pointer nor a second writer can overwrite."""
        first = session.open_append(self.path)
        second = session.open_append(self.path)
        try:
            os.write(first, b"one\n")
            os.write(second, b"two\n")
            os.lseek(first, 0, os.SEEK_SET)
            os.write(first, b"three\n")
        finally:
            os.close(first)
            os.close(second)
        self.assertEqual(Path(self.path).read_bytes(), b"one\ntwo\nthree\n")

    def test_a_non_ascii_path_and_a_missing_directory(self):
        path = os.path.join(self.directory, "café-日本", "log.jsonl")
        with self.assertRaises(OSError):
            session.open_append(path)
        os.mkdir(os.path.dirname(path))
        os.close(session.open_append(path))
        self.assertTrue(os.path.isfile(path))

    def test_rows_keep_this_platforms_line_ending(self):
        """What is written did not change: one os.linesep after each row (the
        C runtime's text mode and TextIOWrapper both gave CRLF on Windows)."""
        ending = os.linesep.encode()
        store = session.Store(self.directory)
        ledger.append(store, {"kind": "trim"})
        self.assertEqual(Path(store.ledger_path).read_bytes(), b'{"kind": "trim"}' + ending)
        with patch.dict(os.environ, {"ORK_HOOK_TELEMETRY_DIR": self.directory}):
            hook_telemetry.Invocation("worker", {}, time.monotonic())
        raw = Path(self.directory, "hook-invocations.jsonl").read_bytes()
        self.assertTrue(raw.endswith(b"}" + ending), raw[-8:])

    def test_telemetry_fails_open(self):
        with patch.dict(os.environ, {"ORK_HOOK_TELEMETRY_DIR": self.directory}), \
                patch("session.open_append", side_effect=OSError("no")):
            invocation = hook_telemetry.Invocation("worker", {}, time.monotonic())
            invocation.finish()
        self.assertFalse(os.path.exists(os.path.join(self.directory, "hook-invocations.jsonl")))

    @unittest.skipIf(os.name == "nt", "POSIX flags and modes")
    def test_posix_flags_and_mode_are_unchanged(self):
        os.close(session.open_append(self.path))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        link = self.path + ".link"
        os.symlink(self.path, link)
        with self.assertRaises(OSError):
            session.open_append(link, os.O_NOFOLLOW)
        os.close(session.open_append(link))


if __name__ == "__main__":
    unittest.main()
