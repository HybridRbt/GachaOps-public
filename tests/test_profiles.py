import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.profiles import (PROFILE_SCHEMA_VERSION, ProfileStore, default_profile, file_digest,
                               profile_document, run_content, validate_profile_document,
                               validate_profile_rows, LegacyProfilesFound)


def write_profile(root, identity, document, name=None):
    directory = Path(root) / 'config/profiles' / identity
    directory.mkdir(parents=True)
    (directory / 'config.json').write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    (directory / 'name.json').write_text(json.dumps(
        {'name': name or identity, 'created': '2026-01-01T00:00:00+00:00'}), encoding='utf-8')


def schema1_document(root, enabled=False, **fields):
    return {'schema_version': 1, 'repeat_limit': 0, 'assistants': [
        dict({'id': 'wuthering-waves', 'adapter': 'ok-ww', 'installation_root': str(root),
              'enabled': enabled}, **fields)]}


class ProfileTests(unittest.TestCase):
    def test_default_profile_is_run_fields_only_and_first_start_creates_it(self):
        with tempfile.TemporaryDirectory() as temp:
            data = default_profile()
            self.assertEqual(len(data['assistants']), 7)
            self.assertTrue(all(r['enabled'] is False for r in data['assistants']))
            self.assertTrue(all(set(r) == {'id', 'adapter', 'enabled', 'game_start', 'timeout_seconds'}
                                for r in data['assistants']))
            store = ProfileStore(temp)
            rows = store.list_profiles()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['name'], '默认组合')
            document = store.load_profile(rows[0]['id'])
            self.assertEqual(document['schema_version'], PROFILE_SCHEMA_VERSION)
            self.assertEqual(document['kind'], 'assistant_profile')
            self.assertEqual(document['revision'], 1)
            self.assertEqual(run_content(document), data)

    def test_run_rows_reject_paths_unknown_fields_and_bad_values(self):
        base = default_profile()
        row = next(r for r in base['assistants'] if r['adapter'] == 'ok-ww')
        assistant_only = next(r for r in base['assistants'] if r['adapter'] == 'bettergi')
        for mutation in ({'installation_root': 'C:/Assistants/ok'}, {'entry_executable': 'C:/x.exe'},
                         {'game_path': 'C:/game.exe'}, {'prepared': True}, {'enabled': 'yes'},
                         {'timeout_seconds': 0}, {'timeout_seconds': -5}):
            with self.subTest(field=sorted(mutation)), self.assertRaises(ValueError):
                validate_profile_rows({'repeat_limit': 0,
                                       'assistants': [dict(row, **mutation)]})
        with self.assertRaises(ValueError):
            validate_profile_rows({'repeat_limit': 0,
                                   'assistants': [dict(assistant_only, game_start='manual')]})
        duplicate = [dict(row), dict(row)]
        with self.assertRaises(ValueError):
            validate_profile_rows({'repeat_limit': 0, 'assistants': duplicate})
        with self.assertRaises(ValueError):
            validate_profile_rows({'repeat_limit': -1, 'assistants': [row]})
        with self.assertRaises(ValueError):
            validate_profile_rows(dict(base, extra=1))

    def test_document_form_requires_schema2_header_and_positive_revision(self):
        data = default_profile()
        for mutation in ({'schema_version': 1}, {'kind': 'other'}, {'revision': 0}, {'revision': '2'},
                         {'unknown': 1}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_profile_document(dict(profile_document(data, 1), **mutation))
        self.assertEqual(profile_document(data, 4)['revision'], 4)

    def test_save_profile_rejects_smuggled_paths_and_stale_revisions(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            identity = store.list_profiles()[0]['id']
            data = run_content(store.load_profile(identity))
            smuggled = {'repeat_limit': 0, 'assistants': [
                dict(data['assistants'][0], installation_root='C:/Assistants/ok')]}
            with self.assertRaises(ValueError) as caught:
                store.save_profile(identity, smuggled, 1)
            self.assertIn('全局登记', str(caught.exception))
            document = store.save_profile(identity, dict(data, repeat_limit=2), 1)
            self.assertEqual(document['revision'], 2)
            with self.assertRaises(ValueError) as caught:
                store.save_profile(identity, data, 1)
            self.assertIn('其他窗口', str(caught.exception))
            with patch('gachaops.profiles.save', side_effect=PermissionError('fixture')):
                with self.assertRaises(PermissionError):
                    store.save_profile(identity, data, 2)
            self.assertEqual(store.load_profile(identity)['repeat_limit'], 2)
            self.assertTrue(list((Path(temp) / 'backups/profiles').rglob('*.json')))

    def test_save_profile_detects_out_of_band_edits_via_digest(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            identity = store.list_profiles()[0]['id']
            data = run_content(store.load_profile(identity))
            digest = file_digest(store._path(identity) / 'config.json')
            path = Path(temp) / 'config/profiles' / identity / 'config.json'
            # Same revision, different bytes: only the digest can catch it.
            edited = store.load_profile(identity); edited['repeat_limit'] = 5
            path.write_text(json.dumps(edited, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            with self.assertRaises(ValueError) as caught:
                store.save_profile(identity, data, 1, digest)
            self.assertIn('读取时不一致', str(caught.exception))
            store.save_profile(identity, data, 1, file_digest(path))

    def test_schema1_profiles_block_listing_until_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_profile(root, '11111111-1111-4111-8111-111111111111', schema1_document(root))
            store = ProfileStore(temp)
            with self.assertRaises(LegacyProfilesFound) as caught:
                store.list_profiles()
            self.assertEqual(caught.exception.identities, ['11111111-1111-4111-8111-111111111111'])

    def test_crud_names_are_not_paths_delete_last_creates_default(self):
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

    def test_corrupt_or_foreign_profile_storage_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = profile_document(default_profile(), 1)
            write_profile(root, '22222222-2222-4222-8222-222222222222', document, name='损坏')
            (root / 'config/profiles/22222222-2222-4222-8222-222222222222/config.json').write_text(
                '{"schema_version": 2}', encoding='utf-8')
            store = ProfileStore(temp)
            with self.assertRaises(ValueError):
                store.list_profiles()
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            identity = store.list_profiles()[0]['id']
            with patch.object(Path, 'is_symlink', return_value=True):
                with self.assertRaises(ValueError): store.load_profile(identity)
