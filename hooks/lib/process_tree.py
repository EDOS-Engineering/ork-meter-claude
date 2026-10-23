"""Contain a noninteractive command before its first instruction executes.

Windows uses a private job and a blocking bootstrap: argv crosses an anonymous
pipe only after job assignment. POSIX uses a separate process group. No shell,
temporary argv file, environment mutation, or visible helper window is needed.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys


class WindowsJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOperations", "WriteOperations", "OtherOperations", "ReadBytes", "WriteBytes", "OtherBytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IO", Counters), ("ProcessMemory", ctypes.c_size_t),
                        ("JobMemory", ctypes.c_size_t), ("PeakProcessMemory", ctypes.c_size_t),
                        ("PeakJobMemory", ctypes.c_size_t)]
        self.limits = Extended()
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.set_kill_on_close(True)
        except BaseException:
            self.close()
            raise

    def checked(self, result):
        if not result:
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def set_kill_on_close(self, enabled):
        self.limits.Basic.LimitFlags = 0x2000 if enabled else 0
        self.checked(self.api.SetInformationJobObject(self.handle, 9,
                     self.ctypes.byref(self.limits), self.ctypes.sizeof(self.limits)))

    def assign(self, process):
        self.checked(self.api.AssignProcessToJobObject(self.handle, int(process._handle)))

    def terminate(self, number):
        self.checked(self.api.TerminateJobObject(self.handle, 128 + number))

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class Tree:
    def __init__(self, argv):
        self.job = None
        self.process = None
        if os.name != "nt":
            self.process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            start_new_session=True)
            self.scope = "posix-process-group"
            return
        import msvcrt
        self.job = WindowsJob()
        read_fd, write_fd = os.pipe()
        try:
            handle = msvcrt.get_osfhandle(read_fd)
            os.set_handle_inheritable(handle, True)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {"handle_list": [handle]}
            self.process = subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), str(handle)],
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            startupinfo=startup, close_fds=True,
                                            creationflags=subprocess.CREATE_NO_WINDOW)
            self.job.assign(self.process)
            # The helper cannot execute argv until assignment succeeded.
            data = memoryview(json.dumps(argv).encode("utf-8"))
            while data:
                data = data[os.write(write_fd, data):]
            self.scope = "windows-job"
        except BaseException:
            if self.process is not None:
                self.process.kill()
                self.process.communicate()
            self.job.close()
            raise
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def terminate(self, number, force=False):
        if self.job:
            self.job.terminate(number)
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL if force else number)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS reports EPERM, not ESRCH, when every process left in
                # the group is an unreaped zombie: the leader before
                # communicate() reaps it, or an orphaned descendant before
                # launchd does. Nothing is left to signal once the leader is gone.
                if self.process.poll() is None:
                    raise

    def close(self, normal=False):
        if self.job:
            try:
                if normal:
                    self.job.set_kill_on_close(False)
            finally:
                self.job.close()
        elif not normal:
            self.terminate(signal.SIGTERM, force=True)


def bootstrap(handle):
    import msvcrt
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    with os.fdopen(fd, "rb") as stream:
        argv = json.load(stream)
    try:
        return subprocess.call(argv, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    except FileNotFoundError:
        print("ork-meter: command executable not found", file=sys.stderr)
        return 127
    except OSError as exc:
        print("ork-meter: command capture failed (%s)" % exc.__class__.__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(bootstrap(int(sys.argv[1])))
