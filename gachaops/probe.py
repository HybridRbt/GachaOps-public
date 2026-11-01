"""Verify an installed interpreter with a fixed bundled probe, not main.py."""
import json
import os
from pathlib import Path
import uuid

from .ok_adapter import runner_environment
from .recovery import ExecutionSession, save
from .windows_observation import owned_processes
from .windows_process import WindowsProcess


def probe_runtime(root, output, state):
    if os.name != "nt":
        raise OSError("runtime probe requires Windows")
    app = Path(root).resolve() / "data/apps/ok-ww"
    runtime = app / "python/pythonw.exe"
    working = app / "working"
    directory = Path(output).resolve() / ("runtime-probe-" + uuid.uuid4().hex)
    sentinel = "参数 空格 中文 " + uuid.uuid4().hex
    with ExecutionSession(state, "stdlib_runtime_probe") as session:
        directory.mkdir(parents=True)
        result_path = directory / "probe.json"
        environment = runner_environment()
        environment["GACHAOPS_PROBE"] = sentinel
        argv = [str(runtime), "-I", "-S", str(Path(__file__).with_name("runtime_probe.py")),
                "--output", str(result_path), "--sentinel", sentinel]
        with WindowsProcess(argv, cwd=str(working), env=environment) as process:
            session.observe_owned(owned_processes(process))
            code = process.wait(15)
            pid = process.pid
        # Reaching here means WindowsProcess.close verified no active job members.
        report = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
        valid = (code == 0 and report.get("pid") == pid and report.get("sentinel") == sentinel
                 and report.get("environment_sentinel") == sentinel and report.get("isolated") is True
                 and report.get("no_site") is True and report.get("application_imported") is False
                 and report.get("pythonpath_present") is False and report.get("pythonhome_present") is False
                 and os.path.normcase(report.get("executable", "")) == os.path.normcase(str(runtime))
                 and os.path.normcase(report.get("cwd", "")) == os.path.normcase(str(working)))
        summary = {"status": "success" if valid else "probe_failed", "game_execution": False,
                   "real_entry_verified": False, "exit_code": code, "probe": report,
                   "cleanup_verified": True, "run_id": session.record["run_id"]}
        save(directory / "summary.json", summary)
        session.finish(summary["status"], cleanup_verified=True)
        return summary, directory
