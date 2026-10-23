"""Recovering trimmed output (#129) and the reads index under parallel Read hooks (#131)."""
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
from support import context as hook_context, load, payload  # noqa: E402
import artifacts
import ledger
import session

# The benchmark pipeline's FULL_OUTPUT pattern (#244): the note's
# first line must keep matching it.
PIPELINE_FULL_OUTPUT = re.compile(r"^Full output: (\S+?)\.txt \(\d+ bytes")


def context(scratch, tool, response, tool_input, prompt="prompt-1", cwd=None):
    return hook_context(payload(scratch, tool, tool_input, response, prompt_id=prompt, cwd=cwd or str(scratch)))


def read_response(path, content):
    lines = content.count("\n") + 1
    return {"type": "text", "file": {"filePath": path, "content": content, "numLines": lines,
                                     "startLine": 1, "totalLines": lines}}


CHILD = "--parallel-read-child"


def parallel_read(scratch, index, start):
    """One Read hook process: wait for the start file, then index one file.

    Run as ``python this-file --parallel-read-child ...`` rather than through
    multiprocessing: spawn re-imports the parent's ``__main__``, which in the
    native Windows CI runner is a whole-suite discovery script without a main
    guard, so every worker re-ran the entire suite."""
    # A slow runner under 24-way contention can outwait the hook's 0.5 s
    # lock budget, and an unlocked fallback write may then drop an entry by
    # design. This test is about the locked path, so give it all the time
    # it needs (as loop-guard's test does).
    session.LOCK_WAIT_SECONDS = 60
    reader = load("hooks/dedupe-reads.py")
    content = "".join("file %d line %d\n" % (index, n) for n in range(200))
    path = "/tmp/project/file_%02d.py" % index
    ctx = context(scratch, "Read", read_response(path, content), {"file_path": path})
    Path("%s.ready-%d" % (start, index)).write_text("")
    deadline = time.monotonic() + 60
    while not os.path.exists(start):
        if time.monotonic() > deadline:
            raise SystemExit("start file never appeared")
        time.sleep(0.001)
    reader.handle(ctx)


class OutputRecovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.scratch = self.root / "scratch"
        self.store = session.Store.for_payload({"scratchpad_dir": str(self.scratch)})
        self.trim = load("hooks/trim-tool-output.py")

    def bash(self, command, stdout, cwd=None):
        response = {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}
        return self.trim.handle(context(self.scratch, "Bash", response, {"command": command}, cwd=cwd))

    def saved(self):
        return sorted(Path(self.store.tool_output_dir()).iterdir())

    def test_reading_a_full_output_artifact_is_not_trimmed_or_saved_again(self):
        text = "".join("record_%05d evidence\n" % i for i in range(4000))
        trimmed = self.bash("generate", text)
        note = trimmed["hookSpecificOutput"]["updatedToolOutput"]["stdout"].rsplit("Full output: ", 1)[1]
        path = note.split(" (", 1)[0]
        self.assertEqual(Path(path).read_text(), text)
        before = self.saved()
        rows = len(ledger.load(self.store))
        quoted = shlex.quote(path)
        relative = shlex.quote(os.path.relpath(path, self.scratch))
        middle = "".join("record_%05d evidence\n" % i for i in range(1000, 3001))
        for command in ("cat " + quoted, "cat -n " + quoted, "/bin/cat " + quoted + " " + quoted,
                        "sed -n '1000,3000p' " + quoted, "sed -n '1000,$p' " + quoted,
                        "head -n 3000 " + quoted, "head -3000 " + quoted, "tail -n +1000 " + quoted,
                        "tail -n 2000 " + quoted, "cat " + relative):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, text if "1000," not in command else middle))
        self.assertEqual(self.saved(), before)
        self.assertEqual(len(ledger.load(self.store)), rows)
        # Anything that is not a plain read of this session's artifacts keeps
        # the normal reduction (and its own Full output note).
        other = session.Store.for_payload({"scratchpad_dir": str(self.root / "other")})
        foreign = artifacts.save(other, "Bash", {"stdout": text}, text)
        plain = self.root / "plain.txt"
        plain.write_text(text)
        for command in ("cat %s | grep record" % quoted, "cat %s; echo done" % quoted,
                        "cat %s > /tmp/copy" % quoted, "cat %s %s" % (quoted, plain),
                        "cat " + shlex.quote(foreign), "cat $HOME/" + os.path.basename(path),
                        "cat \"$(echo %s)\"" % path, "sed -n '1,5p;w /tmp/x' " + quoted,
                        "sed -i '' '1d' " + quoted, "sed '1,5p' " + quoted, "head -n x " + quoted,
                        "grep record " + quoted, "cat " + str(plain), "cat"):
            with self.subTest(command=command):
                reply = self.bash(command, text)
                self.assertIsNotNone(reply)
                self.assertIn("Full output: ", reply["hookSpecificOutput"]["updatedToolOutput"]["stdout"])

    HINT = "Outside the workspace; Bash sed -n 'A,Bp', head, tail or cat reads it."

    def test_note_outside_the_workspace_points_at_bash_reads_that_pass_untrimmed(self):
        """#269: under --restricted (or a permission-confined session) Read and
        Grep refuse the store path outside the workspace; the note's second
        line says Bash reads it, and those reads arrive untrimmed."""
        work = self.root / "work"
        work.mkdir()
        text = "".join("record_%05d evidence\n" % i for i in range(4000))
        stdout = self.bash("generate", text, cwd=str(work))["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        full, hint = stdout.split("\n")[-2:]
        self.assertEqual(hint, self.HINT)
        self.assertNotIn("recover", hint.lower())   # a test-filter note still invites nothing
        # Every parser of the note still reads the same path.
        path = stdout.rsplit("Full output: ", 1)[1].rsplit(" (", 1)[0]
        self.assertEqual(full, "Full output: %s (%d bytes)" % (path, len(text)))
        self.assertEqual(stdout.split("Full output: ")[-1].split(" (")[0], path)
        self.assertEqual(PIPELINE_FULL_OUTPUT.match(full).group(1) + ".txt", path)
        self.assertEqual(Path(path).read_text(), text)
        # Each read the hint names, run from the workspace, passes through
        # whole and saves nothing again.
        lines = text.splitlines(True)
        quoted = shlex.quote(path)
        before = self.saved()
        for command, shown in (("sed -n '1000,3000p' " + quoted, "".join(lines[999:3000])),
                               ("head -n 2000 " + quoted, "".join(lines[:2000])),
                               ("tail -n 2000 " + quoted, "".join(lines[2000:])), ("cat " + quoted, text)):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, shown, cwd=str(work)))
        self.assertEqual(self.saved(), before)

    def test_note_inside_the_workspace_is_the_path_alone(self):
        text = "".join("record_%05d evidence\n" % i for i in range(4000))
        for cwd in (str(self.root), str(self.scratch)):
            with self.subTest(cwd=cwd):
                stdout = self.bash("generate", text, cwd=cwd)["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
                self.assertNotIn(self.HINT, stdout)
                self.assertRegex(stdout.rsplit("\n", 1)[1], r"^Full output: \S+ \(\d+ bytes\)$")

    def test_every_trim_note_outside_the_workspace_carries_the_hint(self):
        work = str(self.root / "work")
        lines = ["src/handler_%03d.go:%d: match %s" % (i, i, "x" * 80) for i in range(600)]
        grep = self.trim.handle(context(self.scratch, "Grep", {"mode": "content", "content": "\n".join(lines),
                                                               "numLines": 600, "numFiles": 600, "filenames": []},
                                        {"pattern": "match"}, cwd=work))
        content = grep["hookSpecificOutput"]["updatedToolOutput"]["content"]
        glob = self.trim.handle(context(self.scratch, "Glob", {"filenames": ["src/%s/handler_%03d.go" % ("d" * 60, i)
                                                                             for i in range(600)],
                                                               "numFiles": 600, "truncated": False, "durationMs": 1},
                                        {"pattern": "**/*.go"}, cwd=work))
        item = glob["hookSpecificOutput"]["updatedToolOutput"]["filenames"][-1]
        for name, note in (("Grep", content.rsplit("\nFull output: ", 1)[1]), ("Glob", item[len("Full output: "):])):
            with self.subTest(tool=name):
                path, rest = note.split(" (", 1)
                self.assertTrue(Path(path).is_file())
                self.assertEqual(rest.split("\n"), [rest.split("\n")[0], self.HINT])
        # The delta note's own suffix stays on the Full output line.
        ctx = context(self.scratch, "Bash", {}, {"command": "x"}, cwd=work)
        path = str(Path(self.store.tool_output_dir()) / ("a" * 64 + ".txt"))
        note = self.trim.note(ctx, path, "abc", "; set ORK_METER_DELTA=off to always get full output.")
        self.assertEqual(note.split("\n"), ["Full output: %s (3 bytes); set ORK_METER_DELTA=off to always get full output."
                                            % path, self.HINT])
        # No cwd in the payload: outside, the safe side.
        bare = hook_context(payload(self.scratch, "Bash", {"command": "x"}, {}))
        self.assertEqual(self.trim.note(bare, path, "abc").split("\n")[1:], [self.HINT])

    def test_repeat_read_is_stubbed_only_within_one_prompt_and_says_how_to_recover(self):
        reader = load("hooks/dedupe-reads.py")
        path = str(self.root / "module.py")
        content = "".join("def fn_%03d():\n    return %d\n" % (i, i) for i in range(200))

        def read(prompt, tool_input=None):
            return reader.handle(context(self.scratch, "Read", read_response(path, content),
                                         tool_input or {"file_path": path}, prompt=prompt))

        self.assertIsNone(read("prompt-1"))
        stub = read("prompt-1")
        self.assertEqual(stub["hookSpecificOutput"]["updatedToolOutput"]["file"]["numLines"], 15)
        note = stub["hookSpecificOutput"]["additionalContext"]
        self.assertIn("If it is not in your context, Read %s with offset=16 and limit=386" % path, note)
        # The suggested range passes through in full.
        self.assertIsNone(read("prompt-1", {"file_path": path, "offset": 16, "limit": 386}))
        # After /rewind or an edited prompt no hook fires; a later prompt's
        # re-read gets the full content because the earlier one may be gone.
        self.assertIsNone(read("prompt-2"))
        # Without a prompt_id there is no evidence either way: never stub.
        self.assertIsNone(read(None))
        self.assertIsNone(read(None))
        self.assertIsNone(read("prompt-3"))
        self.assertIsNotNone(read("prompt-3"))

    def test_parallel_read_hooks_keep_every_reads_index_entry(self):
        workers = 24
        for attempt in range(3):
            scratch = self.root / ("parallel-%d" % attempt)
            start = self.root / ("start-%d" % attempt)
            store = session.Store.for_payload({"scratchpad_dir": str(scratch)})
            processes = [subprocess.Popen([sys.executable, "-I", "-B", __file__, CHILD, str(scratch), str(i), str(start)],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                         for i in range(workers)]
            # Release them together once every child is ready (bounded).
            deadline = time.monotonic() + 60
            while (time.monotonic() < deadline
                   and len(list(self.root.glob(start.name + ".ready-*"))) < workers
                   and all(p.poll() is None for p in processes)):
                time.sleep(0.01)
            start.write_text("")
            for i, process in enumerate(processes):
                output, _ = process.communicate(timeout=120)
                self.assertEqual(process.returncode, 0, "child %d:\n%s" % (i, output))
            paths = sorted(k.split("\x00")[0] for k in store.reads())
            self.assertEqual(paths, ["/tmp/project/file_%02d.py" % i for i in range(workers)])
            self.assertFalse(os.path.exists(store.reads_path + ".lock"))

    def test_windows_replace_retries_a_sharing_violation_for_a_bounded_time(self):
        target = self.root / "state.json"
        source = self.root / "state.tmp"
        real = os.replace
        calls = []

        def busy(src, dst):
            calls.append(dst)
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied")   # another process has it open
            real(src, dst)

        source.write_text("new")
        with patch.object(session.os, "replace", busy), patch.object(session.os, "name", "nt"):
            session._replace(str(source), str(target))
        self.assertEqual((len(calls), target.read_text()), (3, "new"))
        calls.clear()
        source.write_text("again")
        with patch.object(session.os, "replace", busy), patch.object(session.os, "name", "posix"):
            with self.assertRaises(PermissionError):
                session._replace(str(source), str(target))
        self.assertEqual(len(calls), 1, "only Windows sharing violations are retried")
        always = lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "Access is denied"))
        started = time.monotonic()
        with patch.object(session.os, "replace", always), patch.object(session.os, "name", "nt"), \
                patch.object(session, "REPLACE_RETRY_SECONDS", 0.1):
            with self.assertRaises(PermissionError):
                session._replace(str(source), str(target))
        self.assertLess(time.monotonic() - started, 2)

    def test_reads_lock_waits_a_bounded_time_and_breaks_a_stale_lock(self):
        lock = self.store.reads_path + ".lock"
        Path(lock).write_text("")
        started = time.monotonic()
        self.store.update_reads("a", {"sha256": "1"})
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.store.reads(), {"a": {"sha256": "1"}})
        self.assertTrue(os.path.exists(lock), "a live lock held by another hook is not ours to remove")
        old = time.time() - 60
        os.utime(lock, (old, old))
        self.store.update_reads("b", {"sha256": "2"})
        self.assertEqual(sorted(self.store.reads()), ["a", "b"])
        self.assertFalse(os.path.exists(lock))


if __name__ == "__main__":
    if sys.argv[1:2] == [CHILD]:
        parallel_read(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4])
    else:
        unittest.main()
