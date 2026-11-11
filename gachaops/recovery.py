"""Durable admission journal shared by all chains in one deployment.

No automatic stale-PID recovery: process disappearance alone does not prove that
an external launcher or detached game was cleaned. Always hold ExecutionLock.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid
import time

from .execution_lock import ExecutionLock


class RecoveryRequired(RuntimeError):
    pass


def save_bytes(path, payload):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers (including sync/indexing software) may briefly hold
        # the destination without delete sharing. Retry only the atomic replace;
        # never remove the old journal or bypass a persistent permission error.
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                if os.name != 'nt' or getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == 5:
                    raise
                time.sleep(min(0.05 * 2 ** attempt, 0.5))
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def save(path, data):
    save_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_state(directory):
    path = Path(directory) / "execution.json"
    if not path.exists():
        return {"schema_version": 1, "phase": "unused"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("phase") not in {"starting", "running", "idle", "quarantined"}
                or not isinstance(value.get("run_id"), str)):
            raise ValueError("invalid journal schema")
        if value["phase"] == "idle" and value.get("cleanup_verified") is not True:
            raise ValueError("idle journal lacks cleanup proof")
        return value
    except (ValueError, OSError) as exc:
        raise RecoveryRequired("execution journal unreadable or invalid; inspect before running") from exc


class ExecutionSession:
    def __init__(self, directory, kind):
        self.directory = Path(directory).resolve()
        self.kind = kind
        self.lock = ExecutionLock()
        self.record = None

    def __enter__(self):
        self.lock.__enter__()
        try:
            from .app_paths import validate_data_admission
            validate_data_admission(self.directory)
            self.directory.mkdir(parents=True, exist_ok=True)
            old = read_state(self.directory)
            if old["phase"] not in {"unused", "idle"}:
                raise RecoveryRequired("previous execution has unverified cleanup; manual inspection required")
            self.record = {"schema_version": 1, "run_id": uuid.uuid4().hex, "phase": "starting",
                           "kind": self.kind, "supervisor_pid": os.getpid(), "cleanup_verified": False,
                           "started_at": datetime.now(timezone.utc).isoformat(), "owned_processes": []}
            if os.name == 'nt':
                from .windows_observation import inspect_process_identity
                try:
                    self.record['supervisor_identity'] = inspect_process_identity(os.getpid())
                except OSError:
                    self.record['supervisor_identity'] = None
            self._save()
            return self
        except BaseException:
            self.lock.__exit__(None, None, None)
            raise

    def _save(self):
        save(self.directory / "execution.json", self.record)

    def observe_owned(self, rows):
        if self.record["phase"] not in {"starting", "running"}:
            raise RecoveryRequired("session no longer active")
        self.record.update(phase="running", owned_processes=rows)
        self._save()

    def register_chain(self, path):
        self.record['chain_summary'] = str(Path(path).resolve())
        self._save()

    def finish(self, outcome, *, cleanup_verified):
        if self.record["phase"] not in {"starting", "running"}:
            raise RecoveryRequired("session no longer active")
        if cleanup_verified is not True:
            raise RecoveryRequired("cannot release durable admission without verified cleanup")
        self.record.update(phase="idle", outcome=outcome, cleanup_verified=True,
                           ended_at=datetime.now(timezone.utc).isoformat())
        self._save()

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.record["phase"] != "idle":
                self.record.update(phase="quarantined", reason=exc_type.__name__ if exc_type else "unfinished_execution")
                self._save()
        finally:
            self.lock.__exit__(exc_type, exc, traceback)
