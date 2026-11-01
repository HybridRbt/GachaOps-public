import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gachaops.ok_adapter import installation_manifest, runner_environment, verify_manifest
from gachaops.ok_completion import capture_cursor, observe_root
from gachaops.ok_trial import execute_trial, load_approved_descriptor
from gachaops.recovery import ExecutionSession, RecoveryRequired, read_state
from gachaops.windows_observation import elevation_report, find_matching_processes, owned_processes
from gachaops.windows_process import WindowsProcess, environment_block, kernel_api, CleanupError


ROOT = Path(__file__).resolve().parents[1]
START = "2026-10-01 00:00:00,000 INFO ok.core.start_controller start_controller:enabled task <src.task.DailyTask.DailyTask object at 0x1234>"
DONE = "2026-10-01 00:00:01,000 INFO TaskExecutor DailyTask:Daily Task Completed"
EXIT = "2026-10-01 00:00:02,000 INFO TaskExecutor TaskExecutor:Successfully Executed Task, Exiting Game and App!"


class ContractTests(unittest.TestCase):
    def test_environment_cleans_case_variants_and_preserves_parent(self):
        inherited = {"SystemRoot": "C:/Windows" if os.name == "nt" else "/windows",
                     "pythonPath": "injected", "PYTHONHOME": "wrong", "Virtual_Env": "wrong", "KEEP": "中文"}
        before = inherited.copy()
        result = runner_environment(inherited)
        self.assertEqual(inherited, before)
        self.assertNotIn("PYTHONPATH", result)
        self.assertNotIn("PYTHONHOME", result)
        self.assertNotIn("VIRTUAL_ENV", result)
        self.assertEqual(result["KEEP"], "中文")
        self.assertEqual(result["PYTHONNOUSERSITE"], "1")

    def test_environment_block_rejects_invalid_keys_values_and_duplicates(self):
        for value in [{"A": "x", "a": "y"}, {"A=B": "x"}, {"A": "x\0y"}, {"": "x"}, {"A": 1}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                environment_block(value)
        block = environment_block({"z": "中文", "A": "space value"})
        self.assertEqual(block[:len(block) - 1], "A=space value\0z=中文\0\0")

    def test_failed_cleanup_and_restart_require_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                with ExecutionSession(directory, "fixture"):
                    raise RuntimeError("cleanup failed")
            self.assertEqual(read_state(directory)["phase"], "quarantined")
            with self.assertRaises(RecoveryRequired):
                with ExecutionSession(directory, "fixture"):
                    self.fail("must not admit another process")

    def test_verified_cleanup_permits_next_session(self):
        with tempfile.TemporaryDirectory() as directory:
            for _ in range(2):
                with ExecutionSession(directory, "fixture") as session:
                    session.finish("success", cleanup_verified=True)
            self.assertTrue(read_state(directory)["cleanup_verified"])

    def test_no_finish_and_false_cleanup_remain_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            with ExecutionSession(directory, "fixture") as session:
                with self.assertRaises(RecoveryRequired):
                    session.finish("success", cleanup_verified=False)
            self.assertEqual(read_state(directory)["phase"], "quarantined")

    def test_corrupt_or_unproved_idle_journal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "execution.json"
            for value in ["{", json.dumps({"schema_version": 1, "phase": "idle", "run_id": "x"})]:
                path.write_text(value)
                with self.assertRaises(RecoveryRequired):
                    with ExecutionSession(directory, "fixture"):
                        self.fail("invalid state admitted")

    def test_crashed_supervisor_leaves_durable_start_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            state = Path(directory) / "state"
            code = ("from gachaops.recovery import ExecutionSession; import sys,time; from pathlib import Path; "
                    "session=ExecutionSession(sys.argv[1], 'fixture'); session.__enter__(); "
                    "Path(sys.argv[2]).write_text('ready'); time.sleep(60)")
            process = subprocess.Popen([sys.executable, "-c", code, str(state), str(ready)], cwd=ROOT)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
            finally:
                process.kill()
                process.wait(timeout=10)
            self.assertEqual(read_state(state)["phase"], "starting")
            with self.assertRaises(RecoveryRequired):
                with ExecutionSession(state, "fixture"):
                    self.fail("crashed session admitted")

    def test_manifest_detects_added_removed_changed_files_and_ignores_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / "data/apps/ok-ww"
            for relative in ["ok-ww.exe", "data/apps/ok-ww/app.json", "data/apps/ok-ww/.pyappify-shortcut.py",
                             "data/apps/ok-ww/working/main.py", "data/apps/ok-ww/working/config.py",
                             "data/apps/ok-ww/python/pythonw.exe", "data/apps/ok-ww/working/src/a.py"]:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            expected = installation_manifest(root)
            log = app / "working/logs/log.txt"
            log.parent.mkdir(parents=True)
            log.write_text("variable run log")
            self.assertEqual(verify_manifest(root, expected)["status"], "unchanged")
            (app / "working/src/a.py").write_text("changed")
            (app / "working/src/b.py").write_text("added")
            (app / "python/pythonw.exe").unlink()
            result = verify_manifest(root, expected)
            self.assertEqual(len(result["changed_files"]), 3)
            self.assertEqual(result["status"], "changed")

    def test_completion_requires_new_root_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log"
            path.write_text("")
            cursor = capture_cursor(path)
            init = "2026-10-01 00:00:00,000 INFO MainThread ok:ok-script init v3.6.7, [], pid=123 config: {}"
            path.write_text("\n".join([init, START, DONE, EXIT]) + "\n")
            self.assertEqual(observe_root(cursor, 0, 123)["status"], "success")
            self.assertEqual(observe_root(cursor, 0, 124)["status"], "completion_unconfirmed")
            with path.open("a") as stream:
                stream.write(init + "\n")
            self.assertEqual(observe_root(cursor, 0, 123)["status"], "completion_unconfirmed")

    def test_missing_or_wrong_approval_never_prepares_or_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "descriptor.json"
            path.write_text("{}")
            with patch("gachaops.ok_trial.prepare") as prepare, patch("gachaops.ok_trial.WindowsProcess") as launch:
                for digest in [None, "wrong"]:
                    with self.assertRaises(ValueError):
                        execute_trial(path, digest, directory, Path(directory) / "state")
                prepare.assert_not_called()
                launch.assert_not_called()


@unittest.skipUnless(os.name == "nt", "native Windows execution contract")
class NativeContractTests(unittest.TestCase):
    def test_constructor_rollback_verification_failure_is_cleanup_error(self):
        api = kernel_api()
        api.AssignProcessToJobObject = lambda *_: 0
        # Actual TerminateProcess still runs; only its wait verification is injected.
        api.WaitForSingleObject = lambda *_: 0x102
        with patch("gachaops.windows_process.kernel_api", return_value=api):
            with self.assertRaisesRegex(CleanupError, "suspended root"):
                WindowsProcess([sys.executable, "-c", "raise RuntimeError('must stay suspended')"])

    def test_trial_lifecycle_preserves_failures_and_quarantines_unowned_game(self):
        for mode in ["success", "partial_failure", "unowned", "drift", "blocked", "timeout", "interrupt"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                working = root / "working"
                (working / "logs").mkdir(parents=True)
                log = working / "logs/ok-script.log"
                log.write_text("old log\n")
                descriptor = {"schema_version": 1, "mode": "prepare_only", "installation_root": str(root),
                              "cwd": str(working), "argv_candidate": ["C:/fixture/pythonw.exe", "main.py", "-t", "DailyTask", "-e"],
                              "preset_sha256": "preset-fixture", "installation_manifest": {}}
                path = root / "prepared.json"
                path.write_text(json.dumps(descriptor))
                approval = hashlib.sha256(path.read_bytes()).hexdigest()
                current = dict(descriptor, game_path="C:/fixture/game.exe", blockers=[])
                if mode == "blocked":
                    current["blockers"] = ["supervisor_elevation_required"]
                closed = []

                class FixtureProcess:
                    pid = 123
                    job = None

                    def __init__(self, *args, **kwargs):
                        pass

                    def __enter__(self):
                        with log.open("a") as stream:
                            stream.write("2026-10-01 00:00:00,000 INFO MainThread ok:ok-script init v3.6.7, [], pid=123 config: {}\n" + START + "\n")
                        return self

                    def wait(self, timeout):
                        if mode == "interrupt":
                            raise KeyboardInterrupt
                        if mode == "timeout":
                            raise subprocess.TimeoutExpired("fixture", timeout)
                        with log.open("a") as stream:
                            if mode == "partial_failure":
                                stream.write("2026-10-01 00:00:01,000 ERROR TaskExecutor DailyTask:MergeEchoTask Failed\n")
                            stream.write(DONE + "\n" + EXIT + "\n")
                        return 0

                    def __exit__(self, *_):
                        closed.append(True)

                rows = [{"pid": 123, "image": "C:/fixture/pythonw.exe", "creation_ticks": 1, "in_owned_job": True},
                        {"pid": 124, "image": "C:/fixture/game.exe", "creation_ticks": 2, "in_owned_job": True}]
                observed = {"status": "observed", "matches": rows if mode == "unowned" else [], "uncertain": []}
                checked = [dict(rows[1], in_owned_job=False)] if mode == "unowned" else []
                # Clock values drive timeout deterministically without sleeping.
                clock = iter([0, 0, 4000, 4000, 4000]) if mode == "timeout" else None
                with patch("gachaops.ok_trial.prepare", return_value=current), \
                     patch("gachaops.ok_trial.verify_manifest", return_value={"status": "changed" if mode == "drift" else "unchanged"}), \
                     patch("gachaops.ok_trial.WindowsProcess", FixtureProcess), \
                     patch("gachaops.ok_trial.owned_processes", return_value=rows), \
                     patch("gachaops.ok_trial.find_matching_processes", return_value=observed), \
                     patch("gachaops.ok_trial.verify_observed_membership", return_value=checked), \
                     patch("gachaops.ok_trial.time.monotonic", side_effect=(lambda: next(clock)) if clock else time.monotonic):
                    if mode in {"blocked", "drift"}:
                        with self.assertRaises(ValueError):
                            execute_trial(path, approval, root / "runs", root / "state")
                        self.assertEqual(closed, [])
                        self.assertEqual(read_state(root / "state")["phase"], "idle")
                    elif mode == "interrupt":
                        with self.assertRaises(KeyboardInterrupt):
                            execute_trial(path, approval, root / "runs", root / "state")
                        self.assertEqual(closed, [True])
                        self.assertEqual(read_state(root / "state")["phase"], "quarantined")
                    else:
                        report, output = execute_trial(path, approval, root / "runs", root / "state")
                        self.assertEqual(closed, [True])
                        if mode == "unowned":
                            self.assertEqual(report["status"], "ownership_or_cleanup_unconfirmed")
                            self.assertEqual(read_state(root / "state")["phase"], "quarantined")
                        else:
                            self.assertEqual(report["status"], mode)
                            self.assertEqual(read_state(root / "state")["phase"], "idle")
                        self.assertEqual(json.loads((output / "summary.json").read_text()), report)

    def test_unicode_environment_and_arguments_roundtrip_without_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "参数 有空格.json"
            env = runner_environment()
            env["GACHAOPS_PROBE"] = "中文 有空格"
            argv = [sys.executable, "-I", "-S", str(ROOT / "gachaops/runtime_probe.py"),
                    "--output", str(output), "--sentinel", "中文 有空格"]
            with WindowsProcess(argv, cwd=directory, env=env) as process:
                self.assertEqual(process.wait(10), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["sentinel"], report["environment_sentinel"])
            self.assertFalse(report["application_imported"])
            self.assertTrue(report["no_site"])

    def test_observations_distinguish_owned_child_and_unrelated_process(self):
        from gachaops.windows_observation import verify_observed_membership
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.json"
            with WindowsProcess([sys.executable, str(ROOT / "tests/fixtures/process_tree.py"), str(pid_file), "hold"]) as process:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        child_pid = json.loads(pid_file.read_text())["child_pid"]
                        break
                    except (OSError, ValueError):
                        time.sleep(0.02)
                else:
                    self.fail("fixture did not publish child identity")
                rows = owned_processes(process)
                self.assertTrue({process.pid, child_pid}.issubset({r["pid"] for r in rows if r.get("in_owned_job")}))
                observed = find_matching_processes([sys.executable])
                self.assertEqual(observed["status"], "observed")
                relevant = [r for r in observed["matches"] if r["pid"] in {process.pid, child_pid, os.getpid()}]
                checked = verify_observed_membership(process, relevant)
                self.assertFalse(next(r["in_owned_job"] for r in checked if r["pid"] == os.getpid()))
                self.assertTrue(next(r["in_owned_job"] for r in checked if r["pid"] == child_pid))
                self.assertTrue(all(r["creation_ticks"] > 0 for r in checked))
        self.assertIsInstance(elevation_report()["elevated"], bool)


if __name__ == "__main__":
    unittest.main()
