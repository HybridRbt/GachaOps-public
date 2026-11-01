import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.assistants import load_assistants, check_assistants


class ProfileTests(unittest.TestCase):
    def test_first_start_is_seven_unregistered_disabled_and_empty_list_is_valid(self):
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            rows = store.list_profiles()
            self.assertEqual(len(rows), 1)
            config = store.load_profile(rows[0]['id'])
            self.assertEqual(len(config['assistants']), 7)
            self.assertTrue(all(r['enabled'] is False and r['installation_root'] is None for r in config['assistants']))
            self.assertEqual(config['repeat_limit'], 0)
            path = Path(temp) / 'empty.json'
            path.write_text(json.dumps({'schema_version': 1, 'assistants': []}), encoding='utf-8')
            self.assertEqual(check_assistants(load_assistants(path))['assistants'], [])
            zzz = next(r for r in config['assistants'] if r['adapter'] == 'zzz-onedragon')
            zzz['enabled'] = True
            with self.assertRaises(ValueError): store.save_profile(rows[0]['id'], config)

    def test_crud_names_are_not_paths_delete_last_creates_default(self):
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            first = store.create_profile('中文 / duplicate')
            other = store.create_profile('中文 / duplicate')
            self.assertNotEqual(first['id'], other['id'])
            store.rename_profile(first['id'], '../显示名称')
            self.assertEqual(store.list_profiles()[0]['name'], '../显示名称')
            with self.assertRaises(ValueError): store.load_profile('../escape')
            store.delete_profile(first['id']); store.delete_profile(other['id'])
            self.assertEqual(len(store.list_profiles()), 1)
            self.assertEqual(store.list_profiles()[0]['name'], '默认组合')

    def test_import_preview_cancel_and_save_preserve_order_and_settings(self):
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            identity = store.list_profiles()[0]['id']
            data = store.load_profile(identity)
            data['repeat_limit'] = 2
            data['assistants'].reverse()
            data['assistants'][0]['timeout_seconds'] = 1234
            source = Path(temp) / 'old.json'
            source.write_text(json.dumps(data), encoding='utf-8')
            preview = store.preview_import(source)
            self.assertNotEqual(store.load_profile(identity), preview['configuration'])
            store.import_profile('导入', preview)
            imported = store.list_profiles()[-1]
            self.assertEqual(store.load_profile(imported['id']), data)
            store.save_profile(identity, data)
            original = store.load_profile(identity)
            with patch('gachaops.profiles.save', side_effect=PermissionError('fixture')):
                with self.assertRaises(PermissionError): store.save_profile(identity, dict(data, repeat_limit=3))
            self.assertEqual(store.load_profile(identity), original)
            self.assertTrue(list((Path(temp) / 'backups/profiles').rglob('*.json')))
            source.write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError): store.import_profile('changed', preview)

    def test_unknown_metadata_future_schema_and_duplicate_ids_are_rejected(self):
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            identity = store.list_profiles()[0]['id']
            base = store.load_profile(identity)
            for data in (dict(base, schema_version=2), dict(base, prepared=True),
                         dict(base, assistants=[base['assistants'][0], base['assistants'][0]])):
                with self.assertRaises(ValueError): store.save_profile(identity, data)
