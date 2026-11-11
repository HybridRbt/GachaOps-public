import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class RegistrationTests(unittest.TestCase):
    def test_reregister_without_optional_entry_clears_stale_path_after_install_change(self):
        from gachaops.profiles import default_configuration
        from gachaops.registration import inspect_registration, commit_registration
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_root = root / 'old'; old_root.mkdir()
            new_root = root / 'new'; new_root.mkdir()
            data = default_configuration()
            row = next(r for r in data['assistants'] if r['adapter'] == 'maa-gf2')
            row.update(installation_root=str(old_root), entry_executable=str(old_root / 'old.exe'),
                       enabled=True, timeout_seconds=2400)
            before = copy.deepcopy(data)
            with patch('gachaops.registration.prepare_assistant', return_value={'blockers': []}) as prepare:
                inspection = inspect_registration('maa-gf2', {'installation_root': str(new_root)})
            self.assertNotIn('entry_executable', prepare.call_args.args[0])
            updated = commit_registration(data, inspection, row['id'])
            changed = next(r for r in updated['assistants'] if r['id'] == row['id'])
            self.assertFalse(changed.get('entry_executable'))
            self.assertEqual(changed['installation_root'], str(new_root.resolve()))
            self.assertTrue(changed['enabled'])
            self.assertEqual(changed['timeout_seconds'], 2400)
            self.assertEqual([r['id'] for r in updated['assistants']], [r['id'] for r in data['assistants']])
            self.assertEqual(data, before)

    def test_all_seven_inspect_commit_keep_order_selection_timeout_and_never_launch(self):
        from gachaops.adapter_catalog import list_adapters
        from gachaops.profiles import default_configuration
        from gachaops.registration import inspect_registration, commit_registration
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / '中文 assistant'; root.mkdir()
            entry = root / 'entry.exe'; entry.touch()
            game = root / 'game.exe'; game.touch()
            profile = default_configuration()
            profile['assistants'].reverse()
            for spec in list_adapters():
                fields = {'installation_root': str(root)}
                if 'entry_executable' in spec.registration_fields: fields['entry_executable'] = str(entry)
                if 'game_path' in spec.registration_fields: fields['game_path'] = str(game)
                with self.subTest(adapter=spec.adapter_id), \
                     patch('gachaops.registration.prepare_assistant', return_value={'blockers': [], 'runner_version': 'fixture'}), \
                     patch('subprocess.Popen', side_effect=AssertionError('no launch')):
                    inspection = inspect_registration(spec.adapter_id, fields)
                    updated = commit_registration(profile, inspection)
                    self.assertEqual([r['id'] for r in updated['assistants']], [r['id'] for r in profile['assistants']])
                    row = next(r for r in updated['assistants'] if r['adapter'] == spec.adapter_id)
                    self.assertFalse(row['enabled'])
                    self.assertEqual(row['installation_root'], str(root.resolve()))
                    self.assertEqual(row['timeout_seconds'], next(r['timeout_seconds'] for r in profile['assistants'] if r['adapter'] == spec.adapter_id))
                    self.assertTrue(all(r['installation_root'] is None for r in profile['assistants']))

    def test_unsupported_version_can_be_saved_only_disabled(self):
        from gachaops.profiles import default_configuration
        from gachaops.registration import inspect_registration, commit_registration
        with tempfile.TemporaryDirectory() as temp:
            with patch('gachaops.registration.prepare_assistant', side_effect=ValueError('version not verified')):
                inspection = inspect_registration('march7', {'installation_root': temp})
            self.assertIn('version_unsupported', inspection.blockers)
            data = default_configuration()
            data['assistants'][-1]['enabled'] = True
            updated = commit_registration(data, inspection)
            self.assertFalse(next(r for r in updated['assistants'] if r['adapter'] == 'march7')['enabled'])

    def test_external_entries_invalid_fields_missing_installation_and_manual_are_rejected(self):
        from gachaops.registration import inspect_registration
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); install = root / 'install'; install.mkdir(); outside = root / 'outside.exe'; outside.touch()
            for adapter, fields in [('march7', {'installation_root': str(install), 'entry_executable': str(outside)}),
                                    ('ok-nte', {'installation_root': str(install), 'game_start': 'manual'}),
                                    ('ok-ww', {'installation_root': str(install), 'argv': ['unsafe']}),
                                    ('ok-ww', {'installation_root': str(root / 'missing')})]:
                with self.subTest(adapter=adapter), self.assertRaises(ValueError): inspect_registration(adapter, fields)

    def test_edit_preserves_enabled_when_safe_and_cancel_has_no_side_effect(self):
        from gachaops.profiles import default_configuration
        from gachaops.registration import inspect_registration, commit_registration
        with tempfile.TemporaryDirectory() as temp:
            data = default_configuration(); data['assistants'][0].update(enabled=True, game_start='manual', installation_root=temp)
            before = copy.deepcopy(data)
            with patch('gachaops.registration.prepare_assistant', return_value={'blockers': []}):
                inspection = inspect_registration('ok-ww', {'installation_root': temp, 'game_start': 'manual'})
            self.assertEqual(data, before)
            updated = commit_registration(data, inspection)
            self.assertTrue(updated['assistants'][0]['enabled'])
            self.assertEqual(updated['assistants'][0]['game_start'], 'manual')
