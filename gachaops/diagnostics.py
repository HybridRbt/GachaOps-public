"""User-reviewed local diagnostics: only explicitly permitted fields leave the run."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
import zipfile

from .adapter_catalog import list_adapters

MAX_FILE = 1024 * 1024
MAX_TOTAL = 10 * MAX_FILE
MAX_FILES = 100
STATUSES = frozenset({'disabled', 'ready', 'blocked', 'starting', 'running', 'success', 'failed',
    'partial_failure', 'completion_unconfirmed', 'completed_with_failures', 'nothing_enabled',
    'cancelled', 'timeout', 'interrupted', 'execution_error', 'ownership_or_cleanup_unconfirmed',
    'stopped_cleanup_unconfirmed', 'stopped_external_game_running', 'stopped_preflight_blocked',
    'desktop_environment_interrupted', 'quarantined', 'idle', 'observed', 'unknown'})
IDS = frozenset(s.game_id for s in list_adapters())
ADAPTERS = frozenset(s.adapter_id for s in list_adapters())


@dataclass(frozen=True)
class DiagnosticPreview:
    source: Path
    files: tuple[str, ...]
    missing: tuple[str, ...]
    fingerprints: tuple[tuple[str, str], ...]
    payload: str


def _allowed(data, depth=0):
    if not isinstance(data, dict) or depth > 6: return {}
    result = {}
    for field in ('status', 'phase'):
        if field in data: result[field] = data[field] if isinstance(data[field], str) and data[field] in STATUSES else 'unknown'
    if isinstance(data.get('id'), str) and data['id'] in IDS: result['id'] = data['id']
    if isinstance(data.get('adapter'), str) and data['adapter'] in ADAPTERS: result['adapter'] = data['adapter']
    for field in ('cleanup_verified', 'owned_job_cleanup_verified', 'game_execution', 'repeat_blocked',
                  'game_observed_in_job', 'game_observed_external', 'external_game_left_running'):
        if type(data.get(field)) is bool: result[field] = data[field]
    for field in ('exit_code', 'round'):
        if type(data.get(field)) is int and abs(data[field]) <= 2**31: result[field] = data[field]
    for field in ('assistants', 'attempts', 'tasks'):
        if isinstance(data.get(field), list):
            result[field] = [_allowed(row, depth + 1) for row in data[field][:100]]
    if isinstance(data.get('completion'), dict): result['completion'] = _allowed(data['completion'], depth + 1)
    if isinstance(data.get('reason'), str) and data['reason'] in {'game_job_inheritance_unconfirmed', 'matching_process_observation_uncertain',
                             'matching_processes_remain_after_cleanup', 'post_cleanup_observation_failed'}:
        result['reason_code'] = data['reason']
    elif data.get('error') or data.get('reason'):
        result['reason_code'] = 'redacted_or_unknown'
    return result


def build_diagnostic_preview(run_dir):
    original = Path(run_dir)
    if original.is_symlink() or not original.is_dir(): raise ValueError('invalid run directory')
    root = original.resolve(strict=True)
    candidates = [root / 'summary.json']
    for directory in sorted(root.glob('node-*')):
        if not re.fullmatch(r'node-[0-9]+', directory.name): continue
        if directory.is_symlink(): raise ValueError('external diagnostic directory')
        if directory.is_dir(): candidates.append(directory / 'summary.json')
    if len(candidates) > MAX_FILES: raise ValueError('too many diagnostic inputs')
    rows, hashes, missing, total = [], [], [], 0
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.resolve().is_relative_to(root): raise ValueError('external diagnostic input')
        if not path.exists():
            missing.append(relative); continue
        size = path.stat().st_size
        total += size
        if size > MAX_FILE or total > MAX_TOTAL: raise ValueError('diagnostic input too large')
        with path.open('rb') as stream: raw = stream.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE: raise ValueError('diagnostic input changed or too large')
        try:
            data = json.loads(raw.decode('utf-8-sig'))
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError('invalid diagnostic JSON') from None
        if not isinstance(data, dict): raise ValueError('invalid diagnostic JSON')
        hashes.append((relative, hashlib.sha256(raw).hexdigest()))
        rows.append({'evidence': relative, 'summary': _allowed(data)})
    payload = json.dumps({'schema_version': 1, 'application': 'GachaOps', 'records': rows,
                          'missing': missing}, ensure_ascii=True, sort_keys=True, indent=2)
    return DiagnosticPreview(root, ('diagnostics.json', 'manifest.json'), tuple(missing), tuple(hashes), payload)


def export_diagnostics(preview, destination):
    if not isinstance(preview, DiagnosticPreview): raise ValueError('diagnostic preview required')
    destination = Path(destination).resolve()
    if destination.is_relative_to(preview.source) or destination.exists():
        raise ValueError('choose a new destination outside the source run')
    if build_diagnostic_preview(preview.source) != preview:
        raise ValueError('source changed; preview diagnostics again')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('diagnostics.json', preview.payload)
            archive.writestr('manifest.json', json.dumps({'schema_version': 1,
                'source_hashes': dict(preview.fingerprints), 'files': list(preview.files)}, indent=2))
        if build_diagnostic_preview(preview.source) != preview:
            raise ValueError('source changed during export; preview again')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
