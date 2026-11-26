"""Machine-wide single window guard: acquisition, activation, handoff wait.

Process-level tests use a real uniquely-named guard held by a helper child
process (lock holding only, no windows), so same-thread mutex reentrancy
cannot mask a regression; the product's default guard name is never
touched, and every desktop.main() test replaces the message boxes so a
behavioral regression fails fast instead of hanging on a modal dialog.

The desktop.launch paths live in DesktopLaunchTests, skipped wherever the
optional Qt stack is absent (Linux/macOS CI installs no desktop
dependencies) — the same gate the rest of the desktop suite uses. The
pure guard and matching tests stay in WindowGuardTests and run on every
platform.
"""
import ctypes
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from ctypes import wintypes as w
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

TREE = Path(__file__).resolve().parents[1]

CHILD_TEMPLATE = """import os, sys, time
sys.path.insert(0, {tree!r})
from gachaops import window_guard
guard = window_guard.WindowGuard(sys.argv[1])
held = guard.acquire()
print('HELD' if held else 'BUSY', flush=True)
if held and sys.argv[3] == 'exit-held':
    os._exit(3)  # die holding the guard: abandoned ownership
time.sleep(float(sys.argv[2]))
guard.release()
print('RELEASED', flush=True)
"""


def unique_name():
    return 'GachaOps.Window.test-' + uuid.uuid4().hex


