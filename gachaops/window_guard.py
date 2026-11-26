"""Single GUI window guard, independent of the execution lock.

A named kernel mutex (Windows) or a lock file under the system temp
directory (POSIX) admits exactly one GachaOps graphical window,
regardless of data directory. The scope is stated as it really is: the
Windows mutex is machine-global (Global\\), while the POSIX lock file
lives in a per-user directory (gachaops-window-<uid>, created 0700), so
the guard holds per machine on Windows and per user on Linux and macOS —
a shared 0600 lock file would stay unopenable for the next user after
the first window exits, and the failed open would read as "already
running", locking that user out of their own window. Unlike the
execution lock, abandoned ownership is taken over: a crashed prior
window must never keep the next launch from opening. The guard is held
in the GUI process from before the window's startup gate until process
exit; automation launches (--smoke-test, layout previews) never take
it, and command-line/worker processes are never touched by it.
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
# window_matches decides how the existing window is recognized: frozen
# builds (GachaOps.exe) match on the image alone — no lookalike problem,
# so startup-gate dialogs with other titles can be fronted too — while
# source runs (python.exe is shared) additionally require the title to
# start with GachaOps. This prefix feeds that title condition.
ACTIVATION_TITLE_PREFIX = 'GachaOps'


def posix_lock_path(name):
    """The per-user lock file path for a guard name on POSIX.

    The file lives under tempfile.gettempdir() inside a per-user
    directory gachaops-window-<uid> (created 0700; no subdirectory where
    os.getuid() does not exist). The file name itself stays derived from
    the guard name. Tests use this helper to compute the same path.
    """
    digest = hashlib.sha256(name.encode()).hexdigest()
    parent = Path(tempfile.gettempdir())
    getuid = getattr(os, 'getuid', None)
    if getuid is not None:
        parent = parent / ('gachaops-window-' + str(getuid()))
    return parent / ('gachaops-window-' + digest + '.lock')


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
        # The raw WaitForSingleObject result of the last acquisition (None
        # on POSIX and before the first acquire). Recorded so a test can
        # assert the abandoned takeover without making its own raw wait —
        # which would hand ownership to the waiting thread and let a guard
        # that refuses WAIT_ABANDONED slip through same-thread reentrancy.
        self.wait_result = None

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
                self.wait_result = None
                return False
            result = self.api.WaitForSingleObject(self.handle, 0)
            self.wait_result = result
            # WAIT_OBJECT_0 or WAIT_ABANDONED: owned. Abandoned means the
            # previous window died holding it — exactly the case a relaunch
            # must recover from, so it is taken over, unlike the execution
            # lock which fails closed.
            return result in (0, 0x80)
        self.wait_result = None
        import fcntl
        path = posix_lock_path(self.name)
        if path.parent.is_symlink():
            # A planted symlink at the per-user directory would redirect
            # the lock file entirely: refuse instead of following it.
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except PermissionError:
            # The temp area is not writable for us: without our own lock
            # file we cannot own the window either.
            return False
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        try:
            self.stream = os.fdopen(os.open(path, flags, 0o600), 'r+')
        except OSError as error:
            # The lock file lives in a shared temp directory: a second user
            # hits PermissionError on the 0600 file, and a planted symlink
            # trips O_NOFOLLOW (ELOOP). Neither is our own window holding
            # the guard, so both read as "not acquired" instead of raising.
            if isinstance(error, PermissionError) or error.errno == errno.ELOOP:
                return False
            raise
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                self.stream.close()
                self.stream = None
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


def acquire_gui_guard(name=None, wait_seconds=0.0):
    """Take the machine-wide window guard.

    Returns the acquired guard, or None when the machine already has a
    window. With wait_seconds above zero (the elevation handoff) it polls
    for the retiring window to release for up to that long before giving
    up. The name resolves at call time so tests can patch GUARD_NAME.
    """
    if name is None:
        name = GUARD_NAME
    guard = WindowGuard(name)
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        if guard.acquire():
            return guard
        if time.monotonic() >= deadline:
            guard.release()
            return None
        guard.release()
        time.sleep(POLL_INTERVAL_SECONDS)
        guard = WindowGuard(name)


def own_image_name():
    """The executable name this GUI runs as; frozen builds report the exe."""
    return os.path.normcase(os.path.basename(sys.executable))


def window_matches(image_name, title, own_image, frozen=False):
    """Whether a candidate top-level window is the existing GachaOps GUI.

    The executable image's file name must equal our own (the enumeration
    reports a full image path, so compare basenames — the existing window
    may well come from a different install location). Frozen builds match
    on the image alone: GachaOps.exe has no lookalike problem, so the
    startup-gate dialogs (「继续迁移 / 继续导入」「无法准备迁移」 and the
    like, whose titles do not start with GachaOps) can be fronted while
    the main window is still hidden. Source runs additionally require the
    title prefix, because there the image is python.exe and another
    program's window must not be fronted — and excludes the app's own
    dropdown popups and tooltips. Known trade-off: a source run's
    startup-gate dialogs are therefore not found by the second launch.
    Windows with an empty title are never candidates: the main window and
    every dialog carry a title, while dropdowns, tooltips and IME layers
    do not — and an untitled popup at the top of the z-order would fail
    the foreground check and hide the frontable window behind it.
    """
    if os.path.normcase(os.path.basename(image_name)) != os.path.normcase(own_image):
        return False
    if not title:
        return False
    return frozen or title.startswith(ACTIVATION_TITLE_PREFIX)


def _find_own_window():
    """Visible top-level windows of same-image processes, in z-order.

    Enumeration reports windows topmost first, so the list starts with
    what a second launch would most plausibly want; the caller tries
    them front to back until one actually takes the foreground.
    Enumeration only sees the calling session's desktop, so a window in
    another Windows session is simply not found and the caller falls
    back to telling the user. Windows: ctypes only, no synthetic input.
    """
    if os.name != 'nt':
        return []
    mine = own_image_name()
    frozen = bool(getattr(sys, 'frozen', False))
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
        if window_matches(image.value, title.value, mine, frozen):
            found.append(int(hwnd))
        return True

    api.EnumWindows(collect, 0)
    return found


def _bring_to_front(api, hwnd):
    """Restore the window if minimized, set it foreground, and return
    whether it really sits in the foreground afterwards — a denial reads
    as False so the caller can point the user at the taskbar instead of
    exiting silently. The foreground window may be a modal dialog owned
    by the target (SetForegroundWindow on a disabled owner activates the
    dialog), so the check compares root owners rather than exact hwnd."""
    if api.IsIconic(hwnd):
        api.ShowWindow(hwnd, 9)  # SW_RESTORE; no synthetic input
    api.SetForegroundWindow(hwnd)
    foreground = api.GetForegroundWindow()
    root_owner = 3  # GA_ROOTOWNER
    return api.GetAncestor(foreground, root_owner) == api.GetAncestor(hwnd, root_owner)


def _user32_activation_api():
    """The user32 handle for activation, with declarations filled in."""
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.IsIconic.argtypes = [w.HWND]
    api.IsIconic.restype = w.BOOL
    api.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
    api.ShowWindow.restype = w.BOOL
    api.SetForegroundWindow.argtypes = [w.HWND]
    api.SetForegroundWindow.restype = w.BOOL
    api.GetForegroundWindow.argtypes = []
    api.GetForegroundWindow.restype = w.HWND
    api.GetAncestor.argtypes = [w.HWND, w.UINT]
    api.GetAncestor.restype = w.HWND
    return api


def activate_existing_window(attempts=ACTIVATION_ATTEMPTS,
                             interval=ACTIVATION_INTERVAL_SECONDS, api=None):
    """Restore and foreground the existing window, retrying briefly.

    Returns whether a matching window was found and actually sits in the
    foreground now. The guard holder may still be between acquiring the
    guard and showing its window, so a few bounded retries precede the
    manual-switch report.
    """
    if api is None:
        # Only the real activation needs the platform API; an injected
        # fake (tests) runs the same candidate walk on every platform.
        if os.name != 'nt':
            return False
        api = _user32_activation_api()
    for _ in range(attempts):
        # Candidates are z-ordered topmost-first; the topmost one may not
        # actually take the foreground (a popup being torn down, a layered
        # window), so walk the list until the check passes.
        for hwnd in _find_own_window():
            if _bring_to_front(api, hwnd):
                return True
        time.sleep(interval)
    return False
