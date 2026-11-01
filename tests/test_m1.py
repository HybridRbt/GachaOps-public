import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from gachaops.execution_lock import ExecutionBusy, ExecutionLock
from gachaops.ok_completion import capture_cursor, evaluate, observe
from gachaops.ok_inventory import inspect_installation
from gachaops.planner import load
from gachaops.supervisor import simulate

ROOT = Path(__file__).resolve().parents[1]
START = "2026-09-30 18:33:00,000 INFO ok.core.start_controller start_controller:enabled task <src.task.DailyTask.DailyTask object at 0x1234>"
DONE = "2026-09-30 18:41:00,000 INFO TaskExecutor DailyTask:Daily Task Completed"
EXIT = "2026-09-30 18:41:01,000 INFO TaskExecutor TaskExecutor:Successfully Executed Task, Exiting Game and App!"
FAIL = "2026-09-30 18:40:59,416 ERROR TaskExecutor DailyTask:MergeEchoTask Failed Traceback (most recent call last):"


class ExecutionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "native abandoned mutex")
    def test_abandoned_mutex_is_rejected(self):
        import ctypes
        from ctypes import wintypes as w
        name = "GachaOps.test." + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            code = ("from gachaops.execution_lock import ExecutionLock; import sys,time; "
                    "from pathlib import Path; lock=ExecutionLock(sys.argv[1]); lock.__enter__(); "
                    "Path(sys.argv[2]).write_text('ready'); time.sleep(60)")
            child = subprocess.Popen([sys.executable, "-c", code, name, str(ready)], cwd=ROOT)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
                api.CreateMutexW.restype = w.HANDLE
                api.CloseHandle.argtypes = [w.HANDLE]
                api.CloseHandle.restype = w.BOOL
                # Keep the kernel object alive after its owner is terminated.
                handle = api.CreateMutexW(None, False, "Global\\" + name)
                self.assertTrue(handle)
                try:
                    child.kill()
                    child.wait(timeout=10)
                    with self.assertRaisesRegex(ExecutionBusy, "abandoned"):
                        with ExecutionLock(name):
                            pass
                finally:
                    api.CloseHandle(handle)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)

    def test_other_process_cannot_acquire_same_lock(self):
        name = "GachaOps.test." + uuid.uuid4().hex
        code = ("from gachaops.execution_lock import ExecutionLock; "
                "import sys; lock=ExecutionLock(sys.argv[1]); lock.__enter__()")
        with ExecutionLock(name):
            result = subprocess.run([sys.executable, "-c", code, name], cwd=ROOT,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ExecutionBusy", result.stderr)
        with ExecutionLock(name):
            pass

    def test_exception_releases_lock_and_reentrant_call_is_rejected(self):
        name = "GachaOps.test." + uuid.uuid4().hex
        with self.assertRaises(ValueError):
            with ExecutionLock(name):
                with self.assertRaises(ExecutionBusy):
                    with ExecutionLock(name):
                        pass
                raise ValueError("fixture")
        with ExecutionLock(name):
            pass

    def test_chain_lock_blocks_different_output_before_any_fixture(self):
        config = load(ROOT / "config/simulation.json")
        with tempfile.TemporaryDirectory() as directory, ExecutionLock():
            output = Path(directory) / "different-output"
            with self.assertRaises(ExecutionBusy):
                simulate(config, output)
            self.assertFalse(output.exists())


class CompletionTests(unittest.TestCase):
    def test_zero_exit_requires_new_start_both_markers_and_owned_writer(self):
        self.assertEqual(evaluate([START, DONE, EXIT], 0, writer_verified=True)["status"], "success")
        for lines, code, verified in [([DONE, EXIT], 0, True), ([START, DONE], 0, True),
                                      ([START, DONE, EXIT], None, True), ([START, DONE, EXIT], 0, False),
                                      ([START, DONE, EXIT, START], 0, True)]:
            with self.subTest(lines=lines, code=code, verified=verified):
                self.assertEqual(evaluate(lines, code, writer_verified=verified)["status"],
                                 "completion_unconfirmed")

    def test_caught_additional_failure_is_not_erased_by_completion(self):
        result = evaluate([START, FAIL, DONE, EXIT], 0, writer_verified=True)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(len(result["failures"]), 1)

    def test_nonzero_exit_overrides_completion(self):
        self.assertEqual(evaluate([START, DONE, EXIT], 7, writer_verified=True)["status"], "failed")

    def test_daily_error_is_retained_even_without_named_subtask_failure(self):
        error = FAIL.replace("MergeEchoTask Failed Traceback (most recent call last):", "can not battle pass, maybe ended")
        self.assertEqual(evaluate([START, error, DONE, EXIT], 0, writer_verified=True)["status"], "partial_failure")

    def test_unrelated_and_out_of_order_markers_do_not_succeed(self):
        for lines in [[START, EXIT, DONE], [START, DONE.replace("DailyTask:", "OtherTask:"), EXIT],
                      [START, DONE, EXIT.replace(" INFO ", " ERROR ")]]:
            self.assertEqual(evaluate(lines, 0, writer_verified=True)["status"], "completion_unconfirmed")

    def test_old_success_is_ignored_and_new_success_is_observed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.log"
            path.write_text("\n".join([START, DONE, EXIT]) + "\n", encoding="utf-8")
            cursor = capture_cursor(path)
            self.assertEqual(observe(cursor, 0, writer_verified=True)["status"], "completion_unconfirmed")
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\n".join([START, FAIL, DONE, EXIT]) + "\n")
            self.assertEqual(observe(cursor, 0, writer_verified=True)["status"], "partial_failure")

    def test_rotation_truncation_and_prefix_rewrite_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.log"
            for mode in ["rotation", "truncation", "rewrite"]:
                path.write_text("previous run\n", encoding="utf-8")
                cursor = capture_cursor(path)
                if mode == "rotation":
                    path.rename(path.with_name(mode + ".log"))
                if mode == "truncation":
                    path.write_text("", encoding="utf-8")
                else:
                    path.write_text("different data\n" + "\n".join([START, DONE, EXIT]) + "\n", encoding="utf-8")
                result = observe(cursor, 0, writer_verified=True)
                self.assertEqual(result["status"], "completion_unconfirmed")
                self.assertIn("reason", result)

    def test_partial_old_and_new_lines_do_not_create_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.log"
            path.write_bytes(START.encode())
            cursor = capture_cursor(path)
            with path.open("ab") as stream:
                stream.write(("\n" + DONE + "\n" + EXIT + "\n").encode())
            self.assertEqual(observe(cursor, 0, writer_verified=True)["status"], "completion_unconfirmed")
            path.write_bytes(b"")
            cursor = capture_cursor(path)
            with path.open("ab") as stream:
                stream.write((START + "\n" + DONE + "\n" + EXIT).encode())
            self.assertEqual(observe(cursor, 0, writer_verified=True)["status"], "completion_unconfirmed")


class InventoryTests(unittest.TestCase):
    def test_inspection_parses_data_without_executing_installed_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "data/apps/ok-ww"
            working = app / "working"
            files = {root / "ok-ww.exe": "fixture", app / "python/pythonw.exe": "fixture",
                     app / ".pyappify-shortcut.py": "raise RuntimeError('must not execute')",
                     working / "main.py": "raise RuntimeError('must not execute')",
                     working / "config.py": "version='v3.6.7'\nconfig={'onetime_tasks': [['src.task.DailyTask', 'DailyTask']]}\nraise RuntimeError('must not execute')",
                     working / "configs/DailyTask.json": '{"Exit After Task": true}',
                     working / "src/task/DailyTask.py": "fixture",
                     working / "ok/task/TaskExecutor.py": "fixture",
                     working / "ok/util/process.py": "fixture",
                     working / "assets/coco_annotations.json": "{}",
                     app / "app.json": json.dumps({"current_version": "v3.6.7"}),
                     working / "logs/ok-script.log": "\n".join([START, FAIL, DONE, EXIT]) + "\n"}
            for path, content in files.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            report = inspect_installation(root)
            self.assertEqual(report["task_index_observed"], 1)
            self.assertEqual(report["code_version"], "v3.6.7")
            self.assertFalse(report["game_execution"])
            self.assertIsNone(report["resource_version"])
            self.assertEqual(report["historical_log_observation"]["status"], "completion_unconfirmed")
            self.assertEqual(len(report["historical_log_observation"]["failures"]), 1)
            self.assertEqual(len(report["files"]), 10)


if __name__ == "__main__":
    unittest.main()
