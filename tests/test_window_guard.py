"""Machine-wide single window guard: acquisition, activation, handoff wait.

Process-level tests use a real uniquely-named guard held by a helper child
that only holds the lock and shows no window; the product's default guard
name is never touched, so the user can have GachaOps open while this runs.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
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
    """Hold the named guard in a helper child process; no window is shown."""
    directory = Path(tempfile.mkdtemp(prefix='window-guard-child-'))
    script = directory / 'holder.py'
    script.write_text(CHILD_TEMPLATE.format(tree=str(TREE)), encoding='utf-8')
    child = subprocess.Popen([sys.executable, '-B', str(script), name, str(seconds), mode],
                             stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline().strip()
    if line != 'HELD':
        child.kill()
        child.stdout.close()
        raise AssertionError('child did not hold the guard: ' + line)
    return child


def stop_child(child):
    """Terminate a still-running holder and release its pipes."""
    try:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
    finally:
        if child.stdout and not child.stdout.closed:
            child.stdout.close()


def handoff_document():
    from gachaops.desktop_model import write_handoff
    from gachaops.profiles import default_profile
    with tempfile.TemporaryDirectory() as scratch:
        document = write_handoff(Path(scratch) / 'handoff.json', None, None, 1,
                                 default_profile())
    return json.dumps(document, ensure_ascii=False)


class WindowGuardTests(unittest.TestCase):
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

    def test_second_launch_activates_existing_window_and_writes_nothing(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = hold_in_child(name, '30')
            self.addCleanup(stop_child, child)
            activations = []
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(window_guard, 'activate_existing_window',
                              side_effect=lambda *a, **k: activations.append(1) or True), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(len(activations), 1)
            # No window, no gate, no data: the refused launch touches nothing.
            self.assertEqual(list(root.iterdir()), [])

    def test_second_launch_without_visible_window_reports_and_exits(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication, QMessageBox
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = hold_in_child(name, '30')
            self.addCleanup(stop_child, child)
            boxes = []
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(window_guard, 'activate_existing_window',
                              return_value=False), \
                 patch.object(QMessageBox, 'information',
                              side_effect=lambda *a, **k: boxes.append(a)), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(len(boxes), 1)
            self.assertIn('GachaOps', boxes[0][1])
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
            child = hold_in_child(name, '1.2')
            self.addCleanup(stop_child, child)
            started = time.monotonic()
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--handoff', str(handoff)]):
                self.assertEqual(desktop.main(), 0)
            self.assertGreaterEqual(time.monotonic() - started, 1.0)
            # The gate ran: the window was constructed after the release.
            self.assertTrue((root / 'config').is_dir())
            child.wait(timeout=10)

    def test_handoff_timeout_reports_and_exits_without_window(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication, QMessageBox
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            handoff = root / 'runs' / 'gui-setup-fixture' / 'handoff.json'
            handoff.parent.mkdir(parents=True)
            handoff.write_text(handoff_document(), encoding='utf-8')
            child = hold_in_child(name, '30')
            self.addCleanup(stop_child, child)
            boxes = []
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(window_guard, 'HANDOFF_WAIT_SECONDS', 0.6), \
                 patch.object(QMessageBox, 'information',
                              side_effect=lambda *a, **k: boxes.append(a)), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--handoff', str(handoff)]):
                self.assertEqual(desktop.main(), 0)
            self.assertEqual(len(boxes), 1)
            # No window, no gate: the timed-out handoff wrote nothing.
            self.assertEqual(list((root / 'runs').iterdir()), [handoff.parent])
            self.assertFalse((root / 'config').exists())

    def test_window_guard_abandoned_ownership_is_taken_over(self):
        from gachaops import window_guard
        name = unique_name()
        child = hold_in_child(name, '0', mode='exit-held')
        self.addCleanup(stop_child, child)
        child.wait(timeout=10)
        self.assertEqual(child.returncode, 3)
        guard = window_guard.WindowGuard(name)
        self.assertTrue(guard.acquire())
        guard.release()
        # And the name stays usable after a clean release.
        again = window_guard.WindowGuard(name)
        self.assertTrue(again.acquire())
        again.release()

    def test_normal_exit_releases_the_window_guard(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch.object(window_guard, 'GUARD_NAME', name), \
                 patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
            self.assertTrue((root / 'config').is_dir())
            guard = window_guard.WindowGuard(name)
            self.assertTrue(guard.acquire())
            guard.release()

    def test_smoke_test_launch_does_not_take_the_window_guard(self):
        from gachaops import desktop, window_guard
        from PySide6.QtWidgets import QApplication
        self.addCleanup(self._qt)
        name = unique_name()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            # The guard is already held while the smoke launch runs: smoke
            # mode must not consult it, or automation would lose its window.
            holder = window_guard.WindowGuard(name)
            self.assertTrue(holder.acquire())
            try:
                with patch.object(window_guard, 'GUARD_NAME', name), \
                     patch.object(desktop, 'workspace_root', return_value=root), \
                     patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                     patch.object(QApplication, 'exec', return_value=0), \
                     patch('sys.argv', ['desktop-test', '--smoke-test',
                                        str(root / 'smoke.png')]):
                    self.assertEqual(desktop.main(), 0)
                self.assertTrue((root / 'config').is_dir())
            finally:
                holder.release()


if __name__ == '__main__':
    unittest.main()
