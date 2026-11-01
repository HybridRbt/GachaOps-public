"""Owned Windows process tree: assign a suspended root before any user code runs.

No shell, process-name killing, privilege changes or breakaway flags. Children
started through other services/launchers are outside this ownership boundary.
"""
import ctypes
from ctypes import wintypes as w
import math
import os
import subprocess
import time


class CleanupError(RuntimeError):
    pass


class STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
                ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
                ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", ctypes.POINTER(w.BYTE)), ("hStdInput", w.HANDLE),
                ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class ACCOUNTING(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in
                ("TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
    _fields_ += [(name, w.DWORD) for name in
                 ("TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


def kernel_api():
    if os.name != "nt":
        raise OSError("Windows process ownership requires Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
        "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
        "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL,
                            w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(STARTUPINFO),
                            ctypes.POINTER(PROCESS_INFORMATION)], w.BOOL),
        "ResumeThread": ([w.HANDLE], w.DWORD),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
        "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
        "CloseHandle": ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


def environment_block(env):
    if env is None:
        return None
    if not isinstance(env, dict):
        raise ValueError("environment must be a dictionary")
    seen = set()
    for key, value in env.items():
        if (not isinstance(key, str) or not key or "=" in key or "\0" in key
                or not isinstance(value, str) or "\0" in value or key.upper() in seen):
            raise ValueError("invalid or duplicate environment variable")
        seen.add(key.upper())
    # Explicit double terminator, including the empty environment case.
    return ctypes.create_unicode_buffer("\0".join(f"{key}={env[key]}" for key in sorted(env, key=str.upper)) + "\0\0")


class WindowsProcess:
    """A root plus its inherited Job Object descendants. Always use a context."""

    def __init__(self, argv, cwd=None, env=None, additional_roots=()):
        if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
            raise ValueError("argv must contain non-NUL strings")
        if not os.path.isabs(argv[0]):
            raise ValueError("executable must be an absolute path")
        child_environment = environment_block(env)
        self.api = kernel_api()
        self.job = None
        self.process = None
        self.pid = None
        self._closed = False
        thread = None
        assigned = False
        try:
            self.job = checked(self.api.CreateJobObjectW(None, None))
            limits = EXTENDED_LIMIT()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            checked(self.api.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            startup = STARTUPINFO()
            startup.cb = ctypes.sizeof(startup)
            info = PROCESS_INFORMATION()
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            flags = 0x4 | 0x08000000 | (0x400 if child_environment is not None else 0)
            checked(self.api.CreateProcessW(argv[0], command, None, None, False,
                                           flags, child_environment, cwd, ctypes.byref(startup), ctypes.byref(info)))
            self.process, thread, self.pid = info.hProcess, info.hThread, info.dwProcessId
            checked(self.api.AssignProcessToJobObject(self.job, self.process))
            assigned = True
            # Official launchers must be owned before the assistant can reuse
            # them. Both roots enter the same Job before any assistant code runs.
            for member in additional_roots:
                self.launch_member(member, env=env)
            if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException as original:
            # Also covers assignment failure: root is still suspended and cannot spawn.
            cleanup_problem = None
            try:
                if assigned:
                    try:
                        self.terminate_tree()
                    except (OSError, CleanupError) as exc:
                        cleanup_problem = CleanupError(f'cannot verify startup Job cleanup: {exc}')
                if self.process and not assigned:
                    if not self.api.TerminateProcess(self.process, 1) or self.api.WaitForSingleObject(self.process, 5000) != 0:
                        cleanup_problem = CleanupError(f"cannot verify suspended root cleanup, pid={self.pid}")
            finally:
                if self.process:
                    self.api.CloseHandle(self.process)
                    self.process = None
                if self.job:
                    self.api.CloseHandle(self.job)
                    self.job = None
            if cleanup_problem:
                raise cleanup_problem from original
            raise
        finally:
            if thread:
                self.api.CloseHandle(thread)

    def launch_member(self, argv, env=None):
        if not argv or not os.path.isabs(argv[0]) or any(not isinstance(a, str) or '\0' in a for a in argv):
            raise ValueError('member executable must be absolute, with non-NUL arguments')
        startup, info = STARTUPINFO(), PROCESS_INFORMATION()
        startup.cb = ctypes.sizeof(startup)
        block = environment_block(env)
        checked(self.api.CreateProcessW(argv[0], ctypes.create_unicode_buffer(subprocess.list2cmdline(argv)),
                                       None, None, False, 0x4 | (0x400 if block is not None else 0), block,
                                       os.path.dirname(argv[0]), ctypes.byref(startup), ctypes.byref(info)))
        try:
            checked(self.api.AssignProcessToJobObject(self.job, info.hProcess))
            if self.api.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            if not self.api.TerminateProcess(info.hProcess, 1) or self.api.WaitForSingleObject(info.hProcess, 5000) != 0:
                raise CleanupError('cannot verify suspended launcher cleanup')
            raise
        finally:
            self.api.CloseHandle(info.hThread)
            self.api.CloseHandle(info.hProcess)

    def wait(self, timeout):
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("finite nonnegative timeout required")
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0, deadline - time.monotonic())
            # Return to Python regularly so Ctrl+C can enter context cleanup.
            result = self.api.WaitForSingleObject(self.process, min(math.ceil(remaining * 1000), 100))
            if result == 0:
                break
            if result != 0x102:
                raise ctypes.WinError(ctypes.get_last_error())
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
        code = w.DWORD()
        checked(self.api.GetExitCodeProcess(self.process, ctypes.byref(code)))
        return code.value

    def active_processes(self):
        accounting = ACCOUNTING()
        checked(self.api.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting),
                                                  ctypes.sizeof(accounting), None))
        return accounting.ActiveProcesses

    def terminate_tree(self, timeout=5):
        checked(self.api.TerminateJobObject(self.job, 1))
        deadline = time.monotonic() + timeout
        while self.active_processes():
            if time.monotonic() >= deadline:
                raise CleanupError("owned Job Object still has active processes; stop chain")
            time.sleep(0.01)

    def close(self):
        if self._closed:
            return
        try:
            self.terminate_tree()
        except OSError as exc:
            raise CleanupError("cannot verify owned tree cleanup; stop chain") from exc
        finally:
            # Kernel kill-on-close remains a final backstop on cleanup/query failure.
            self.api.CloseHandle(self.process)
            self.api.CloseHandle(self.job)
            self.process = self.job = None
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
