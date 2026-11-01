"""Stable resources/data paths and explicit, locked legacy-data migration."""
from dataclasses import dataclass
import hashlib
from pathlib import Path
import json
import os
import shutil
import sys
import uuid


@dataclass(frozen=True)
class AppPaths:
    resource_root: Path
    data_root: Path

    @property
    def config_dir(self): return self.data_root / 'config'
    @property
    def state_dir(self): return self.data_root / 'state'
    @property
    def runs_dir(self): return self.data_root / 'runs'
    @property
    def backups_dir(self): return self.data_root / 'backups'


def absolute_directory(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('data directory must be an absolute, non-symlink path')
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError('data directory is not a directory')
    return path


def resolve_app_paths(data_root=None):
    frozen = getattr(sys, 'frozen', False)
    source = Path(__file__).resolve().parents[1]
    home = Path(sys.executable).resolve().parent if frozen else source
    resources = Path(getattr(sys, '_MEIPASS', home)).resolve() if frozen else source
    marker = home / 'gachaops-workspace.json'
    bound = None
    if marker.exists():
        if marker.is_symlink():
            raise ValueError('external workspace binding')
        data = json.loads(marker.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('workspace'), str):
            raise ValueError('invalid GachaOps workspace binding')
        bound = absolute_directory(data['workspace'])
        if not (bound / 'config').is_dir():
            raise ValueError('GachaOps workspace unavailable')
    legacy = None
    if frozen and bound is None:
        for parent in (home, *home.parents):
            if (parent / 'gachaops/__init__.py').is_file() and (parent / 'config').is_dir():
                legacy = parent
                break
    default_base = os.environ.get('LOCALAPPDATA')
    default_root = absolute_directory(Path(default_base) / 'GachaOps') if default_base else None
    authoritative = bound or legacy or (source if not frozen else default_root)
    if data_root is not None:
        root = absolute_directory(data_root)
        if bound is not None and root != bound:
            raise ValueError('explicit data directory conflicts with existing workspace binding')
        # A command-line override cannot escape a known unfinished deployment.
        from .recovery import read_state
        for known in {authoritative, default_root} - {None, root}:
            journal = read_state(known / 'state')
            if journal['phase'] not in {'unused', 'idle'}:
                raise ValueError('existing deployment has unverified cleanup; data directory change blocked')
    elif authoritative is not None:
        root = authoritative
    else:
        base = os.environ.get('LOCALAPPDATA')
        if not base:
            raise ValueError('LOCALAPPDATA unavailable; choose an explicit data directory')
        root = absolute_directory(Path(base) / 'GachaOps')
    return AppPaths(resources, root)


def validate_data_admission(state_directory):
    """Call while holding the execution lock, before writing a new journal."""
    resolve_app_paths(Path(state_directory).resolve().parent)


def workspace_root(data_root=None):
    return resolve_app_paths(data_root).data_root


def state_directory(data_root=None):
    return resolve_app_paths(data_root).state_dir


def preview_migration(source, target):
    from .recovery import read_state
    source, target = absolute_directory(source), absolute_directory(target)
    if (not (source / 'config').is_dir() or target == source
            or source.is_relative_to(target) or target.is_relative_to(source)):
        raise ValueError('invalid migration roots')
    if target.exists():
        # Do not merge two authorities, even when the target looks empty.
        raise ValueError('migration target already exists')
    journal = read_state(source / 'state')
    if journal.get('phase') != 'idle' or journal.get('cleanup_verified') is not True:
        raise ValueError('migration requires an idle source with verified cleanup')
    files = []
    for directory in ('config', 'state', 'runs'):
        parent = source / directory
        if parent.is_symlink():
            raise ValueError('migration source contains symlink')
        if not parent.exists():
            continue
        for path in sorted(parent.rglob('*')):
            if path.is_symlink():
                raise ValueError('migration source contains symlink')
            if path.is_file():
                files.append({'path': path.relative_to(source).as_posix(),
                              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'source': str(source), 'target': str(target), 'journal': journal, 'files': files}


def migrate_data(source, target, preview):
    from .execution_lock import ExecutionLock
    from .recovery import save
    with ExecutionLock():
        current = preview_migration(source, target)
        if current != preview:
            raise ValueError('migration source changed; preview again')
        source, target = Path(current['source']), Path(current['target'])
        staging = target.with_name(target.name + '.migration-' + uuid.uuid4().hex)
        staging.mkdir(parents=True)
        # Stage and verify before the one atomic publication. On failure retain
        # source and staging for inspection; never replace an existing target.
        for row in current['files']:
            destination = staging / row['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / row['path'], destination)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('migration copy verification failed')
        backup = staging / 'backups' / ('migration-' + uuid.uuid4().hex + '.json')
        backup.parent.mkdir()
        save(backup, current)
        if preview_migration(source, target) != current:
            raise ValueError('migration source changed during copy')
        os.replace(staging, target)
        return current
