"""Inspect known PyAppify OK-WW installation files without importing runner code."""
import ast
import hashlib
import json
from pathlib import Path

from .ok_completion import evaluate, LINE, START


def fingerprint(path):
    path = Path(path)
    content = path.read_bytes()
    return {"path": str(path.resolve()), "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


def inspect_installation(root):
    root = Path(root).resolve()
    app = root / "data/apps/ok-ww"
    working = app / "working"
    metadata = json.loads((app / "app.json").read_text(encoding="utf-8"))
    tree = ast.parse((working / "config.py").read_text(encoding="utf-8-sig"))
    code_version = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "version" for t in node.targets):
            code_version = ast.literal_eval(node.value)
    daily = json.loads((working / "configs/DailyTask.json").read_text(encoding="utf-8"))
    task_classes = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "onetime_tasks":
                    task_classes = ast.literal_eval(value)
    daily_index = next((i for i, pair in enumerate(task_classes or [], 1)
                        if pair == ["src.task.DailyTask", "DailyTask"]), None)
    log_path = working / "logs/ok-script.log"
    log_lines = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    starts = [i for i, line in enumerate(log_lines)
              if (match := LINE.fullmatch(line)) and match["source"] == "start_controller"
              and START.fullmatch(match["message"])]
    recent_lines = log_lines[starts[-1]:] if starts else []
    historical = evaluate(recent_lines, None)
    historical["note"] = "Existing log only; no observed process exit or owned writer. Not a GachaOps run."
    profile = next((p for p in metadata.get("profiles", []) if p.get("name") == metadata.get("current_profile")), {})
    paths = [root / "ok-ww.exe", app / "python/pythonw.exe", app / ".pyappify-shortcut.py",
             working / "main.py", working / "config.py", working / "configs/DailyTask.json",
             working / "src/task/DailyTask.py", working / "ok/task/TaskExecutor.py",
             working / "ok/util/process.py", working / "assets/coco_annotations.json"]
    return {"schema_version": 1, "mode": "read_only_inventory", "game_execution": False,
            "runner": "ok-ww", "readiness": "inventory_observed_contract_pending",
            "root": str(root), "cwd": str(working), "launcher": str(root / "ok-ww.exe"),
            "runtime": str(app / "python/pythonw.exe"), "main_script": str(working / "main.py"),
            "wrapper": str(app / ".pyappify-shortcut.py"),
            "metadata_version": metadata.get("current_version"), "code_version": code_version,
            "resource_version": None, "patch_version": None,
            "task_index_observed": daily_index, "task_classes_observed": task_classes,
            "profile": metadata.get("current_profile"), "declared_admin_required": profile.get("admin"),
            "update_method": metadata.get("update_method"), "daily_preset": daily,
            "log": str(working / "logs/ok-script.log"),
            "historical_log_observation": historical,
            "files": [fingerprint(path) for path in paths],
            "blockers": ["entry_argument_forwarding_unverified", "owned_log_writer_unverified",
                         "game_process_ownership_unverified", "privilege_compatibility_unverified"],
            "completion_contract": "ok-ww-v3.6.7-log-candidate-v1"}