def hold_in_child(name, seconds, mode='release'):
    """Hold the named guard in a helper child process; no window is shown.

    The first stdout line reports whether the child took the guard
    ('HELD') or found it already taken ('BUSY').
    """
    directory = Path(tempfile.mkdtemp(prefix='window-guard-child-'))
    script = directory / 'holder.py'
    script.write_text(CHILD_TEMPLATE.format(tree=str(TREE)), encoding='utf-8')
    child = subprocess.Popen([sys.executable, '-B', str(script), name, str(seconds), mode],
                             stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline().strip()
    if line not in ('HELD', 'BUSY'):
        child.kill()
        child.stdout.close()
        raise AssertionError('child produced no guard report: ' + line)
    return child, line


def stop_child(child):
    """Terminate a still-running holder and release its pipes."""
    try:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
    finally:
        if child.stdout and not child.stdout.closed:
            child.stdout.close()


def open_witness(name):
    """Open (never own) the named mutex on Windows so the kernel object
    outlives any handle lifecycle bug in release(): without a witness, a
    release that drops only ReleaseMutex but keeps CloseHandle destroys
    the owned object along with the last handle, and no other process
    can observe the leaked ownership. Returns (api, handle), or None off
    Windows."""
    if os.name != 'nt':
        return None
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
    api.CreateMutexW.restype = w.HANDLE
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    handle = api.CreateMutexW(None, False, 'Global\\' + name)
    if not handle:
        raise AssertionError('witness mutex creation failed')
    return api, handle


def handoff_document():
    from gachaops.desktop_model import write_handoff
    from gachaops.profiles import default_profile
    with tempfile.TemporaryDirectory() as scratch:
        document = write_handoff(Path(scratch) / 'handoff.json', None, None, 1,
                                 default_profile())
    return json.dumps(document, ensure_ascii=False)


class WindowGuardTests(unittest.TestCase):
    def test_window_match_requires_own_image_and_gachaops_title(self):
        from gachaops import window_guard
        # Platform-native absolute paths: the caller passes the full image
        # path enumeration reports, and only the file name has to equal
        # ours, from any install location.
        image = os.path.join(os.sep, 'Apps', 'GachaOps', 'GachaOps.exe')
        own = os.path.basename(image)
        elsewhere = os.path.join(os.sep, 'Elsewhere', 'GachaOps', 'GachaOps.exe')
        match = window_guard.window_matches
        self.assertTrue(match(image, 'GachaOps — 助手编排', own))
        self.assertTrue(match(elsewhere, 'GachaOps 无法打开', own))
        # Frozen builds match on the image alone: the startup-gate dialogs
        # (「继续迁移」「无法准备迁移」…) do not start with GachaOps and are
        # exactly the windows a second launch must be able to front.
        self.assertTrue(match(image, '继续迁移 / 继续导入', own, frozen=True))
        self.assertTrue(match(image, '任意标题', own, frozen=True))
        # Source runs keep the title condition: python.exe is shared, and
        # the app's own popups carry other titles.
        self.assertFalse(match(os.path.join(os.sep, 'Tools', 'python.exe'),
                               'GachaOps — 助手编排', own))
        self.assertFalse(match(image, '查看原因', own))
        self.assertFalse(match(image, '继续迁移 / 继续导入', own))
        self.assertFalse(match(image, '', own))
        self.assertFalse(match(image, 'some other tool', own))
        # An image whose file name differs is refused, frozen or not.
        self.assertFalse(match(os.path.join(os.sep, 'Tools', 'gachaops2.exe'),
                               '任意标题', own, frozen=True))

    @unittest.skipUnless(os.name == 'nt',
                         'normcase folds file-name case on Windows only')
    def test_window_match_folds_case_on_windows(self):
        from gachaops import window_guard
        match = window_guard.window_matches
        # os.path.normcase lowercases on Windows, so the enumeration's
        # differently-cased name of the same image still matches ours.
        self.assertTrue(match(r'D:\Elsewhere\GachaOps\GACHAOPS.EXE',
                              'GachaOps 无法打开', 'gachaops.exe'))
        self.assertFalse(match(r'C:\Tools\GACHAOPS2.EXE', '任意标题',
                               'gachaops.exe', frozen=True))

    def test_find_own_window_matches_by_real_enumeration(self):
        # Real enumeration against two zero-size offscreen top-level windows
        # created in this process (predefined STATIC class, WS_POPUP visible,
        # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW so a run inside a foreground-
        # privileged terminal never steals focus briefly, parked at
        # -32000,-32000): nothing is visible on the user's desktop and both
        # are destroyed before the test returns.
        if os.name != 'nt':
            self.skipTest('Windows window enumeration')
        api = ctypes.WinDLL('user32', use_last_error=True)
        api.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, w.HWND, w.HANDLE, w.HINSTANCE,
                                        w.LPVOID]
        api.CreateWindowExW.restype = w.HWND
        api.DestroyWindow.argtypes = [w.HWND]
        api.DestroyWindow.restype = w.BOOL
        noactivate_tool = 0x08000080  # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        popup_visible = 0x90000000  # WS_POPUP | WS_VISIBLE
        gachaops_hwnd = api.CreateWindowExW(
            noactivate_tool, 'STATIC', 'GachaOps — 助手编排 (window find probe)',
            popup_visible, -32000, -32000, 0, 0, None, None, None, None)
        # Created later, so it sits on top of the z-order; the candidate
        # list must preserve that order (topmost first).
        topmost_hwnd = api.CreateWindowExW(
            noactivate_tool, 'STATIC', 'GachaOps — 助手编排 (window find probe 2)',
            popup_visible, -32000, -32000, 0, 0, None, None, None, None)
        other_hwnd = api.CreateWindowExW(
            noactivate_tool, 'STATIC', 'some other tool',
            popup_visible, -32000, -32000, 0, 0, None, None, None, None)
        self.assertTrue(gachaops_hwnd and topmost_hwnd and other_hwnd)
        try:
            from gachaops import window_guard
            found = window_guard._find_own_window()
            self.assertEqual(found, [int(topmost_hwnd), int(gachaops_hwnd)])
            self.assertNotIn(int(other_hwnd), found)
        finally:
            api.DestroyWindow(gachaops_hwnd)
            api.DestroyWindow(topmost_hwnd)
            api.DestroyWindow(other_hwnd)

    def test_frozen_enumeration_includes_gate_dialogs_and_skips_untitled(self):
        # Frozen builds must find windows whose title does not start with
        # GachaOps — the startup-gate dialogs are the only visible windows
        # while the main window is still hidden — but an untitled popup is
        # never a candidate: topmost in the z-order it would fail the
        # foreground check and hide the frontable window behind it.
        if os.name != 'nt':
            self.skipTest('Windows window enumeration')
        api = ctypes.WinDLL('user32', use_last_error=True)
        api.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, w.HWND, w.HANDLE, w.HINSTANCE,
                                        w.LPVOID]
        api.CreateWindowExW.restype = w.HWND
        api.DestroyWindow.argtypes = [w.HWND]
        api.DestroyWindow.restype = w.BOOL
        noactivate_tool = 0x08000080  # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
        popup_visible = 0x90000000  # WS_POPUP | WS_VISIBLE
        gate_hwnd = api.CreateWindowExW(
            noactivate_tool, 'STATIC', '继续迁移 / 继续导入',
            popup_visible, -32000, -32000, 0, 0, None, None, None, None)
        untitled_hwnd = api.CreateWindowExW(
            noactivate_tool, 'STATIC', '',
            popup_visible, -32000, -32000, 0, 0, None, None, None, None)
        self.assertTrue(gate_hwnd and untitled_hwnd)
        try:
            from gachaops import window_guard
            with patch.object(sys, 'frozen', True, create=True):
                found = window_guard._find_own_window()
            self.assertIn(int(gate_hwnd), found)
            self.assertNotIn(int(untitled_hwnd), found)
        finally:
            api.DestroyWindow(gate_hwnd)
            api.DestroyWindow(untitled_hwnd)

    def test_activation_wait_is_bounded_on_the_handoff_scale(self):
        from gachaops import window_guard

        class SilentApi:
            def IsIconic(self, hwnd): return False
            def ShowWindow(self, hwnd, command): pass
            def SetForegroundWindow(self, hwnd): pass
            def GetForegroundWindow(self): return 0
            def GetAncestor(self, hwnd, flag): return hwnd

        sleeps = []
        with patch.object(window_guard, '_find_own_window', return_value=[]), \
             patch.object(window_guard.time, 'sleep',
                          side_effect=lambda seconds: sleeps.append(seconds)):
            self.assertFalse(window_guard.activate_existing_window(api=SilentApi()))
        expected = window_guard.ACTIVATION_ATTEMPTS * window_guard.ACTIVATION_INTERVAL_SECONDS
        self.assertAlmostEqual(sum(sleeps), expected, places=6)
        self.assertEqual(len(sleeps), window_guard.ACTIVATION_ATTEMPTS)
        # The activation wait stays on the same scale as the handoff wait:
        # quick double-clicks must not turn into the "already running"
        # message before the first window has even appeared.
        self.assertAlmostEqual(expected, window_guard.HANDOFF_WAIT_SECONDS, delta=0.01)

    def test_window_guard_abandoned_ownership_is_taken_over(self):
        from gachaops import window_guard
        name = unique_name()
        # A witness handle keeps the named object alive after the child
        # dies holding it: on Windows the next acquisition then observes
        # WAIT_ABANDONED, which the guard must take over (a crashed prior
        # window is exactly the case a relaunch must recover from).
        witness = None
        if os.name == 'nt':
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
            api.CreateMutexW.restype = w.HANDLE
            api.CloseHandle.argtypes = [w.HANDLE]
            api.CloseHandle.restype = w.BOOL
            witness = api.CreateMutexW(None, False, 'Global\\' + name)
            self.assertTrue(witness)
            self.addCleanup(api.CloseHandle, witness)
        child, held = hold_in_child(name, '0', mode='exit-held')
        self.assertEqual(held, 'HELD')
        self.addCleanup(stop_child, child)
        child.wait(timeout=10)
        self.assertEqual(child.returncode, 3)
        # Take over from this thread and read the recorded result — the
        # guard's own observation, not a raw wait here: a raw one would
        # hand ownership to the test thread and let a guard that refuses
        # WAIT_ABANDONED slip through same-thread reentrancy.
        guard = window_guard.WindowGuard(name)
        self.assertTrue(guard.acquire())
        if os.name == 'nt':
            self.assertEqual(guard.wait_result, 0x80)
        guard.release()
        # And the name stays usable after a clean release: a properly
        # released mutex is no longer abandoned, so this acquire is a
        # plain take — only the result being owned matters here.
        again = window_guard.WindowGuard(name)
        self.assertTrue(again.acquire())
        if os.name == 'nt':
            self.assertEqual(again.wait_result, 0)
        again.release()

    def test_bring_to_front_restores_and_reports_real_foreground(self):
        from gachaops import window_guard
        hwnd = 4321

        class FakeApi:
            def __init__(self, iconic, foreground, roots=None):
                self.iconic = iconic
                self.foreground = foreground
                self.roots = roots or {}
                self.calls = []

            def IsIconic(self, hwnd):
                self.calls.append('IsIconic')
                return self.iconic

            def ShowWindow(self, hwnd, command):
                self.calls.append(('ShowWindow', command))
                return True

            def SetForegroundWindow(self, hwnd):
                self.calls.append('SetForegroundWindow')
                return True

            def GetForegroundWindow(self):
                self.calls.append('GetForegroundWindow')
                return self.foreground

            def GetAncestor(self, hwnd, flag):
                self.calls.append(('GetAncestor', hwnd, flag))
                # Unmapped windows are unowned top-levels: their own root.
                return self.roots.get(hwnd, hwnd)

        # Minimized: restored with SW_RESTORE (9), then set foreground, and
        # the real foreground read confirms the switch.
        restored = FakeApi(iconic=True, foreground=hwnd)
        self.assertTrue(window_guard._bring_to_front(restored, hwnd))
        self.assertIn(('ShowWindow', 9), restored.calls)
        self.assertIn('SetForegroundWindow', restored.calls)
        # Already on screen: foregrounded without a restore call.
        onscreen = FakeApi(iconic=False, foreground=hwnd)
        self.assertTrue(window_guard._bring_to_front(onscreen, hwnd))
        self.assertNotIn(('ShowWindow', 9), onscreen.calls)
        # The switch was denied — another window holds the foreground — so
        # the result reads False and the caller points at the taskbar.
        denied = FakeApi(iconic=False, foreground=9999)
        self.assertFalse(window_guard._bring_to_front(denied, hwnd))
        # SetForegroundWindow on a disabled owner activates its modal
        # dialog instead: the foreground hwnd differs from the target but
        # shares its root owner, which still counts as switched.
        dialog = 555
        modal = FakeApi(iconic=False, foreground=dialog,
                        roots={dialog: hwnd, hwnd: hwnd})
        self.assertTrue(window_guard._bring_to_front(modal, hwnd))

    def test_activation_tries_candidates_in_z_order_until_one_fronts(self):
        from gachaops import window_guard

        class FakeApi:
            def __init__(self, foreground, roots):
                self.foreground = foreground
                self.roots = roots
                self.fronted = []

            def IsIconic(self, hwnd):
                return False

            def ShowWindow(self, hwnd, command):
                pass

            def SetForegroundWindow(self, hwnd):
                self.fronted.append(hwnd)
                return True

            def GetForegroundWindow(self):
                return self.foreground

            def GetAncestor(self, hwnd, flag):
                return self.roots.get(hwnd, hwnd)

        # Z-order topmost-first: the topmost candidate fronts
        # unsuccessfully while the window behind it is frontable, so both
        # are tried in order and the second one decides the result.
        api = FakeApi(333, {333: 222, 111: 111, 222: 222})
        with patch.object(window_guard, '_find_own_window',
                          return_value=[111, 222]), \
             patch.object(window_guard.time, 'sleep', lambda seconds: None):
            self.assertTrue(window_guard.activate_existing_window(
                attempts=1, api=api))
        self.assertEqual(api.fronted, [111, 222])
        # Every candidate refusing the foreground reads as failure — the
        # caller then tells the user to switch manually.
        api = FakeApi(444, {444: 444, 111: 111, 222: 222})
        with patch.object(window_guard, '_find_own_window',
                          return_value=[111, 222]), \
             patch.object(window_guard.time, 'sleep', lambda seconds: None):
            self.assertFalse(window_guard.activate_existing_window(
                attempts=1, api=api))
        self.assertEqual(api.fronted, [111, 222])

    def test_acquire_with_empty_mutex_handle_never_waits(self):
        # CreateMutexW can fail; the guard must report not-acquired without
        # touching the dead handle — no WaitForSingleObject, and the raw
        # wait result stays unset.
        if os.name != 'nt':
            self.skipTest('Windows mutex branch')
        from gachaops import window_guard

        class FakeKernel:
            def __init__(self):
                self.waits = []
                def create(*args):
                    return None
                def wait(*args):
                    self.waits.append(args)
                    return 0
                def accept(*args):
                    return True
                # Plain functions: acquire declares argtypes/restype on
                # them, which bound methods would refuse.
                self.CreateMutexW = create
                self.WaitForSingleObject = wait
                self.ReleaseMutex = accept
                self.CloseHandle = accept

        fake = FakeKernel()
        with patch.object(window_guard.ctypes, 'WinDLL', return_value=fake):
            guard = window_guard.WindowGuard(unique_name())
            self.assertFalse(guard.acquire())
        self.assertIsNone(guard.wait_result)
        self.assertEqual(fake.waits, [])
        guard.release()

    def test_posix_permission_error_reads_as_not_acquired(self):
        # The temp area is not writable for us: without our own lock file
        # we cannot own the window — read as not acquired, never raise.
        if os.name == 'nt':
            self.skipTest('POSIX flock branch')
        from gachaops import window_guard
        guard = window_guard.WindowGuard(unique_name())
        with patch.object(window_guard.os, 'open',
                          side_effect=PermissionError(13, 'fixture')):
            self.assertFalse(guard.acquire())
        self.assertIsNone(guard.wait_result)
        self.assertIsNone(guard.stream)

    def test_posix_symlinked_lock_file_reads_as_not_acquired(self):
        # A planted symlink at the lock path trips O_NOFOLLOW (ELOOP):
        # neither a real holder nor ours — not acquired, never followed.
        if os.name == 'nt':
            self.skipTest('POSIX flock branch')
        from gachaops import window_guard
        name = unique_name()
        lock = window_guard.posix_lock_path(name)
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = lock.with_name(lock.name + '.fixture-target')
        target.write_text('x', encoding='utf-8')
        os.symlink(target, lock)

        def cleanup():
            if lock.is_symlink():
                lock.unlink()
            if target.exists():
                target.unlink()

        self.addCleanup(cleanup)
        guard = window_guard.WindowGuard(name)
        self.assertFalse(guard.acquire())
        self.assertIsNone(guard.stream)

    def test_posix_held_flock_reads_as_not_acquired(self):
        # flock conflicts across open file descriptions even inside one
        # process, so a second description holding LOCK_EX reproduces
        # another holder (EAGAIN) without a helper child.
        if os.name == 'nt':
            self.skipTest('POSIX flock branch')
        import fcntl
        from gachaops import window_guard
        name = unique_name()
        lock = window_guard.posix_lock_path(name)
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        holder = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            guard = window_guard.WindowGuard(name)
            self.assertFalse(guard.acquire())
            # The refused attempt leaves no stream of its own behind.
            self.assertIsNone(guard.stream)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.unlink(lock)

    def test_posix_lock_directory_is_per_user(self):
        # The lock directory is scoped to the current user: a shared 0600
        # lock file would stay unopenable for the next user after the
        # first window exits, and the failed open would read as "already
        # running", locking that user out of their own window.
        if os.name == 'nt':
            self.skipTest('POSIX flock branch')
        uid = getattr(os, 'getuid', None)
        self.assertIsNotNone(uid)
        from gachaops import window_guard
        path = window_guard.posix_lock_path(unique_name())
        self.assertTrue(str(path).startswith(tempfile.gettempdir()))
        self.assertEqual(path.parent.name,
                         'gachaops-window-' + str(uid()))

    def test_posix_symlinked_lock_directory_reads_as_not_acquired(self):
        # A planted symlink at the per-user directory would redirect the
        # lock file entirely: refused without following it, never raised.
        # The whole scenario lives in a private temporary directory — the
        # real per-user directory under the shared temp is left alone, so
        # test order cannot matter.
        if os.name == 'nt':
            self.skipTest('POSIX flock branch')
        from gachaops import window_guard
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            # Bound before the patch: inside the with-block the module
            # global is the stand-in itself, so the closure must call the
            # original captured here, not look it up again.
            real_lock_path = window_guard.posix_lock_path

            def private_lock_path(guard_name):
                # The real helper's name derivation and layout, relocated
                # into this test's private directory.
                real = real_lock_path(guard_name)
                return base / real.parent.name / real.name

            lock = private_lock_path(name)
            decoy = base / (lock.parent.name + '.fixture-target')
            decoy.mkdir()
            os.symlink(decoy, lock.parent)
            with patch.object(window_guard, 'posix_lock_path', private_lock_path):
                guard = window_guard.WindowGuard(name)
                self.assertFalse(guard.acquire())
                self.assertIsNone(guard.stream)

    def test_posix_lock_directory_path_taken_by_file_reads_as_not_acquired(self):
        # A plain file where the per-user directory belongs makes mkdir
        # raise FileExistsError — not a PermissionError subclass, so the
        # broad OSError catch is what keeps this from escaping as a
        # traceback before the GUI's error handling exists. Private
        # directory again: nothing outside it is touched or left behind.
        if os.name == 'nt':
            self.skipTest('POSIX flock branch')
        from gachaops import window_guard
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            real_lock_path = window_guard.posix_lock_path

            def private_lock_path(guard_name):
                real = real_lock_path(guard_name)
                return base / real.parent.name / real.name

            private_lock_path(name).parent.write_text('x', encoding='utf-8')
            with patch.object(window_guard, 'posix_lock_path', private_lock_path):
                guard = window_guard.WindowGuard(name)
                self.assertFalse(guard.acquire())
                self.assertIsNone(guard.stream)


