"""Machine-wide single GUI window guard, independent of the execution lock.

One named kernel mutex (Windows) or lock file (POSIX) admits exactly one
GachaOps graphical window per machine, regardless of data directory. Unlike
the execution lock, abandoned ownership is taken over: a crashed prior
window must never keep the next launch from opening. The guard is held in
the GUI process from before the window's startup gate until process exit;
automation launches (--smoke-test, layout previews) never take it, and
command-line/worker processes are never touched by it.
"""
import ctypes
import errno
import hashlib
import os
import sys
import time
from ctypes import wintypes as w
from pathlib import Path
import tempfile

# Overridable in tests: patch this name so a test never touches the
# product's guard while the user has GachaOps open.
GUARD_NAME = 'GachaOps.Window.v1'
# Elevation handoff: the old window closes itself right after launching the
# elevated replacement, but may not have exited yet. Wait — bounded — for
# the guard to be released instead of refusing the takeover.
HANDOFF_WAIT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.2
# A just-acquired guard may belong to a window that has not appeared yet
# (double-clicking twice in quick succession); wait on the same scale as
# the elevation handoff before telling the user to switch manually, and
# stay silent while waiting.
ACTIVATION_ATTEMPTS = 25
ACTIVATION_INTERVAL_SECONDS = 0.4
# The existing window is identified by its executable image and a title
# starting with GachaOps: the main window is 'GachaOps — 助手编排' and the
# startup-failure message box is 'GachaOps 无法打开'. The title condition
# also excludes unrelated windows when running from source (python.exe)
# and the app's own dropdown popups and tooltips.
ACTIVATION_TITLE_PREFIX = 'GachaOps'


class WindowGuard:
    """Kernel-wide guard held for the lifetime of one GUI window.

    Windows: a named mutex whose abandoned ownership is taken over — a
    crashed prior window is exactly the case the next launch must recover
    from. POSIX: a non-blocking flock on a lock file named after the guard;
    the OS releases it when the holder dies, which is the same contract.
    """

    def __init__(self, name=GUARD_NAME):
        self.name = name
        self.handle = None
        self.api = None
        self.stream = None

    def acquire(self):
        """Take the guard without waiting; False when it is held elsewhere."""
        if os.name == 'nt':
            self.api = ctypes.WinDLL('kernel32', use_last_error=True)
            signatures = {
                'CreateMutexW': ([ctypes.c_void_p, w.BOOL, w.LPCWSTR], w.HANDLE),
                'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
                'ReleaseMutex': ([w.HANDLE], w.BOOL),
                'CloseHandle': ([w.HANDLE], w.BOOL),
            }
            for key, (arguments, result) in signatures.items():
                getattr(self.api, key).argtypes = arguments
                getattr(self.api, key).restype = result
            self.handle = self.api.CreateMutexW(None, False, 'Global\\' + self.name)
            if not self.handle:
                return False
            result = self.api.WaitForSingleObject(self.handle, 0)
            # WAIT_OBJECT_0 or WAIT_ABANDONED: owned. Abandoned means the
            # previous window died holding it — exactly the case a relaunch
            # must recover from, so it is taken over, unlike the execution
            # lock which fails closed.
            return result in (0, 0x80)
        import fcntl
        digest = hashlib.sha256(self.name.encode()).hexdigest()
        path = Path(tempfile.gettempdir()) / ('gachaops-window-' + digest + '.lock')
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        self.stream = os.fdopen(os.open(path, flags, 0o600), 'r+')
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
        return True

    def release(self):
        if self.handle and self.api:
            self.api.ReleaseMutex(self.handle)
            self.api.CloseHandle(self.handle)
            self.handle = None
        if self.stream:
            self.stream.close()
            self.stream = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


def acquire_gui_guard(name=None, wait_seconds=0.0):
    """Take the machine-wide window guard.

    Returns (guard, reason). reason is 'acquired', 'busy' (another window
    holds it right now) or 'timeout' (still held after wait_seconds, used
    by the elevation handoff to bound its wait for the retiring window).
    The name resolves at call time so tests can patch GUARD_NAME.
    """
    if name is None:
        name = GUARD_NAME
    guard = WindowGuard(name)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        if guard.acquire():
            return guard, 'acquired'
        if time.monotonic() >= deadline:
            guard.release()
            return None, 'busy' if wait_seconds <= 0 else 'timeout'
        guard.release()
        time.sleep(POLL_INTERVAL_SECONDS)
        guard = WindowGuard(name)


def own_image_name():
    """The executable name this GUI runs as; frozen builds report the exe."""
    return os.path.normcase(os.path.basename(sys.executable))


def window_matches(image_name, title, own_image):
    """Whether a candidate top-level window is the existing GachaOps GUI:
    the executable image's file name equals our own (the enumeration
    reports a full image path, so compare basenames — the existing window
    may well come from a different install location) and the title starts
    with GachaOps. The title condition is what keeps an unrelated Python
    program's window (source runs have python.exe as their image) from
    being fronted, and excludes the app's own dropdown popups and
    tooltips, which carry other titles."""
    return (os.path.normcase(os.path.basename(image_name)) == os.path.normcase(own_image)
            and title.startswith(ACTIVATION_TITLE_PREFIX))


def _find_own_window():
    """The first visible top-level window of a same-image process, or 0.

    Enumeration only sees the calling session's desktop, so a window in
    another Windows session is simply not found and the caller falls back
    to telling the user. Windows: ctypes only, no synthetic input.
    """
    if os.name != 'nt':
        return 0
    mine = own_image_name()
    api = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    signatures = {
        'EnumWindows': ([callback_type, w.LPARAM], w.BOOL),
        'IsWindowVisible': ([w.HWND], w.BOOL),
        'GetWindowThreadProcessId': ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
        'GetWindowTextW': ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        getattr(api, name).argtypes = arguments
        getattr(api, name).restype = result
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR,
                                                  ctypes.POINTER(w.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    found = []

    @callback_type
    def collect(hwnd, _):
        if not api.IsWindowVisible(hwnd):
            return True
        pid = w.DWORD()
        api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        handle = kernel.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED
        if not handle:
            return True
        try:
            size = w.DWORD(1024)
            image = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                return True
        finally:
            kernel.CloseHandle(handle)
        title = ctypes.create_unicode_buffer(256)
        api.GetWindowTextW(hwnd, title, len(title))
        if window_matches(image.value, title.value, mine):
            found.append(int(hwnd))
        return True

    api.EnumWindows(collect, 0)
    return found[0] if found else 0


def activate_existing_window(attempts=ACTIVATION_ATTEMPTS,
                             interval=ACTIVATION_INTERVAL_SECONDS):
    """Restore and foreground the existing window, retrying briefly.

    The guard holder may still be between acquiring the guard and showing
    its window, so a few bounded retries precede the manual-switch report.
    Best effort: a foreground denial still reports success of the attempt
    because the caller has no second window to offer either way.
    """
    if os.name != 'nt':
        return False
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.IsIconic.argtypes = [w.HWND]
    api.IsIconic.restype = w.BOOL
    api.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
    api.ShowWindow.restype = w.BOOL
    api.SetForegroundWindow.argtypes = [w.HWND]
    api.SetForegroundWindow.restype = w.BOOL
    for _ in range(attempts):
        hwnd = _find_own_window()
        if hwnd:
            if api.IsIconic(hwnd):
                api.ShowWindow(hwnd, 9)  # SW_RESTORE; no synthetic input
            api.SetForegroundWindow(hwnd)
            return True
        time.sleep(interval)
    return False
