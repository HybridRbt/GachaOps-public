"""Nonblocking, host-wide admission lock, independent of run output directories.

Windows uses a named kernel mutex across sessions. Access denial and abandoned
ownership fail closed. This coordinates GachaOps, not independently run tools.
"""
import ctypes
from ctypes import wintypes as w
import hashlib
import os
from pathlib import Path
import tempfile
import threading


class ExecutionBusy(RuntimeError):
    pass


_guard = threading.Lock()
_held = set()


class ExecutionLock:
    def __init__(self, name="GachaOps.Execution.v1"):
        self.name = name
        self.handle = None
        self.stream = None
        self.owner_thread = None

    def __enter__(self):
        with _guard:
            if self.name in _held:
                raise ExecutionBusy("another GachaOps chain owns the execution lock")
            _held.add(self.name)
        try:
            if os.name == "nt":
                self.api = ctypes.WinDLL("kernel32", use_last_error=True)
                signatures = {
                    "CreateMutexW": ([ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE),
                    "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
                    "ReleaseMutex": ([w.HANDLE], w.BOOL),
                    "CloseHandle": ([w.HANDLE], w.BOOL),
                }
                for key, (args, result) in signatures.items():
                    getattr(self.api, key).argtypes = args
                    getattr(self.api, key).restype = result
                self.handle = self.api.CreateMutexW(None, False, "Global\\" + self.name)
                if not self.handle:
                    raise ExecutionBusy("execution mutex inaccessible; inspect permissions")
                result = self.api.WaitForSingleObject(self.handle, 0)
                if result == 0x80:  # WAIT_ABANDONED: we own it, but cannot assume cleanup.
                    self.api.ReleaseMutex(self.handle)
                    raise ExecutionBusy("abandoned execution mutex; inspect previous processes before retry")
                if result != 0:
                    raise ExecutionBusy("another chain owns the execution mutex, or mutex wait failed")
                self.owner_thread = threading.get_ident()
            else:
                import fcntl
                digest = hashlib.sha256(self.name.encode()).hexdigest()
                path = Path(tempfile.gettempdir()) / ("gachaops-execution-" + digest + ".lock")
                flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                self.stream = os.fdopen(os.open(path, flags, 0o600), "r+")
                try:
                    fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ExecutionBusy("another chain owns the execution file lock") from exc
            return self
        except BaseException:
            self._dispose()
            with _guard:
                _held.discard(self.name)
            raise

    def _dispose(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None
        if self.stream:
            self.stream.close()
            self.stream = None

    def __exit__(self, *_):
        if self.owner_thread is not None and self.owner_thread != threading.get_ident():
            raise RuntimeError("execution mutex must be released by its owning thread")
        try:
            if self.handle and not self.api.ReleaseMutex(self.handle):
                raise RuntimeError("execution mutex release failed")
        finally:
            self.owner_thread = None
            self._dispose()
            with _guard:
                _held.discard(self.name)