@unittest.skipUnless(importlib.util.find_spec('PySide6'),
                     'optional desktop dependencies unavailable')
class DesktopLaunchTests(unittest.TestCase):
    """The desktop.main() launch paths: guard acquisition, busy report,
    handoff wait, smoke/preview exemptions. Skipped wherever the optional
    Qt stack is absent — the same gate the rest of the desktop suite uses."""

    def _qt(self):
        # PySide6 pins the QApplication singleton (builtins.qApp); drop it so
        # the next Qt test can create another instance. Runs via addCleanup,
        # so a failed assertion cannot leak the singleton into other tests.
        import builtins
        import gc
        import shiboken6
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            shiboken6.delete(app)
        if getattr(builtins, 'qApp', None) is not None:
            del builtins.qApp
        gc.collect()

    def _mute_boxes(self):
        """Replace every message box with a recorder: a behavioral regression
        must fail an assertion within seconds, not hang on a modal dialog."""
        from PySide6.QtWidgets import QMessageBox
        boxes = []
        recorder = lambda *a, **k: boxes.append(a)
        for name in ('information', 'critical', 'warning'):
            self.enterContext(patch.object(QMessageBox, name, side_effect=recorder))
        return boxes

    def test_second_launch_activates_existing_window_and_writes_nothing(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child, held = hold_in_child(name, '30')
            self.assertEqual(held, 'HELD')
            self.addCleanup(stop_child, child)
            activations = []
            boxes = self._mute_boxes()
            started = time.monotonic()
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(window_guard, 'activate_existing_window',
                              side_effect=lambda *a, **k: activations.append(1) or True), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
            # With activation succeeding immediately there is no wait: the
            # launch must not sit through the whole budget first.
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(len(activations), 1)
            self.assertEqual(boxes, [])
            # No window, no gate, no data: the refused launch touches nothing.
            self.assertEqual(list(root.iterdir()), [])

    def test_second_launch_without_visible_window_reports_and_exits(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child, held = hold_in_child(name, '30')
            self.assertEqual(held, 'HELD')
            self.addCleanup(stop_child, child)
            boxes = self._mute_boxes()
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(window_guard, 'activate_existing_window',
                              return_value=False), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(len(boxes), 1)
            self.assertIn('GachaOps', boxes[0][1])
            # One sentence that fits both "not found" and "found but not
            # switched": switch from the taskbar, single-window rule.
            self.assertIn('任务栏切换过去', boxes[0][2])
            self.assertIn('另一个登录会话', boxes[0][2])
            self.assertIn('同时只允许一个', boxes[0][2])
            self.assertEqual(list(root.iterdir()), [])

    def test_handoff_launch_waits_for_guard_release_then_continues(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            handoff = root / 'runs' / 'gui-setup-fixture' / 'handoff.json'
            handoff.parent.mkdir(parents=True)
            handoff.write_text(handoff_document(), encoding='utf-8')
            child, held = hold_in_child(name, '1.2')
            self.assertEqual(held, 'HELD')
            self.addCleanup(stop_child, child)
            boxes = self._mute_boxes()
            started = time.monotonic()
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--handoff', str(handoff)]):
                self.assertEqual(desktop.main(), 0)
            self.assertGreaterEqual(time.monotonic() - started, 1.0)
            # A successful handoff never reports anything.
            self.assertEqual(boxes, [])
            # The gate ran: the window was constructed after the release.
            self.assertTrue((root / 'config').is_dir())
            child.wait(timeout=10)

    def test_handoff_timeout_reports_and_exits_without_window(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            handoff = root / 'runs' / 'gui-setup-fixture' / 'handoff.json'
            handoff.parent.mkdir(parents=True)
            handoff.write_text(handoff_document(), encoding='utf-8')
            child, held = hold_in_child(name, '30')
            self.assertEqual(held, 'HELD')
            self.addCleanup(stop_child, child)
            boxes = self._mute_boxes()
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(window_guard, 'HANDOFF_WAIT_SECONDS', 0.6), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--handoff', str(handoff)]):
                # A timed-out handoff exits with its own non-zero code, so
                # scripts can tell it apart from a successful activation.
                self.assertEqual(desktop.main(), 3)
            self.assertEqual(len(boxes), 1)
            # No window, no gate: the timed-out handoff wrote nothing.
            self.assertEqual(list((root / 'runs').iterdir()), [handoff.parent])
            self.assertFalse((root / 'config').exists())

    def test_gate_failure_releases_the_guard_before_the_box(self):
        from gachaops import desktop, window_guard
        from gachaops.planner import ConfigError
        from PySide6.QtWidgets import QApplication, QMessageBox
        self.addCleanup(self._qt)
        name = unique_name()
        # Same witness rationale as the normal-exit test: the mutex object
        # must outlive a release that closes handles without releasing
        # ownership, or the leak is invisible to the probing child.
        witness = open_witness(name)
        if witness is not None:
            self.addCleanup(witness[0].CloseHandle, witness[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            boxes = []

            def probe_at_box_time(*args, **kwargs):
                # The critical box is up only after the guard is gone: a
                # fresh process must be able to take the name right now,
                # or relaunching while the old box lingers would just front
                # that stale box. A child cannot mask via reentrancy.
                child, held = hold_in_child(name, '0')
                self.assertEqual(held, 'HELD')
                stop_child(child)

            record = lambda *a, **k: boxes.append(a)
            # Window is a local class inside main(); the failure is
            # injected through workspace_root, which Window.__init__ calls
            # inside the guarded try (the gate's own failure surface).
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(desktop, 'workspace_root',
                              side_effect=ConfigError('fixture gate failure')), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QMessageBox, 'critical',
                              side_effect=lambda *a, **k:
                                  (boxes.append(a), probe_at_box_time())), \
                 patch.object(QMessageBox, 'information', side_effect=record), \
                 patch.object(QMessageBox, 'warning', side_effect=record), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 2)
            self.assertEqual(boxes[0][1], 'GachaOps 无法打开')
            # The gate never ran: nothing was written.
            self.assertEqual(list(root.iterdir()), [])

    def test_normal_exit_releases_the_window_guard(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        # A witness keeps the mutex object alive even if release() drops
        # only ReleaseMutex while keeping CloseHandle: the object would
        # otherwise die with the last handle and no other process could
        # observe the leaked ownership.
        witness = open_witness(name)
        if witness is not None:
            self.addCleanup(witness[0].CloseHandle, witness[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            boxes = self._mute_boxes()
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(boxes, [])
            self.assertTrue((root / 'config').is_dir())
            # A separate process must see the guard released: the test
            # thread owning the mutex would mask a missing release through
            # same-thread reentrancy.
            prober, report = hold_in_child(name, '0')
            self.assertEqual(report, 'HELD')
            stop_child(prober)

    def test_smoke_test_launch_does_not_take_the_window_guard(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            # The guard is held by a separate process while the smoke launch
            # runs: smoke mode must not consult it, or automation would lose
            # its window (and a same-thread holder would mask the regression
            # through mutex reentrancy).
            child, held = hold_in_child(name, '30')
            self.assertEqual(held, 'HELD')
            self.addCleanup(stop_child, child)
            boxes = self._mute_boxes()
            activations = []
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(window_guard, 'activate_existing_window',
                              side_effect=lambda *a, **k: activations.append(1) or True), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--smoke-test',
                                    str(root / 'smoke.png')]):
                self.assertEqual(desktop.main(), 0)
            # The window was constructed despite the held guard: smoke mode
            # neither consults the guard nor reports busy.
            self.assertTrue((root / 'config').is_dir())
            self.assertEqual(activations, [])
            self.assertEqual(boxes, [])

    def test_preview_factory_launch_does_not_take_the_window_guard(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            # Layout previews have the same exemption as smoke runs: a
            # held guard must not block them or front the user's window.
            child, held = hold_in_child(name, '30')
            self.assertEqual(held, 'HELD')
            self.addCleanup(stop_child, child)
            boxes = self._mute_boxes()

            def preview(window):
                window.timer.stop(); window.recheck_timer.stop()

            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', preview, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
            self.assertTrue((root / 'config').is_dir())
            self.assertEqual(boxes, [])

    def test_recover_launch_waits_for_guard_release_like_handoff(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child, held = hold_in_child(name, '1.2')
            self.assertEqual(held, 'HELD')
            self.addCleanup(stop_child, child)
            boxes = self._mute_boxes()
            started = time.monotonic()
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--recover']):
                self.assertEqual(desktop.main(), 0)
            self.assertGreaterEqual(time.monotonic() - started, 1.0)
            self.assertEqual(boxes, [])
            # The gate ran after the release; --recover then arms the
            # recovery timer on the constructed window.
            self.assertTrue((root / 'config').is_dir())
            child.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
