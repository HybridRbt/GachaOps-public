"""Global assistant registration library shared by every profile in one data root.

One entry per adapter holds only that adapter's registration_fields. The
library is the single authority for installation paths; profiles keep run
settings only. A missing library is a migration requirement, never a fact to
overwrite with an empty file; a corrupt library is an error, never rebuilt.
"""
from copy import deepcopy
import json
from pathlib import Path
import uuid

from .adapter_catalog import get_adapter
from .config_lock import config_lock
from .migration_journal import require_no_prepared_journal
from .planner import ConfigError
from .recovery import save

REGISTRY_SCHEMA_VERSION = 1
REGISTRY_KIND = 'assistant_registry'


class RegistryMissing(ConfigError):
    """No registry exists yet; migration or an explicit first-time setup is required."""


class RevisionConflict(ConfigError):
    """The stored revision changed; the caller must re-read before writing."""


def validate_registry(data):
    if (not isinstance(data, dict) or data.get('schema_version') != REGISTRY_SCHEMA_VERSION
            or data.get('kind') != REGISTRY_KIND):
        raise ConfigError('expected assistant_registry schema 1')
    if set(data) != {'schema_version', 'kind', 'revision', 'assistants'}:
        raise ConfigError('unknown registry field')
    if type(data['revision']) is not int or data['revision'] < 1:
        raise ConfigError('registry revision must be a positive integer')
    entries = data['assistants']
    if not isinstance(entries, dict):
        raise ConfigError('registry assistants must be an object')
    for adapter_id, fields in entries.items():
        try:
            spec = get_adapter(adapter_id)
        except ValueError:
            raise ConfigError(f'{adapter_id}: registry lists an unknown adapter') from None
        if not isinstance(fields, dict) or not fields:
            raise ConfigError(f'{adapter_id}: registration fields required')
        if set(fields) - set(spec.registration_fields):
            raise ConfigError(f'{adapter_id}: unknown registration field')
        for name, value in fields.items():
            if not isinstance(value, str) or not value or not Path(value).is_absolute():
                raise ConfigError(f'{adapter_id}: absolute {name} required')
        if adapter_id == 'zzz-onedragon' and 'game_path' not in fields:
            raise ConfigError('zenless-zone-zero registration requires game EXE')
    return data


def empty_registry():
    return {'schema_version': REGISTRY_SCHEMA_VERSION, 'kind': REGISTRY_KIND,
            'revision': 1, 'assistants': {}}


class RegistrationStore:
    def __init__(self, data_root):
        self.root = Path(data_root).resolve()
        self.path = self.root / 'config' / 'assistant-registry.json'
        self.backups = self.root / 'backups' / 'registry'

    def _load_bytes(self):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ConfigError('external registry file')
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding='utf-8-sig'))
        except ValueError as error:
            raise ConfigError(f'登记库损坏，无法读取：{error}') from None

    def read(self):
        """Validated registry document, or None when no registry exists yet."""
        data = self._load_bytes()
        return None if data is None else validate_registry(data)

    def read_required(self):
        data = self.read()
        if data is None:
            raise RegistryMissing('全局登记不存在；请先完成迁移或首次初始化')
        return data

    def _check_boundaries(self):
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if (self.path.is_symlink() or parent.is_symlink()
                or not parent.resolve().is_relative_to(self.root)):
            raise ConfigError('external registry location')

    def create(self, data=None):
        """Create the initial library for a fresh data root, under the config lock."""
        document = empty_registry() if data is None else validate_registry(deepcopy(data))
        with config_lock(self.root):
            require_no_prepared_journal(self.root)
            if self.path.exists():
                raise ConfigError('registry already exists; use commit')
            self._check_boundaries()
            save(self.path, document)
            return deepcopy(document)

    def commit(self, adapter_id, inspection, expected_revision):
        """Publish one adapter's registration; back up the previous library first.

        Never writes profiles and never disables anything: run eligibility is
        decided by checks, not by the registration library.
        """
        from .registration import RegistrationInspection
        if not isinstance(inspection, RegistrationInspection):
            raise ConfigError('registration inspection required')
        row = inspection.candidate_row
        if row.get('adapter') != adapter_id:
            raise ConfigError('inspection adapter mismatch')
        spec = get_adapter(adapter_id)
        entry = {field: row[field] for field in spec.registration_fields if field in row}
        if not entry or set(entry) - set(spec.registration_fields):
            raise ConfigError('registration fields required')
        with config_lock(self.root):
            require_no_prepared_journal(self.root)
            current = self.read_required()
            if current['revision'] != expected_revision:
                raise RevisionConflict(
                    f'登记已被其他窗口修改（当前 revision {current["revision"]}，预期 {expected_revision}）；请刷新后重试')
            updated = deepcopy(current)
            if entry:
                updated['assistants'][adapter_id] = entry
            else:
                updated['assistants'].pop(adapter_id, None)
            updated['revision'] = expected_revision + 1
            validate_registry(updated)
            self._check_boundaries()
            # Back up the previous library before publishing; a failed backup
            # must leave the old file untouched.
            self.backups.mkdir(parents=True, exist_ok=True)
            if self.backups.is_symlink() or not self.backups.resolve().is_relative_to(self.root):
                raise ConfigError('external registry backup location')
            save(self.backups / (f'assistant-registry-{expected_revision}-{uuid.uuid4().hex}.json'), current)
            save(self.path, updated)
            return updated
