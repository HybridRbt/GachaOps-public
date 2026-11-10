import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.assistants import load_assistants, check_assistants


class ProfileTests(unittest.TestCase):
    def test_delete_permanently_removes_only_selected_profile_without_new_backup(self):
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            first = store.create_profile('待删除')
            other = store.create_profile('保留')
            store.save_profile(first['id'], store.load_profile(first['id']))
            backups = {p.name: p.read_bytes() for p in store.backups.iterdir()}
            other_bytes = Path(other['path']).read_bytes()
            external = Path(temp) / 'native-install'
            external.mkdir(); (external / 'keep.txt').write_text('keep')
            store.delete_profile(first['id'])
            self.assertFalse(Path(first['path']).parent.exists())
            self.assertEqual([r['id'] for r in store.list_profiles()], [other['id']])
            self.assertEqual(Path(other['path']).read_bytes(), other_bytes)
            self.assertEqual({p.name for p in store.backups.iterdir()}, set(backups))
            self.assertEqual({p.name: p.read_bytes() for p in store.backups.iterdir()}, backups)
            self.assertEqual((external / 'keep.txt').read_text(), 'keep')

    def test_delete_permission_failure_preserves_selection_and_other_profile(self):
        import os
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            first = store.create_profile('待删除'); other = store.create_profile('保留')
            original = Path(first['path']).read_bytes()
            actual_replace = os.replace
            def deny_selected(source, destination):
                if Path(source) == Path(first['path']).parent:
                    raise PermissionError('fixture denied')
                return actual_replace(source, destination)
            with patch('os.replace', side_effect=deny_selected):
                with self.assertRaises(PermissionError): store.delete_profile(first['id'])
            self.assertEqual(Path(first['path']).read_bytes(), original)
            self.assertEqual({r['id'] for r in store.list_profiles()}, {first['id'], other['id']})

    def test_delete_last_failure_does_not_leave_an_extra_default_profile(self):
        import os
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            first = store.create_profile('唯一组合')
            actual_replace = os.replace
            def deny_selected(source, destination):
                if Path(source) == Path(first['path']).parent:
                    raise PermissionError('fixture denied')
                return actual_replace(source, destination)
            with patch('os.replace', side_effect=deny_selected):
                with self.assertRaises(PermissionError): store.delete_profile(first['id'])
            self.assertEqual([r['id'] for r in store.list_profiles()], [first['id']])

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows readonly deletion')
    def test_partial_delete_keeps_active_profiles_valid_and_reports_hidden_residue(self):
        import stat
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            first = store.create_profile('唯一组合')
            metadata = Path(first['path']).with_name('name.json')
            metadata.chmod(stat.S_IREAD)
            try:
                result = store.delete_profile(first['id'])
                self.assertFalse(result['cleanup_complete'])
                residue = Path(result['residual_path'])
                self.assertTrue(residue.name.startswith('.deleting-'))
                self.assertTrue(residue.exists())
                self.assertFalse(Path(first['path']).parent.exists())
                rows = store.list_profiles()
                self.assertEqual(len(rows), 1)
                self.assertNotEqual(rows[0]['id'], first['id'])
                self.assertEqual(store.load_profile(rows[0]['id'])['repeat_limit'], 0)
            finally:
                for path in store.directory.rglob('name.json'):
                    path.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_delete_rejects_reparse_profile_even_when_path_is_not_a_symlink(self):
        from types import SimpleNamespace
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            first = store.create_profile('重解析点')
            target = Path(first['path']).parent
            real_lstat = Path.lstat
            def reparse(path):
                result = real_lstat(path)
                return (SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
                        if path == target else result)
            with patch.object(Path, 'lstat', reparse):
                with self.assertRaises(ValueError): store.delete_profile(first['id'])
            self.assertTrue(Path(first['path']).exists())

    def test_external_profile_startup_error_names_entry_and_recovery_steps(self):
        import re
        from types import SimpleNamespace
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            first = store.create_profile('外部组合')
            target = Path(first['path']).parent
            real_lstat = Path.lstat
            def reparse(path):
                result = real_lstat(path)
                return (SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
                        if path == target else result)
            with patch.object(Path, 'lstat', reparse):
                # Startup surfaces this message verbatim, so it must identify the
                # affected profile directory and say how to recover safely.
                with self.assertRaisesRegex(ValueError, re.escape(str(target))) as caught:
                    store.list_profiles()
            self.assertIn('恢复', str(caught.exception))

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows junction boundary')
    def test_delete_rejects_junction_profile_and_preserves_external_directory(self):
        import subprocess
        import uuid
        from gachaops.profiles import ProfileStore
        with tempfile.TemporaryDirectory() as temp:
            store = ProfileStore(temp)
            store.create_profile('保留')
            external_store = ProfileStore(Path(temp) / 'external')
            external = external_store.create_profile('外部')
            identity = str(uuid.uuid4()); link = store.directory / identity
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(Path(external['path']).parent)],
                                    capture_output=True)
            if result.returncode: self.skipTest('junction creation unavailable')
            try:
                with self.assertRaises(ValueError): store.delete_profile(identity)
                self.assertTrue(Path(external['path']).exists())
            finally:
                if link.exists(): link.rmdir()

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
