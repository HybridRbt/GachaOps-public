from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.registration import inspect_registration


class RegistrationTests(unittest.TestCase):
    def test_all_seven_inspect_without_writing_profiles_or_launching(self):
        from gachaops.adapter_catalog import list_adapters
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / '中文 assistant'; root.mkdir()
            entry = root / 'entry.exe'; entry.touch()
            game = root / 'game.exe'; game.touch()
            for spec in list_adapters():
                fields = {'installation_root': str(root)}
                if 'entry_executable' in spec.registration_fields: fields['entry_executable'] = str(entry)
                if 'game_path' in spec.registration_fields: fields['game_path'] = str(game)
                with self.subTest(adapter=spec.adapter_id), \
                     patch('gachaops.registration.prepare_assistant',
                           return_value={'blockers': [], 'runner_version': 'fixture'}), \
                     patch('subprocess.Popen', side_effect=AssertionError('no launch')):
                    inspection = inspect_registration(spec.adapter_id, fields)
                    self.assertEqual(inspection.blockers, ())
                    row = inspection.candidate_row
                    self.assertEqual(row['adapter'], spec.adapter_id)
                    self.assertEqual(row['installation_root'], str(root.resolve()))
                    # game_start never becomes part of the stored candidate.
                    self.assertNotIn('game_start', row)
                    self.assertNotIn('enabled', row)
                    self.assertTrue(all(field in spec.registration_fields or field in ('id', 'adapter')
                                        for field in row))

    def test_game_start_participates_in_validation_only(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('gachaops.registration.prepare_assistant', return_value={'blockers': []}) as prepare:
                inspection = inspect_registration('ok-ww', {'installation_root': temp}, game_start='manual')
            self.assertNotIn('game_start', inspection.candidate_row)
            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.args[0]['game_start'], 'manual')
            with self.assertRaises(ValueError):
                inspect_registration('bettergi', {'installation_root': temp}, game_start='manual')

    def test_unsupported_version_still_yields_a_savable_inspection(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('gachaops.registration.prepare_assistant', side_effect=ValueError('version not verified')):
                inspection = inspect_registration('march7', {'installation_root': temp})
            self.assertIn('version_unsupported', inspection.blockers)
            self.assertEqual(inspection.candidate_row['installation_root'], str(Path(temp).resolve()))

    def test_external_entries_invalid_fields_missing_installation_and_manual_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); install = root / 'install'; install.mkdir(); outside = root / 'outside.exe'; outside.touch()
            for adapter, fields in [('march7', {'installation_root': str(install), 'entry_executable': str(outside)}),
                                    ('ok-nte', {'installation_root': str(install), 'game_start': 'manual'}),
                                    ('ok-ww', {'installation_root': str(install), 'argv': ['unsafe']}),
                                    ('ok-ww', {'installation_root': str(root / 'missing')}),
                                    ('zzz-onedragon', {'installation_root': str(install)})]:
                with self.subTest(adapter=adapter), self.assertRaises(ValueError):
                    inspect_registration(adapter, fields)
