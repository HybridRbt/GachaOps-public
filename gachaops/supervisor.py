"""Serial simulator exercising actual subprocesses with a fixed bundled fixture.

CLI exposes no arbitrary program launch. Windows fixtures use owned Job Objects;
real runners still require installation and task completion contracts.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

from .planner import ConfigError, validate
from .windows_process import CleanupError, WindowsProcess
from .execution_lock import ExecutionLock

BEHAVIORS = {"success", "failure", "timeout", "ambiguous"}
# Allow cold Python startup on shared CI hosts while bounding simulated hangs.
SIMULATION_TIMEOUT_CAP = 5


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def attempt(behavior, timeout, directory, index):
    log = directory / f"attempt-{index}.log"
    started = time.monotonic()
    fixture = Path(__file__).with_name("mock_runner.py")
    status = "launch_failed"
    code = None
    if os.name == "nt":
        log.write_text("", encoding="utf-8")
        argv = [sys.executable, str(fixture), "--behavior", behavior, "--log-file", str(log.resolve())]
        with WindowsProcess(argv) as process:
            try:
                code = process.wait(timeout)
                status = "failed" if code != 0 else "completion_unconfirmed"
            except subprocess.TimeoutExpired:
                status = "timeout"
    else:
        with log.open("w", encoding="utf-8") as stream:
            code, status = posix_fixture(behavior, timeout, fixture, stream)
    if code == 0:
        events = []
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if {"event": "task_completed", "success": True} in events:
            status = "success"
    return {"status": status, "exit_code": code, "duration_seconds": round(time.monotonic() - started, 3),
            "log": log.name}


def posix_fixture(behavior, timeout, fixture, stream):
    code = None
    status = "launch_failed"
    process = subprocess.Popen([sys.executable, str(fixture), "--behavior", behavior],
                               stdout=stream, stderr=subprocess.STDOUT, shell=False)
    try:
        code = process.wait(timeout=timeout)
        status = "failed" if code != 0 else "completion_unconfirmed"
    except subprocess.TimeoutExpired:
        status = "timeout"
    finally:
        # Fixture is strictly one process. Production tree cleanup is M1.
        if process.poll() is None:
            process.kill()
        process.wait()
    return code, status

def simulate(data, output, behaviors=None):
    with ExecutionLock():
        return _simulate(data, output, behaviors)


def _simulate(data, output, behaviors=None):
    validate(data)
    behaviors = behaviors or {}
    unknown = set(behaviors) - {task["id"] for task in data["tasks"]}
    if unknown:
        raise ConfigError(f"unknown simulation tasks: {sorted(unknown)}")
    for value in behaviors.values():
        if not isinstance(value, list) or not value or any(b not in BEHAVIORS for b in value):
            raise ConfigError("behaviors must be nonempty lists of known mock behaviors")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".simulation.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ConfigError("simulation already running, or stale lock needs inspection") from exc
    directory = None
    report = {"schema_version": 1, "mode": "simulation", "game_execution": False, "tasks": []}
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        directory = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        directory.mkdir()
        write_json(directory / "summary.json", report)
        for index, task in enumerate(data["tasks"]):
            task_dir = directory / f"task-{index:02d}"
            task_dir.mkdir()
            row = {"task": task["id"], "runner": "bundled-mock", "status": "running", "attempts": []}
            report["tasks"].append(row)
            sequence = behaviors.get(task["id"], ["success"])
            for retry in range(task.get("retries", 0) + 1):
                try:
                    result = attempt(sequence[min(retry, len(sequence) - 1)],
                                     min(task.get("timeout_seconds", SIMULATION_TIMEOUT_CAP),
                                         SIMULATION_TIMEOUT_CAP), task_dir, retry + 1)
                except CleanupError as exc:
                    row["status"] = "cleanup_failed"
                    row["attempts"].append({"status": "cleanup_failed", "error": str(exc)})
                    report["status"] = "cleanup_failed"
                    report["tasks"].extend({"task": later["id"], "status": "not_run",
                                            "reason": "prior_cleanup_failure"}
                                           for later in data["tasks"][index + 1:])
                    write_json(directory / "summary.json", report)
                    return report, directory
                except OSError as exc:
                    result = {"status": "launch_failed", "error": str(exc)}
                if "log" in result:
                    result["log"] = str((task_dir / result["log"]).relative_to(directory))
                row["attempts"].append(result)
                row["status"] = result["status"]
                write_json(directory / "summary.json", report)
                if result["status"] == "success":
                    break
        report["status"] = "success" if all(r["status"] == "success" for r in report["tasks"]) else "partial_failure"
        write_json(directory / "summary.json", report)
        return report, directory
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        if directory:
            write_json(directory / "summary.json", report)
        raise
    finally:
        lock.unlink(missing_ok=True)
