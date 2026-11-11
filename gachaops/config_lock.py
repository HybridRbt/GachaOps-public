"""Short-lived cross-process lock for configuration writes in one data root.

Reuses the ExecutionLock mutex implementation under a different, data-root
scoped name so registration/profile/migration writes serialize between GUI
windows and workers. It never replaces or resets the global execution lock.
"""
import hashlib
from pathlib import Path

from .execution_lock import ExecutionLock, ExecutionBusy  # noqa: F401 (re-export)


def config_lock_name(data_root):
    root = Path(data_root).resolve()
    digest = hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:20]
    return f'GachaOps.Config.{digest}.v1'


def config_lock(data_root):
    return ExecutionLock(name=config_lock_name(data_root))
