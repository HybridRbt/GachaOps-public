"""Registration migration and import as prepared/complete transactions.

Migration turns legacy schema 1 profiles (paths inside each profile) into the
global registration library plus schema 2 run-field profiles. Import splits an
external schema 1 configuration into a profile and registration candidates, or
adds an external schema 2 profile as references only. Both multi-file writes
run as one prepared/complete transaction with byte digests, a full backup and
idempotent forward-only recovery after a restart.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from .adapter_catalog import get_adapter, list_adapters
from .assistants import validate_assistants
from .config_lock import config_lock
from .execution_lock import ExecutionLock
from .migration_journal import (JOURNAL_KIND, archive_journal, backup_root, journal_path,
                                read_journal)
from .planner import ConfigError
from .profiles import ProfileStore, profile_document, run_content, validate_profile_document
from .recovery import RecoveryRequired, read_state, save, save_bytes
from .registry import RegistrationStore, validate_registry

REGISTRATION_FIELDS = ('installation_root', 'entry_executable', 'game_path')
REGISTRY_RELATIVE = 'config/assistant-registry.json'


class SourceChanged(ConfigError):
    """The recorded sources changed between preview and commit; preview again."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _file_digest(path):
    """Raw-byte digest of a stored file; independent of any text decoding."""
    return _digest(Path(path).read_bytes())


