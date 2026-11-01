import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gachaops.preflight import desktop_report
from gachaops.windows_process import CleanupError, WindowsProcess, kernel_api

FIXTURE = Path(__file__).parent / "fixtures/process_tree.py"


@unittest.skipUnless(os.name == "nt", "native Windows Job Object tests")
class WindowsTests(unittest.TestCase):
    def test_official_launcher_root_and_game_child_share_assistant_job(self):
        from gachaops.windows_observation import owned_processes
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'launcher-game.json'
            with WindowsProcess([sys.executable, '-c', 'pass'], additional_roots=[
                    [sys.executable, str(FIXTURE), str(pid_file), 'hold']]) as process:
                self.assertEqual(process.wait(10), 0)
                api, child = self.child_handle(pid_file)
                rows = owned_processes(process)
                self.assertGreaterEqual(len(rows), 2)
                self.assertTrue(all(row.get('in_owned_job') for row in rows))
            self.assertEqual(api.WaitForSingleObject(child, 5000), 0)

    def test_missing_official_launcher_prevents_assistant_code_from_running(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / 'must-not-run'
            with self.assertRaises(OSError):
                WindowsProcess([sys.executable, '-c',
                                'from pathlib import Path; Path(' + repr(str(sentinel)) + ').touch()'],
                               additional_roots=[[str(Path(directory) / 'missing.exe')]])
            self.assertFalse(sentinel.exists())

    def child_handle(self, pid_file):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                pid = json.loads(pid_file.read_text())["child_pid"]
                break
            except (OSError, ValueError):
                time.sleep(0.02)
        else:
            self.fail("fixture did not publish child pid")
        api = kernel_api()
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        handle = api.OpenProcess(0x100000, False, pid)
        self.assertTrue(handle, "child must be alive before cleanup")
        self.addCleanup(api.CloseHandle, handle)
        return api, handle

    def test_parent_exit_keeps_child_owned_until_close(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.json"
            with WindowsProcess([sys.executable, str(FIXTURE), str(pid_file), "exit"]) as process:
                self.assertEqual(process.wait(10), 0)
                api, handle = self.child_handle(pid_file)
                self.assertGreaterEqual(process.active_processes(), 1)
            self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
            process.close()  # idempotent

    def test_timeout_cleans_entire_tree_and_preserves_unrelated_process(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(unrelated.wait)
        self.addCleanup(unrelated.kill)
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.json"
            with WindowsProcess([sys.executable, str(FIXTURE), str(pid_file), "hold"]) as process:
                api, handle = self.child_handle(pid_file)
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(0.01)
            self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
            self.assertIsNone(unrelated.poll())

    def test_context_exception_cleans_child(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.json"
            with self.assertRaises(KeyboardInterrupt):
                with WindowsProcess([sys.executable, str(FIXTURE), str(pid_file), "hold"]):
                    api, handle = self.child_handle(pid_file)
                    raise KeyboardInterrupt
            self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)

    def test_failed_assignment_never_runs_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.json"
            api = kernel_api()
            api.AssignProcessToJobObject = lambda *_: 0
            with patch("gachaops.windows_process.kernel_api", return_value=api), self.assertRaises(OSError):
                WindowsProcess([sys.executable, str(FIXTURE), str(pid_file), "hold"])
            self.assertFalse(pid_file.exists())

    def test_cleanup_failure_is_not_silently_successful(self):
        with WindowsProcess([sys.executable, "-c", "import time; time.sleep(60)"]) as process:
            with patch.object(process, "active_processes", return_value=1), self.assertRaises(CleanupError):
                process.terminate_tree(timeout=0)

    def test_preflight_reports_observations_without_changing_desktop(self):
        report = desktop_report()
        self.assertIn(report["status"], ["observed_available", "blocked"])
        self.assertIn("primary_screen", report["checks"])
        self.assertFalse(report["capture_verified"])


class PortableTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "non-Windows behavior")
    def test_preflight_unsupported(self):
        self.assertEqual(desktop_report()["status"], "unsupported")

    def test_rejects_relative_executable_before_launch(self):
        with self.assertRaises(ValueError):
            WindowsProcess(["python.exe"])
