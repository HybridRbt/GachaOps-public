"""Local user profiles with UUID storage, backups and explicit import previews.

Schema 2 profiles keep run settings only (order, selection, game start,
timeout, repeat limit); installation paths live in the global registration
library. Saves that try to smuggle registration fields in are rejected so a
stale window can never write old paths back.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import uuid

from .adapter_catalog import list_adapters, get_adapter
from .config_lock import config_lock
from .migration_journal import require_no_prepared_journal
from .planner import ConfigError
from .recovery import save

PROFILE_SCHEMA_VERSION = 2
PROFILE_KIND = 'assistant_profile'
RUN_ROW_FIELDS = ('id', 'adapter', 'enabled', 'game_start', 'timeout_seconds')
REGISTRATION_FIELDS = ('installation_root', 'entry_executable', 'game_path')


def file_digest(path):
    """Digest of the decoded text, computed the same way by every caller."""
    return hashlib.sha256(Path(path).read_text(encoding='utf-8-sig').encode('utf-8')).hexdigest()


class LegacyProfilesFound(ConfigError):
    """Schema 1 profiles exist; the registration migration must run first."""

    def __init__(self, identities):
        super().__init__('发现旧版组合配置，需要先完成登记迁移：' + ', '.join(identities))
        self.identities = list(identities)


def default_profile():
    timeouts = {'zzz-onedragon': 3600, 'maa-gf2': 3600, 'maa-end': 7200, 'march7': 3600}
    rows = [{'id': spec.game_id, 'adapter': spec.adapter_id, 'enabled': False,
             'game_start': 'assistant', 'timeout_seconds': timeouts.get(spec.adapter_id, 1800)}
            for spec in list_adapters()]
    return {'repeat_limit': 0, 'assistants': rows}


def validate_profile_rows(data):
    """Validate the run-content form: {repeat_limit, assistants} with no paths."""
    if not isinstance(data, dict) or set(data) - {'repeat_limit', 'assistants'}:
        raise ConfigError('profile keeps run settings only (repeat_limit, assistants)')
    rows = data.get('assistants')
    if not isinstance(rows, list):
        raise ConfigError('assistants must be an ordered list')
    limit = data.get('repeat_limit', 0)
    if type(limit) is not int or limit < 0:
        raise ConfigError('repeat_limit must be a nonnegative integer (additional rounds)')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - set(RUN_ROW_FIELDS):
            raise ConfigError('unknown profile field; registration paths belong to the global library')
        identity = row.get('id')
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ConfigError('assistant id must be unique and nonempty')
        seen.add(identity)
        try:
            spec = get_adapter(row.get('adapter'))
        except ValueError:
            raise ConfigError(f'{identity}: adapter not yet implemented') from None
        if type(row.get('enabled', True)) is not bool:
            raise ConfigError(f'{identity}: enabled must be boolean')
        if row.get('game_start', 'assistant') not in spec.start_modes:
            raise ConfigError(f'{identity}: unsupported game start mode')
        timeout = row.get('timeout_seconds', 1800)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError(f'{identity}: timeout_seconds must be positive and finite')
    return data


def validate_profile_document(data):
    """Validate the stored form with the schema 2 header."""
    if (not isinstance(data, dict) or data.get('schema_version') != PROFILE_SCHEMA_VERSION
            or data.get('kind') != PROFILE_KIND):
        raise ConfigError('expected assistant_profile schema 2')
    if set(data) != {'schema_version', 'kind', 'revision', 'repeat_limit', 'assistants'}:
        raise ConfigError('unknown profile field')
    if type(data['revision']) is not int or data['revision'] < 1:
        raise ConfigError('profile revision must be a positive integer')
    validate_profile_rows({'repeat_limit': data.get('repeat_limit', 0), 'assistants': data['assistants']})
    return data


def profile_document(data, revision):
    document = deepcopy(validate_profile_rows(data))
    document.update(schema_version=PROFILE_SCHEMA_VERSION, kind=PROFILE_KIND, revision=revision)
    return document


def run_content(document):
    """Strip storage header fields; the run snapshot carries neither kind nor revision."""
    data = validate_profile_document(document)
    return {'repeat_limit': data.get('repeat_limit', 0),
            'assistants': [{field: row[field] for field in row if field in RUN_ROW_FIELDS}
                           for row in deepcopy(data['assistants'])]}


class ProfileStore:
    def __init__(self, data_root):
        self.root = Path(data_root).resolve()
        self.directory = self.root / 'config/profiles'
        self.backups = self.root / 'backups/profiles'

    def _path(self, identity):
        try:
            if str(uuid.UUID(identity)) != identity:
                raise ValueError('invalid profile id')
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid profile id') from None
        path = self.directory / identity
        if (self.directory.is_symlink() or path.is_symlink()
                or not self.directory.resolve().is_relative_to(self.root)
                or any((path / name).is_symlink() for name in ('config.json', 'name.json'))):
            raise ValueError('external profile directory')
        return path

    def _name(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('profile name must contain 1-100 characters')
        return name.strip()

    def _read_metadata(self, path):
        metadata = json.loads((path / 'name.json').read_text(encoding='utf-8'))
        if (not isinstance(metadata, dict) or set(metadata) != {'name', 'created'}
                or not isinstance(metadata.get('created'), str)):
            raise ValueError('invalid profile metadata')
        return metadata

    def _scan(self):
        """List profile directories; schema 1 configs raise for migration instead."""
        rows, legacy = [], []
        if self.directory.is_symlink():
            raise ValueError('external profiles directory')
        if self.directory.exists():
            for path in sorted(self.directory.iterdir()):
                if path.name.startswith('.'):
                    continue
                path = self._path(path.name)
                metadata = self._read_metadata(path)
                raw = json.loads((path / 'config.json').read_text(encoding='utf-8-sig'))
                if not isinstance(raw, dict) or 'schema_version' not in raw:
                    raise ValueError(f'invalid profile configuration: {path / "config.json"}')
                if raw['schema_version'] == 1:
                    legacy.append(path.name)
                    continue
                validate_profile_document(raw)
                rows.append({'id': path.name, 'name': self._name(metadata['name']),
                             'path': str(path / 'config.json'), 'created': metadata['created']})
        if legacy:
            raise LegacyProfilesFound(legacy)
        return rows

    def list_profiles(self):
        rows = self._scan()
        if not rows:
            self.create_profile('默认组合')
            return self._scan()
        return tuple(sorted(rows, key=lambda r: r['created']))

    def has_profiles(self):
        return bool(self._scan())

    def _create_unlocked(self, name, profile=None):
        name = self._name(name)
        require_no_prepared_journal(self.root)
        data = profile_document(default_profile() if profile is None else profile, 1)
        if self.directory.is_symlink() or not self.directory.resolve().is_relative_to(self.root):
            raise ValueError('external profiles directory')
        self.directory.mkdir(parents=True, exist_ok=True)
        identity = str(uuid.uuid4())
        path = self._path(identity)
        staging = self.directory / ('.' + identity)
        staging.mkdir()
        save(staging / 'config.json', data)
        save(staging / 'name.json', {'name': name, 'created': datetime.now(timezone.utc).isoformat()})
        os.replace(staging, path)
        return {'id': identity, 'name': name, 'path': str(path / 'config.json')}

    def create_profile(self, name, profile=None):
        with config_lock(self.root):
            return self._create_unlocked(name, profile)

    def load_profile(self, identity):
        path = self._path(identity) / 'config.json'
        if path.is_symlink():
            raise ValueError('external profile configuration')
        return validate_profile_document(json.loads(path.read_text(encoding='utf-8-sig')))

    def _backup(self, path):
        self.backups.mkdir(parents=True, exist_ok=True)
        if self.backups.is_symlink() or not self.backups.resolve().is_relative_to(self.root):
            raise ValueError('external profile backup location')
        destination = self.backups / (path.parent.name + '-' + uuid.uuid4().hex + '-' + path.name)
        # Saving the old JSON atomically is mandatory before replacing it.
        save(destination, json.loads(path.read_text(encoding='utf-8')))

    def save_profile(self, identity, data, expected_revision, expected_digest=None):
        """Write run fields only; reject path smuggling and stale revisions."""
        if any(field in row for row in (data or {}).get('assistants', []) for field in REGISTRATION_FIELDS):
            raise ConfigError('接入路径属于全局登记库，不能写入组合')
        validate_profile_rows({'repeat_limit': (data or {}).get('repeat_limit', 0),
                               'assistants': (data or {}).get('assistants', [])})
        with config_lock(self.root):
            require_no_prepared_journal(self.root)
            path = self._path(identity) / 'config.json'
            if path.is_symlink():
                raise ValueError('external profile configuration')
            raw = path.read_text(encoding='utf-8-sig')
            current = validate_profile_document(json.loads(raw))
            if current['revision'] != expected_revision:
                raise ConfigError(
                    f'组合已被其他窗口修改（当前 revision {current["revision"]}，预期 {expected_revision}）；请刷新后重试')
            if expected_digest is not None and hashlib.sha256(raw.encode('utf-8')).hexdigest() != expected_digest:
                raise ConfigError('组合文件内容与读取时不一致；请刷新后重试')
            self._backup(path)
            document = profile_document(data, expected_revision + 1)
            save(path, document)
            return document

    def rename_profile(self, identity, name):
        with config_lock(self.root):
            require_no_prepared_journal(self.root)
            path = self._path(identity) / 'name.json'
            metadata = json.loads(path.read_text(encoding='utf-8'))
            metadata['name'] = self._name(name)
            self._backup(path)
            save(path, metadata)

    def delete_profile(self, identity):
        with config_lock(self.root):
            require_no_prepared_journal(self.root)
            path = self._path(identity)
            rows = self._scan()
            if len(rows) == 1:
                self._create_unlocked('默认组合')
            self.backups.mkdir(parents=True, exist_ok=True)
            os.replace(path, self.backups / ('deleted-' + identity + '-' + uuid.uuid4().hex))