def _document_bytes(document):
    return (json.dumps(document, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _normalized(value):
    """Host-platform lexical normalization; never touches the disk."""
    return None if value is None else os.path.normcase(os.path.normpath(value))


def candidate_key(adapter_id, fields):
    spec = get_adapter(adapter_id)
    return tuple(_normalized(fields.get(field)) for field in spec.registration_fields)


def _safe_relative(root, relative):
    path = (root / relative)
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ConfigError(f'外部路径不被接受：{relative}')
    return path


def scan_profiles(data_root):
    """Strict scan of config/profiles; reports the exact file on invalid data."""
    store = ProfileStore(data_root)
    rows = []
    if store.directory.is_symlink():
        raise ConfigError('external profiles directory')
    if not store.directory.exists():
        return rows
    for path in sorted(store.directory.iterdir()):
        if path.name.startswith('.'):
            continue
        config_path = path / 'config.json'
        try:
            store._path(path.name)
        except ValueError as error:
            raise ConfigError(f'{config_path}: {error}') from None
        metadata = store._read_metadata(path)
        try:
            raw = json.loads(config_path.read_text(encoding='utf-8-sig'))
        except ValueError as error:
            raise ConfigError(f'{config_path}: 配置损坏（{error}）') from None
        if not isinstance(raw, dict) or not isinstance(raw.get('schema_version'), int):
            raise ConfigError(f'{config_path}: 无法识别的配置格式')
        if raw['schema_version'] == 1:
            try:
                config = validate_assistants(raw)
            except ConfigError as error:
                raise ConfigError(f'{config_path}: {error}') from None
            schema = 1
        elif raw['schema_version'] == 2:
            try:
                config = validate_profile_document(raw)
            except ConfigError as error:
                raise ConfigError(f'{config_path}: {error}') from None
            schema = 2
        else:
            raise ConfigError(f'{config_path}: 不支持的 schema_version {raw["schema_version"]}')
        rows.append({'id': path.name, 'name': metadata['name'], 'created': metadata['created'],
                     'schema': schema, 'config': config, 'digest': _file_digest(config_path),
                     'name_digest': _file_digest(path / 'name.json')})
    return rows


def _collect_candidates(rows, registry):
    """Full path tuples per adapter; empty registrations never shadow real ones."""
    by_adapter = {}
    for profile in rows:
        if profile['schema'] != 1:
            continue
        label = f'{profile["name"]}（{profile["id"][:8]}）'
        for row in profile['config']['assistants']:
            root_value = row.get('installation_root')
            if not isinstance(root_value, str) or not root_value:
                continue
            adapter_id = row['adapter']
            spec = get_adapter(adapter_id)
            fields = {field: row[field] for field in spec.registration_fields
                      if isinstance(row.get(field), str) and row[field]}
            key = candidate_key(adapter_id, fields)
            slot = by_adapter.setdefault(adapter_id, {})
            if key in slot:
                if label not in slot[key]['sources']:
                    slot[key]['sources'].append(label)
                continue
            slot[key] = {'fields': fields, 'sources': [label], 'from_registry': False}
    if registry is not None:
        for adapter_id, fields in registry['assistants'].items():
            key = candidate_key(adapter_id, fields)
            slot = by_adapter.setdefault(adapter_id, {})
            if key in slot:
                slot[key]['sources'].append('当前全局登记')
                continue
            slot[key] = {'fields': dict(fields), 'sources': ['当前全局登记'], 'from_registry': True}
    return by_adapter


def _preview_adapters(by_adapter):
    adapters = []
    for spec in list_adapters():
        candidates = by_adapter.get(spec.adapter_id)
        if not candidates:
            continue
        ordered = sorted(candidates.values(), key=lambda entry: not entry['from_registry'])
        adapters.append({'adapter': spec.adapter_id, 'display_name': spec.display_name,
                         'candidates': [{'fields': dict(entry['fields']),
                                         'sources': list(entry['sources']),
                                         'from_registry': entry['from_registry']} for entry in ordered],
                         'conflict': len(ordered) > 1})
    return adapters


def startup_state(data_root):
    """Startup gate: pending transaction, required migration, fresh root or ready."""
    root = Path(data_root).resolve()
    journal = read_journal(root)
    if journal is not None:
        if journal['state'] == 'prepared':
            return {'status': 'prepared', 'transaction': journal['transaction']}
        archive_journal(root, journal)
    registry_exists = (root / REGISTRY_RELATIVE).exists()
    rows = scan_profiles(root)
    if any(row['schema'] == 1 for row in rows):
        return {'status': 'migration_required'}
    if not registry_exists:
        return {'status': 'fresh' if not rows else 'migration_required'}
    return {'status': 'ready'}


def initialize_fresh(data_root):
    """Create the empty library for a brand-new data directory."""
    return RegistrationStore(data_root).create()


def preview_registration_migration(data_root):
    """Read-only migration preview with per-adapter candidates and conflicts."""
    root = Path(data_root).resolve()
    journal = read_journal(root)
    if journal is not None:
        if journal['state'] == 'prepared':
            raise ConfigError('存在未完成的迁移/导入事务；请先继续该事务')
        archive_journal(root, journal)
    rows = scan_profiles(root)
    store = RegistrationStore(root)
    registry = store.read()
    sources = []
    for profile in rows:
        if profile['schema'] != 1:
            continue
        sources.append({'path': f'config/profiles/{profile["id"]}/config.json',
                        'exists': True, 'sha256': profile['digest']})
        sources.append({'path': f'config/profiles/{profile["id"]}/name.json',
                        'exists': True, 'sha256': profile['name_digest']})
    sources.append({'path': REGISTRY_RELATIVE, 'exists': registry is not None,
                    **({'sha256': _file_digest(store.path)} if registry is not None else {})})
    return {'schema_version': 1, 'kind': 'registration_migration_preview', 'data_root': str(root),
            'created_at': _now(), 'sources': sources,
            'profiles': [{'id': row['id'], 'name': row['name'], 'created': row['created'],
                          'schema': row['schema']} for row in rows],
            'registry': {'exists': registry is not None,
                         'revision': registry['revision'] if registry is not None else None},
            'adapters': _preview_adapters(_collect_candidates(rows, registry))}


def _validate_preview(root, preview, kind):
    if (not isinstance(preview, dict) or preview.get('kind') != kind
            or preview.get('data_root') != str(root)):
        raise ConfigError('预览与当前数据目录不符，请重新预览')


def _resolve_choices(preview, choices):
    """Map {adapter: candidate index or None} to registration fields.

    A unique candidate is adopted automatically. Every conflict must appear in
    choices, either as a candidate index or as None (稍后重新接入：no entry).
    """
    adapters = {entry['adapter']: entry for entry in preview['adapters']}
    unknown = set(choices or {}) - set(adapters)
    if unknown:
        raise ConfigError('预览之外的选择：' + ', '.join(sorted(unknown)))
    resolved = {}
    for adapter_id, entry in adapters.items():
        choice = (choices or {}).get(adapter_id)
        if entry['conflict'] and adapter_id not in (choices or {}):
            raise ConfigError(f'{entry["display_name"]}：存在多个候选接入路径，请选择一个，或选择“稍后重新接入”')
        if choice is None:
            if not entry['conflict']:
                resolved[adapter_id] = dict(entry['candidates'][0]['fields'])
            continue
        if not isinstance(choice, int) or not 0 <= choice < len(entry['candidates']):
            raise ConfigError(f'{entry["display_name"]}：无效的候选选择')
        resolved[adapter_id] = dict(entry['candidates'][choice]['fields'])
    return resolved


def _convert_profile(profile):
    """Schema 1 profile to schema 2, preserving identity/order/selection/run settings."""
    rows = [{'id': row['id'], 'adapter': row['adapter'], 'enabled': row.get('enabled', True),
             'game_start': row.get('game_start', 'assistant'),
             'timeout_seconds': row.get('timeout_seconds', 1800)}
            for row in profile['config']['assistants']]
    return profile_document({'repeat_limit': profile['config'].get('repeat_limit', 0),
                             'assistants': rows}, 1)


def _verify_sources(root, preview, rows):
    preview_ids = {row['id'] for row in preview['profiles']}
    current_ids = {row['id'] for row in rows}
    if preview_ids != current_ids:
        raise SourceChanged('组合清单已变化（新增或删除组合），请重新预览')
    for source in preview['sources']:
        path = _safe_relative(root, source['path'])
        if source['exists']:
            if not path.exists():
                raise SourceChanged(f'{source["path"]} 已丢失，请重新预览')
            if _file_digest(path) != source['sha256']:
                raise SourceChanged(f'{source["path"]} 内容已变化，请重新预览')
        elif path.exists():
            raise SourceChanged(f'{source["path"]} 已出现，请重新预览')


def _execution_gate(root):
    try:
        state = read_state(root / 'state')
    except RecoveryRequired as error:
        raise ConfigError(f'执行日志不可读，不能迁移：{error}') from None
    if state['phase'] not in {'unused', 'idle'}:
        raise ConfigError('上一轮执行清理未确认，不能开始迁移/导入；请先恢复运行资格')


def _execute_transaction(root, kind, *, choices, sources, targets, profiles, extra=None):
    """Stage backup, write the prepared journal, publish, complete, archive."""
    backup_uuid = uuid.uuid4().hex
    backup_dir = backup_root(root) / backup_uuid
    if backup_dir.exists():
        raise ConfigError('备份目录冲突，请重试')
    if backup_root(root).is_symlink() or not backup_root(root).resolve().is_relative_to(root):
        raise ConfigError('external migration backup location')
    backup_dir.mkdir(parents=True)
    manifest = {'schema_version': 1,
                'kind': 'registration_migration' if kind == 'migration' else 'registration_import',
                'created_at': _now(), 'choices': choices,
                'sources': sources,
                'targets': [{'path': target['path'], 'sha256': _digest(_document_bytes(target['document']))}
                            for target in targets],
                'profiles': profiles}
    if extra:
        manifest.update(extra)
    manifest_bytes = _document_bytes(manifest)
    manifest_sha256 = _digest(manifest_bytes)
    for source in sources:
        if not source['exists']:
            continue
        origin = _safe_relative(root, source['path'])
        if not origin.exists():
            raise SourceChanged(f'{source["path"]} 已丢失，请重新预览')
        payload = origin.read_bytes()
        if _digest(payload) != source['sha256']:
            raise SourceChanged(f'{source["path"]} 内容已变化，请重新预览')
        destination = backup_dir / 'sources' / source['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        if _digest(destination.read_bytes()) != source['sha256']:
            raise ConfigError(f'备份写入校验失败：{source["path"]}')
    for target in targets:
        payload = _document_bytes(target['document'])
        destination = backup_dir / 'targets' / target['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        if _digest(destination.read_bytes()) != _digest(payload):
            raise ConfigError(f'目标备份校验失败：{target["path"]}')
    (backup_dir / 'manifest.json').write_bytes(manifest_bytes)
    journal = {'schema_version': 1, 'kind': JOURNAL_KIND, 'state': 'prepared',
               'transaction': kind, 'backup': backup_uuid,
               'manifest_sha256': manifest_sha256, 'created_at': _now()}
    save(journal_path(root), journal)
    _publish_targets(root, targets)
    journal['state'] = 'complete'
    save(journal_path(root), journal)
    archive_journal(root, journal)
    return {'kind': kind, 'backup': backup_uuid,
            'targets': [target['path'] for target in targets]}


def _publish_targets(root, targets):
    for target in targets:
        payload = _document_bytes(target['document'])
        path = _safe_relative(root, target['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        save_bytes(path, payload)
        if _digest(path.read_bytes()) != _digest(payload):
            raise ConfigError(f'目标发布校验失败：{target["path"]}')


def commit_registration_migration(data_root, preview, choices):
    """Publish registry and schema 2 profiles after re-verifying every source."""
    root = Path(data_root).resolve()
    _validate_preview(root, preview, 'registration_migration_preview')
    resolved = _resolve_choices(preview, choices)
    rows = scan_profiles(root)
    with ExecutionLock():
        with config_lock(root):
            journal = read_journal(root)
            if journal is not None:
                if journal['state'] == 'prepared':
                    raise ConfigError('已有未完成的迁移/导入事务，不能开始新事务')
                archive_journal(root, journal)
            _verify_sources(root, preview, rows)
            _execution_gate(root)
            registry = RegistrationStore(root).read()
            # Adapters with no candidates at all keep their existing global
            # entries; only previewed adapters are chosen or declined.
            entries = {} if registry is None else {a: dict(f) for a, f in registry['assistants'].items()}
            for adapter_id in {entry['adapter'] for entry in preview['adapters']}:
                entries.pop(adapter_id, None)
            entries.update({adapter: dict(fields) for adapter, fields in resolved.items()})
            target_registry = {'schema_version': 1, 'kind': 'assistant_registry',
                               'revision': (registry['revision'] + 1) if registry is not None else 1,
                               'assistants': entries}
            validate_registry(target_registry)
            targets = [{'path': REGISTRY_RELATIVE, 'document': target_registry}]
            for profile in rows:
                if profile['schema'] != 1:
                    continue
                targets.append({'path': f'config/profiles/{profile["id"]}/config.json',
                                'document': _convert_profile(profile)})
            return _execute_transaction(root, 'migration', choices=dict(choices or {}),
                                        sources=preview['sources'], targets=targets,
                                        profiles=preview['profiles'])


def _load_manifest(backup_dir, expected_sha256):
    path = backup_dir / 'manifest.json'
    if path.is_symlink() or not path.exists():
        raise ConfigError('事务清单缺失或为外部链接，无法继续')
    payload = path.read_bytes()
    if _digest(payload) != expected_sha256:
        raise ConfigError('事务清单摘要不符，无法继续')
    try:
        manifest = json.loads(payload.decode('utf-8'))
    except ValueError as error:
        raise ConfigError(f'事务清单损坏：{error}') from None
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1
            or manifest.get('kind') not in {'registration_migration', 'registration_import'}
            or not isinstance(manifest.get('sources'), list)
            or not isinstance(manifest.get('targets'), list)
            or not isinstance(manifest.get('profiles'), list)):
        raise ConfigError('事务清单格式无效，无法继续')
    return manifest


def pending_transaction(data_root):
    """Describe the pending transaction for the startup continuation prompt."""
    root = Path(data_root).resolve()
    journal = read_journal(root)
    if journal is None or journal['state'] != 'prepared':
        return None
    return {'transaction': journal['transaction'], 'backup': journal['backup'],
            'created_at': journal.get('created_at')}


def continue_registration_transaction(data_root):
    """Idempotently finish a prepared transaction after a restart or failure."""
    root = Path(data_root).resolve()
    journal = read_journal(root)
    if journal is None or journal['state'] != 'prepared':
        raise ConfigError('当前没有待继续的迁移/导入事务')
    with ExecutionLock():
        with config_lock(root):
            _execution_gate(root)
            backup_dir = backup_root(root) / journal['backup']
            manifest = _load_manifest(backup_dir, journal['manifest_sha256'])
            for source in manifest['sources']:
                if not source['exists']:
                    continue
                path = backup_dir / 'sources' / source['path']
                if not path.exists() or _digest(path.read_bytes()) != source['sha256']:
                    raise ConfigError(f'备份损坏，停止继续：{source["path"]}')
            for target in manifest['targets']:
                path = backup_dir / 'targets' / target['path']
                if not path.exists() or _digest(path.read_bytes()) != target['sha256']:
                    raise ConfigError(f'目标备份损坏，停止继续：{target["path"]}')
            expected_ids = {row['id'] for row in manifest['profiles']}
            for target in manifest['targets']:
                parts = target['path'].split('/')
                if len(parts) == 4 and parts[1] == 'profiles':
                    expected_ids.add(parts[2])
            current_ids = {row['id'] for row in scan_profiles(root)}
            if current_ids != expected_ids:
                raise ConfigError('组合清单与事务记录不一致（出现未纳入清单的改动），停止继续')
            source_by_path = {source['path']: source for source in manifest['sources']}
            published = []
            for target in manifest['targets']:
                path = _safe_relative(root, target['path'])
                source = source_by_path.get(target['path'])
                if path.exists():
                    current = _digest(path.read_bytes())
                    if current == target['sha256']:
                        published.append(target['path'])
                        continue
                    if source is not None and source['exists'] and current == source['sha256']:
                        pass  # untouched since the preview; publish below
                    else:
                        raise ConfigError(
                            f'{target["path"]} 的内容与事务记录不符（外部改动），停止继续，不覆盖该文件')
                elif source is not None and source['exists']:
                    raise ConfigError(f'{target["path"]} 原本存在但已丢失，停止继续；请人工核对备份')
                # Publish the exact staged bytes from the transaction backup.
                payload = (backup_dir / 'targets' / target['path']).read_bytes()
                path.parent.mkdir(parents=True, exist_ok=True)
                save_bytes(path, payload)
                if _digest(path.read_bytes()) != target['sha256']:
                    raise ConfigError(f'目标发布校验失败：{target["path"]}')
            journal['state'] = 'complete'
            journal['continued'] = True
            save(journal_path(root), journal)
            archive_journal(root, journal)
            return {'kind': journal['transaction'], 'backup': journal['backup'],
                    'already_published': published}


def _split_schema1(config):
    rows = []
    candidates = {}
    for row in config['assistants']:
        rows.append({'id': row['id'], 'adapter': row['adapter'], 'enabled': row.get('enabled', True),
                     'game_start': row.get('game_start', 'assistant'),
                     'timeout_seconds': row.get('timeout_seconds', 1800)})
        root_value = row.get('installation_root')
        if not isinstance(root_value, str) or not root_value:
            continue
        spec = get_adapter(row['adapter'])
        fields = {field: row[field] for field in spec.registration_fields
                  if isinstance(row.get(field), str) and row[field]}
        previous = candidates.get(row['adapter'])
        if previous is not None and candidate_key(row['adapter'], previous) != candidate_key(row['adapter'], fields):
            raise ConfigError(f'导入文件中 {spec.display_name} 存在多个不同接入路径，请先统一后再导入')
        candidates.setdefault(row['adapter'], fields)
    return {'repeat_limit': config.get('repeat_limit', 0), 'assistants': rows}, candidates


def preview_registration_import(data_root, source, name):
    """Read-only import preview; schema 2 adds references, schema 1 splits."""
    root = Path(data_root).resolve()
    store = ProfileStore(root)
    name = store._name(name)
    path = Path(source).resolve(strict=True)
    if path.is_relative_to(root):
        raise ConfigError('导入来源不能位于数据目录内')
    payload = path.read_bytes()
    digest = _digest(payload)
    try:
        raw = json.loads(payload.decode('utf-8-sig'))
    except ValueError as error:
        raise ConfigError(f'导入文件损坏：{error}') from None
    registry = RegistrationStore(root).read_required()
    if isinstance(raw, dict) and raw.get('schema_version') == 2:
        document = validate_profile_document(raw)
        return {'schema_version': 1, 'kind': 'registration_import_preview', 'schema': 2,
                'data_root': str(root), 'created_at': _now(), 'name': name,
                'source': str(path), 'source_sha256': digest,
                'profile': run_content(document),
                'adapters': []}
    config = validate_assistants(raw)
    profile, file_candidates = _split_schema1(config)
    adapters = []
    for adapter_id, fields in file_candidates.items():
        spec = get_adapter(adapter_id)
        current = registry['assistants'].get(adapter_id)
        if current is None:
            action = 'new'
        elif candidate_key(adapter_id, current) == candidate_key(adapter_id, fields):
            action = 'same'
        else:
            action = 'conflict'
        adapters.append({'adapter': adapter_id, 'display_name': spec.display_name,
                         'fields': fields, 'current': dict(current) if current else None,
                         'action': action})
    return {'schema_version': 1, 'kind': 'registration_import_preview', 'schema': 1,
            'data_root': str(root), 'created_at': _now(), 'name': name,
            'source': str(path), 'source_sha256': digest,
            'profile': profile, 'adapters': adapters,
            'new_uuid': str(uuid.uuid4())}


def commit_registration_import(data_root, preview, choices):
    """Add the imported profile; schema 1 also merges chosen registration paths."""
    root = Path(data_root).resolve()
    _validate_preview(root, preview, 'registration_import_preview')
    choices = choices or {}
    if preview['schema'] == 2:
        created = ProfileStore(root).create_profile(preview['name'], preview['profile'])
        return {'kind': 'import', 'schema': 2, 'profile_id': created['id']}
    path = Path(preview['source'])
    if not path.exists() or _digest(path.read_bytes()) != preview['source_sha256']:
        raise SourceChanged('导入来源文件已变化，请重新预览')
    registry = RegistrationStore(root).read_required()
    adapters = {entry['adapter']: entry for entry in preview['adapters']}
    unknown = set(choices) - {adapter for adapter, entry in adapters.items() if entry['action'] == 'conflict'}
    if unknown:
        raise ConfigError('预览之外的选择：' + ', '.join(sorted(unknown)))
    for adapter_id, entry in adapters.items():
        if entry['action'] == 'conflict' and adapter_id not in choices:
            raise ConfigError(f'{entry["display_name"]}：导入路径与当前全局登记冲突，请选择保留当前值或使用导入路径')
    entries = {adapter: dict(fields) for adapter, fields in registry['assistants'].items()}
    applied = []
    for adapter_id, entry in adapters.items():
        if entry['action'] == 'new' or choices.get(adapter_id) == 'import':
            entries[adapter_id] = dict(entry['fields'])
            applied.append(adapter_id)
        elif entry['action'] == 'same':
            applied.append(adapter_id)
    target_registry = dict(registry, revision=registry['revision'] + 1)
    target_registry['assistants'] = entries
    validate_registry(target_registry)
    document = profile_document(preview['profile'], 1)
    sources = [{'path': REGISTRY_RELATIVE, 'exists': True,
                'sha256': _file_digest(RegistrationStore(root).path)}]
    targets = [{'path': REGISTRY_RELATIVE, 'document': target_registry},
               {'path': f'config/profiles/{preview["new_uuid"]}/config.json', 'document': document},
               {'path': f'config/profiles/{preview["new_uuid"]}/name.json',
                'document': {'name': preview['name'], 'created': _now()}}]
    with ExecutionLock():
        with config_lock(root):
            journal = read_journal(root)
            if journal is not None:
                if journal['state'] == 'prepared':
                    raise ConfigError('已有未完成的迁移/导入事务，不能开始新事务')
                archive_journal(root, journal)
            current = RegistrationStore(root).read_required()
            if current != registry:
                raise SourceChanged('全局登记已变化，请重新预览')
            _execution_gate(root)
            rows = scan_profiles(root)
            if preview['new_uuid'] in {row['id'] for row in rows}:
                raise ConfigError('导入目标组合已存在，请重新预览')
            executed = _execute_transaction(
                root, 'import', choices=dict(choices),
                sources=sources, targets=targets,
                profiles=[{'id': row['id'], 'name': row['name'], 'created': row['created'],
                           'schema': row['schema']} for row in rows],
                extra={'import': {'source': preview['source'], 'sha256': preview['source_sha256'],
                                  'name': preview['name'], 'uuid': preview['new_uuid']}})
            return dict(executed, schema=1, profile_id=preview['new_uuid'])
