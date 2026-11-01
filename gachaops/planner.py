"""Validate inventory and route capabilities without launching anything."""
import json
import math
from pathlib import Path


class ConfigError(ValueError):
    pass


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate(data)
    return data


def validate(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ConfigError("schema_version must be 1")
    if not isinstance(data.get("runners"), dict) or not isinstance(data.get("tasks"), list):
        raise ConfigError("runners must be an object; tasks must be a list")
    ids = set()
    for rid, runner in data["runners"].items():
        if not isinstance(runner, dict) or runner.get("adapter") not in {"ok", "maa", "onedragon", "bettergi"}:
            raise ConfigError(f"{rid}: unsupported adapter")
        if not isinstance(runner.get("capabilities"), list):
            raise ConfigError(f"{rid}: capabilities required")
    for task in data["tasks"]:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
            raise ConfigError("task id required")
        tid = task["id"]
        if tid in ids:
            raise ConfigError(f"duplicate task: {tid}")
        ids.add(tid)
        for field in ("game", "capability"):
            if not isinstance(task.get(field), str) or not task[field]:
                raise ConfigError(f"{tid}: {field} required")
        choices = task.get("runners")
        if not isinstance(choices, list) or not choices or any(r not in data["runners"] for r in choices):
            raise ConfigError(f"{tid}: unknown or missing runner")
        timeout = task.get("timeout_seconds", 1800)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError(f"{tid}: timeout must be finite and positive")
        retries = task.get("retries", 0)
        if type(retries) is not int or retries not in (0, 1):
            raise ConfigError(f"{tid}: retries must be 0 or 1")
        if retries and not task.get("replay_safe", False):
            raise ConfigError(f"{tid}: retries require confirmed replay_safe")


def blockers(runner, task):
    missing = []
    if runner.get("game") != task["game"] or task["capability"] not in runner["capabilities"]:
        missing.append("capability_not_supported")
    if runner.get("health", {}).get(task["capability"], "unknown") != "healthy":
        missing.append("capability_health_unverified")
    if runner.get("readiness") != "validated":
        missing.append("installation_unvalidated")
    for field in ("exe", "cwd", "runner_version", "resource_version", "completion_contract"):
        if not runner.get(field):
            missing.append(f"missing_{field}")
    return missing


def plan(data):
    validate(data)
    rows = []
    for task in data["tasks"]:
        candidates = [{"runner": rid, "blockers": blockers(data["runners"][rid], task)}
                      for rid in task["runners"]]
        selected = next((c["runner"] for c in candidates if not c["blockers"]), None)
        rows.append({"task": task["id"], "game": task["game"], "capability": task["capability"],
                     "selected_runner": selected, "status": "planned" if selected else "blocked",
                     "candidates": candidates})
    return {"schema_version": 1, "mode": "plan_only", "tasks": rows}


def command(runner):
    """Only the documented OK contract is currently implemented. Never invoke here."""
    if runner["adapter"] != "ok":
        raise ConfigError(f"{runner['adapter']}: CLI/completion contract pending verification")
    task_index = runner.get("task_index")
    if type(task_index) is not int or task_index < 1 or not runner.get("exe"):
        raise ConfigError("OK requires exe and task_index >= 1")
    return [runner["exe"], "-t", str(task_index), "-e"]
