import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class SupportMatrixTests(unittest.TestCase):
    def test_committed_matrix_matches_catalog_and_requires_evidence_for_windows(self):
        from gachaops.compatibility import load_support_matrix, validate_support_matrix
        matrix = load_support_matrix()
        self.assertEqual(len(matrix['adapters']), 7)
        self.assertTrue(all(r['status'] == 'offline_verified' for r in matrix['adapters']))
        bad = copy.deepcopy(matrix)
        bad['adapters'][0].update(status='windows_verified', verified_versions=[])
        with self.assertRaises(ValueError): validate_support_matrix(bad)
        bad = copy.deepcopy(matrix); bad['adapters'].pop()
        with self.assertRaises(ValueError): validate_support_matrix(bad)

    def test_installation_stamp_detects_replacement_before_any_launch(self):
        from gachaops.compatibility import capture_installation_stamp, verify_installation_stamp
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / 'fixture.exe'; executable.write_bytes(b'fixture')
            version = root / 'version.json'; version.write_text('{"version":"old"}', encoding='utf-8')
            descriptor = {'installation_root': str(root), 'argv_candidate': [str(executable)],
                          'cwd': str(root), 'runner_version': 'old'}
            stamped = capture_installation_stamp(descriptor)
            verify_installation_stamp(stamped)
            version.write_text('{"version":"new"}', encoding='utf-8')
            with self.assertRaises(ValueError): verify_installation_stamp(stamped)
            version.unlink()
            with self.assertRaises(ValueError): verify_installation_stamp(stamped)

    def test_manual_ok_settings_and_device_changes_after_prepare_are_blocked(self):
        from gachaops.compatibility import capture_installation_stamp, verify_installation_stamp
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); exe = root / 'fixture.exe'; exe.touch()
            configs = root / 'data/apps/ok-ww/working/configs'; configs.mkdir(parents=True)
            basic = configs / 'Basic Options.json'; basic.write_text('{"Auto Start Game When App Starts":false}', encoding='utf-8')
            devices = configs / 'devices.json'; devices.write_text('{"pc_full_path":"fixture"}', encoding='utf-8')
            for path in (basic, devices):
                stamp = capture_installation_stamp({'installation_root': str(root), 'argv_candidate': [str(exe)]})
                old = path.read_bytes(); path.write_bytes(old + b' ')
                with self.assertRaises(ValueError): verify_installation_stamp(stamp)

    def test_drift_blocks_real_execution_entry_before_windows_process(self):
        from types import SimpleNamespace
        from gachaops.compatibility import capture_installation_stamp
        from gachaops.ok_trial import run_prepared
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); exe = root / 'fixture.exe'; exe.write_bytes(b'old')
            current = capture_installation_stamp({'installation_root': str(root), 'cwd': str(root),
                'argv_candidate': [str(exe)], 'game_path': str(root / 'game.exe'), 'runner_version': 'old'})
            exe.write_bytes(b'new')
            session = SimpleNamespace(record={'run_id': 'fixture'}, finish=lambda *args, **kwargs: None)
            with patch('gachaops.ok_trial.WindowsProcess', side_effect=AssertionError('must not launch')):
                report, _ = run_prepared(current, session, root, SimpleNamespace(offset=0), 1)
            self.assertEqual(report['status'], 'blocked')
            self.assertTrue(report['cleanup_verified'])
            self.assertFalse(report['game_execution'])
            self.assertTrue(report['repeat_blocked'])
