"""Actual descendants, partial output, and pre-execution cancellation."""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ROOT, StoreCase, clean_environ  # noqa: E402
import artifacts  # noqa: E402
import ledger  # noqa: E402


class Cancellation(StoreCase):
    def setUp(self):
        super().setUp()
        self.cancel = self.root / "cancel"
        self.env = clean_environ()

    def argv(self, command):
        return [sys.executable, "-I", "-B", str(ROOT / "scripts/command.py"),
                "--scratchpad", str(self.scratch), "--cancel-file", str(self.cancel), "--", *command]

    def saved(self):
        paths = list(Path(self.store.tool_output_dir()).glob("*.json"))
        self.assertEqual(len(paths), 1)
        return artifacts.load(self.store, paths[0].stem)["response"]

    def start_tree(self):
        child = self.root / "child.py"
        child.write_text("import os,signal,sys,time\nfrom pathlib import Path\n"
                         "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
                         "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                         "Path('child-pid').write_text(str(os.getpid()))\n"
                         "sys.stdout.buffer.write(b'child partial\\r\\n');sys.stdout.buffer.flush()\n"
                         "Path('child-ready').touch()\ntime.sleep(10)\nPath('escaped').touch()\n")
        parent = self.root / "parent.py"
        parent.write_text("import os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
                          "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
                          "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                          "Path('parent-pid').write_text(str(os.getpid()))\n"
                          "sys.stdout.buffer.write(b'partial\\n'*5000);sys.stdout.buffer.flush()\n"
                          "sys.stderr.buffer.write(b'partial error\\r\\n');sys.stderr.buffer.flush()\n"
                          "subprocess.Popen([sys.executable,'child.py'])\n"
                          "while not Path('child-ready').exists(): time.sleep(.01)\n"
                          "Path('ready').touch()\ntime.sleep(30)\n")
        wrapper = subprocess.Popen(self.argv([sys.executable, str(parent)]), cwd=self.root,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        def cleanup():
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.communicate(timeout=5)
            # Only fixture-owned process group; Windows job close handles its tree.
            if os.name != "nt" and (self.root / "parent-pid").exists():
                try:
                    os.killpg(int((self.root / "parent-pid").read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not (self.root / "ready").exists():
            if wrapper.poll() is not None or time.monotonic() > deadline:
                self.fail("fixture did not reach ready state")
            time.sleep(.02)
        return wrapper

    def assert_terminated(self, pid):
        if os.name == "nt":
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.OpenProcess(0x100000, False, pid)
            if handle:
                try:
                    self.assertEqual(api.WaitForSingleObject(handle, 3000), 0, "fixture descendant survived cancellation")
                finally:
                    api.CloseHandle(handle)
        else:
            # A terminated child can briefly remain a zombie until reaped.
            path = Path("/proc") / str(pid) / "stat"
            if path.exists():
                self.assertEqual(path.read_text().split(")", 1)[1].split()[0], "Z")

    def test_existing_cancel_never_executes_original(self):
        self.cancel.touch()
        command = "from pathlib import Path;Path('executed').touch()"
        result = subprocess.run(self.argv([sys.executable, "-c", command]), cwd=self.root,
                                capture_output=True, env=self.env, timeout=5)
        self.assertEqual(result.returncode, 130)
        self.assertFalse((self.root / "executed").exists())
        response = self.saved()
        self.assertTrue(response["interrupted"])
        self.assertEqual(response["cancellation_scope"], "not-started")
        self.assertEqual(ledger.load(self.store), [])

    def test_cancel_kills_grandchild_and_preserves_partial_streams(self):
        wrapper = self.start_tree()
        self.cancel.touch()
        stdout, stderr = wrapper.communicate(timeout=5)
        self.assertNotEqual(wrapper.returncode, 0)
        self.assertTrue(stdout == b"partial\n" * 5000 + b"child partial\r\n")
        self.assertEqual(stderr, b"partial error\r\n")
        response = self.saved()
        self.assertTrue(response["interrupted"])
        self.assertTrue(response["stdout"].encode() == stdout)
        self.assertEqual(response["stderr"].encode(), stderr)
        self.assertEqual(response["cancellation_scope"], "windows-job" if os.name == "nt" else "posix-process-group")
        self.assertEqual(ledger.load(self.store), [])
        for name in ("parent-pid", "child-pid"):
            self.assert_terminated(int((self.root / name).read_text()))
        self.assertFalse((self.root / "escaped").exists())

    @unittest.skipUnless(os.name == "nt", "Windows job lifecycle")
    def test_hard_wrapper_death_closes_job_and_kills_descendants(self):
        wrapper = self.start_tree()
        wrapper.kill()
        wrapper.communicate(timeout=5)
        for name in ("parent-pid", "child-pid"):
            self.assert_terminated(int((self.root / name).read_text()))
        self.assertEqual(ledger.load(self.store), [])
        self.assertEqual(list(Path(self.store.tool_output_dir()).glob("*.json")), [])

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_signalling_a_zombie_only_group_is_not_a_capture_failure(self):
        # macOS killpg gives EPERM for a group of only zombies (#140).
        import process_tree
        tree = process_tree.Tree([sys.executable, "-c", "pass"])
        # Leave the exited leader unreaped (os.waitid is missing on macOS before 3.13).
        deadline = time.monotonic() + 5
        while not subprocess.run(["ps", "-o", "stat=", "-p", str(tree.process.pid)],
                                 capture_output=True, text=True).stdout.strip().startswith("Z"):
            self.assertLess(time.monotonic(), deadline, "fixture did not become a zombie")
            time.sleep(.01)
        tree.terminate(signal.SIGINT)
        tree.terminate(signal.SIGINT, force=True)
        tree.close(normal=False)
        tree.process.communicate(timeout=5)
        self.assertEqual(tree.process.returncode, 0)

    @unittest.skipIf(os.name == "nt", "POSIX signal forwarding")
    def test_signal_cancels_process_group(self):
        wrapper = self.start_tree()
        wrapper.send_signal(signal.SIGTERM)
        stdout, stderr = wrapper.communicate(timeout=5)
        self.assertNotEqual(wrapper.returncode, 0)
        self.assertTrue(self.saved()["interrupted"])
        self.assertTrue(stdout == b"partial\n" * 5000 + b"child partial\r\n")
        self.assertEqual(stderr, b"partial error\r\n")
        self.assertEqual(ledger.load(self.store), [])


if __name__ == "__main__":
    unittest.main()
