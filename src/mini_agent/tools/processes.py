"""Process-tree lifetime management for trusted local test suites."""

import os
import signal
import time


class PosixGroup:
    def attach_and_resume(self, pid):
        self.pid = pid

    def stop(self):
        if hasattr(self, "pid"):
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        pass


class WindowsJob:
    """Assign a suspended process before its Python/project code can run."""

    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.k = c.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, c.c_void_p], w.BOOL),
            "Thread32Next": ([w.HANDLE, c.c_void_p], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.k, name)
            function.argtypes, function.restype = args, result

        class BasicLimits(c.Structure):
            _fields_ = [("ProcessTime", c.c_longlong), ("JobTime", c.c_longlong),
                        ("Flags", w.DWORD), ("MinWorkingSet", c.c_size_t),
                        ("MaxWorkingSet", c.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", c.c_size_t), ("Priority", w.DWORD), ("Scheduling", w.DWORD)]

        class ExtendedLimits(c.Structure):
            _fields_ = [("Basic", BasicLimits), ("IO", c.c_ulonglong * 6),
                        ("ProcessMemory", c.c_size_t), ("JobMemory", c.c_size_t),
                        ("PeakProcessMemory", c.c_size_t), ("PeakJobMemory", c.c_size_t)]

        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.k.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def attach_and_resume(self, pid):
        c, w, k = self.c, self.w, self.k
        process = k.OpenProcess(0x101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not process:
            raise c.WinError(c.get_last_error())
        try:
            if not k.AssignProcessToJobObject(self.handle, process):
                raise c.WinError(c.get_last_error())
        finally:
            k.CloseHandle(process)

        class ThreadEntry(c.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("thread", w.DWORD),
                        ("owner", w.DWORD), ("base_priority", w.LONG),
                        ("delta_priority", w.LONG), ("flags", w.DWORD)]

        snapshot = k.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = c.sizeof(entry)
            found = []
            success = k.Thread32First(snapshot, c.byref(entry))
            while success:
                if entry.owner == pid:
                    found.append(entry.thread)
                entry.size = c.sizeof(entry)
                success = k.Thread32Next(snapshot, c.byref(entry))
            if len(found) != 1:
                raise OSError("Cannot identify suspended primary thread")
            thread = k.OpenThread(2, False, found[0])  # THREAD_SUSPEND_RESUME
            if not thread:
                raise c.WinError(c.get_last_error())
            try:
                if k.ResumeThread(thread) != 1:
                    raise OSError("Cannot resume suspended primary thread")
            finally:
                k.CloseHandle(thread)
        finally:
            k.CloseHandle(snapshot)

    def stop(self):
        c, w = self.c, self.w
        if not self.k.TerminateJobObject(self.handle, 1):
            raise c.WinError(c.get_last_error())

        class Accounting(c.Structure):
            _fields_ = [("times", c.c_longlong * 4), ("faults", w.DWORD),
                        ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]

        deadline = time.monotonic() + 5
        while True:
            info = Accounting()
            if not self.k.QueryInformationJobObject(self.handle, 1, c.byref(info), c.sizeof(info), None):
                raise c.WinError(c.get_last_error())
            if info.active == 0:
                return
            if time.monotonic() >= deadline:
                raise OSError("Job processes did not terminate")
            time.sleep(0.01)

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None
