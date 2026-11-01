"""Local user profiles with UUID storage, backups and explicit import previews."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import uuid

from .adapter_catalog import list_adapters
from .assistants import validate_assistants, load_assistants
from .recovery import save


def default_configuration():
    timeouts = {'zzz-onedragon': 3600, 'maa-gf2': 3600, 'maa-end': 7200, 'march7': 3600}
    rows = []
    for spec in list_adapters():
        row = {'id': spec.game_id, 'adapter': spec.adapter_id, 'installation_root': None,
               'enabled': False, 'game_start': 'assistant', 'timeout_seconds': timeouts.get(spec.adapter_id, 1800)}
        if 'entry_executable' in spec.registration_fields:
            row['entry_executable'] = None
        if 'game_path' in spec.registration_fields:
            row['game_path'] = None
        rows.append(row)
    return {'schema_version': 1, 'repeat_limit': 0, 'assistants': rows}


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

    def list_profiles(self):
        if self.directory.is_symlink():
            raise ValueError('external profiles directory')
        rows = []
        if self.directory.exists():
            for path in self.directory.iterdir():
                if path.name.startswith('.'):
                    continue
                path = self._path(path.name)
                metadata = json.loads((path / 'name.json').read_text(encoding='utf-8'))
                if (not isinstance(metadata, dict) or set(metadata) != {'name', 'created'}
                        or not isinstance(metadata.get('created'), str)):
                    raise ValueError('invalid profile metadata')
                rows.append({'id': path.name, 'name': self._name(metadata['name']),
                             'path': str(path / 'config.json'), 'created': metadata['created']})
        if not rows:
            self.create_profile('默认组合')
            return self.list_profiles()
        return tuple(sorted(rows, key=lambda r: r['created']))

    def create_profile(self, name, configuration=None):
        from datetime import datetime, timezone
        name = self._name(name)
        data = deepcopy(validate_assistants(configuration if configuration is not None else default_configuration()))
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

    def load_profile(self, identity):
        path = self._path(identity) / 'config.json'
        if path.is_symlink(): raise ValueError('external profile configuration')
        return load_assistants(path)

    def _backup(self, path):
        self.backups.mkdir(parents=True, exist_ok=True)
        destination = self.backups / (path.parent.name + '-' + uuid.uuid4().hex + '-' + path.name)
        # Saving the old JSON atomically is mandatory before replacing it.
        save(destination, json.loads(path.read_text(encoding='utf-8')))

    def save_profile(self, identity, data):
        data = deepcopy(validate_assistants(data))
        path = self._path(identity) / 'config.json'
        self._backup(path)
        save(path, data)
        return self.load_profile(identity)

    def rename_profile(self, identity, name):
        path = self._path(identity) / 'name.json'
        metadata = json.loads(path.read_text(encoding='utf-8'))
        metadata['name'] = self._name(name)
        self._backup(path)
        save(path, metadata)

    def delete_profile(self, identity):
        path = self._path(identity)
        rows = self.list_profiles()
        if len(rows) == 1:
            self.create_profile('默认组合')
        self.backups.mkdir(parents=True, exist_ok=True)
        os.replace(path, self.backups / ('deleted-' + identity + '-' + uuid.uuid4().hex))

    def preview_import(self, path):
        source = Path(path).resolve(strict=True)
        raw = source.read_bytes()
        return {'source': str(source), 'sha256': hashlib.sha256(raw).hexdigest(),
                'configuration': deepcopy(validate_assistants(json.loads(raw.decode('utf-8-sig'))))}

    def import_profile(self, name, preview):
        if self.preview_import(preview['source']) != preview:
            raise ValueError('import source changed; preview again')
        return self.create_profile(name, preview['configuration'])


def list_profiles(data_root):
    return ProfileStore(data_root).list_profiles()
