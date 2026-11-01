import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gachaops import maaend


class EndfieldNativeStartTests(unittest.TestCase):
    def exercise(self, mutate=None, manual=False, matches=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for folder in ('config', 'maafw', 'tasks/pretasks', 'agent', 'gryph/1.6.0', 'gryph/games/Endfield'):
                (root / folder).mkdir(parents=True, exist_ok=True)
            launcher = root / 'gryph/Launcher.exe'; launcher.write_bytes(b'fixture')
            (root / 'gryph/1.6.0/Games.exe').write_bytes(b'fixture')
            game = root / 'gryph/games/Endfield/Endfield.exe'; game.write_bytes(b'fixture')
            (root / 'MaaEnd.exe').write_bytes(b'--autostart --instance --quit-after-run')
            (root / 'maafw/MaaFramework.dll').write_bytes(b'fixture')
            (root / 'agent/go-service.exe').write_bytes(b'fixture')
            (root / 'interface.json').write_text(json.dumps({'version': 'v2.34.0'}))
            (root / 'tasks/pretasks/LaunchGamePC.json').write_text(json.dumps({'pretask': {
                'name': 'LaunchGamePC', 'exec': 'agent/go-service', 'args': ['--pretask', 'LaunchGamePC']}}))
            task = {'taskName': '__MXU_PRETASK__LaunchGamePC', 'enabled': True, 'optionValues': {
                'LaunchGamePCRegion': {'type': 'select', 'caseName': 'Global'},
                'LaunchGamePCLauncherPath': {'type': 'select', 'caseName': 'Auto'}}}
            instance = {'id': 'daily', 'name': '全套日常', 'controllerName': 'Win32-Front',
                        'tasks': [task], 'preActions': []}
            if mutate: mutate(root, instance)
            path = root / 'config/mxu-MaaEnd.json'
            path.write_text(json.dumps({'lastActiveInstanceId': 'daily', 'instances': [instance]}))
            before = path.read_bytes()
            row = {'installation_root': str(root), 'game_start': 'manual' if manual else 'assistant'}
            with patch.object(maaend, 'launcher_install_dir', return_value=root / 'gryph', create=True), \
                 patch.object(maaend, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(maaend, 'desktop_report', return_value={'status': 'observed_available'}), \
                 patch.object(maaend, 'find_matching_processes', return_value={
                     'status': 'observed', 'matches': matches or []}) as observe:
                result = maaend.prepare(row)
            self.assertEqual(path.read_bytes(), before)
            return result, observe.call_args.args[0], str(game), str(launcher)

    def test_native_global_auto_accepted_without_legacy_preaction(self):
        try:
            result, targets, game, launcher = self.exercise()
        except ValueError as exc:
            self.fail(f'valid native startup rejected: {exc}')
        self.assertEqual(result['blockers'], [])
        self.assertEqual(result['game_path'], game)
        self.assertIn(launcher, targets)
        self.assertTrue(any(p.endswith('Games.exe') for p in targets))
        self.assertEqual(result['argv_candidate'][-4:], ['--autostart', '--instance', '全套日常', '--quit-after-run'])

    def test_missing_ambiguous_paths_or_duplicate_start_blocks(self):
        def duplicate_game(root, instance):
            other = root / 'gryph/games/Other'; other.mkdir()
            (other / 'Endfield.exe').write_bytes(b'fixture')
        def conflicting_action(root, instance):
            instance['preActions'] = [{'enabled': True, 'program': str(root/'other.exe'), 'useCmd': False}]
        mutations = [duplicate_game, conflicting_action,
            lambda r,i: (r/'gryph/Launcher.exe').unlink(),
            lambda r,i: (r/'gryph/games/Endfield/Endfield.exe').unlink(),
            lambda r,i: (r/'agent/go-service.exe').unlink(),
            lambda r,i: i['tasks'].append(dict(i['tasks'][0])),
            lambda r,i: i['tasks'][0]['optionValues'].pop('LaunchGamePCLauncherPath'),
            lambda r,i: i['tasks'][0]['optionValues']['LaunchGamePCLauncherPath'].update(type='input'),
            lambda r,i: i['tasks'][0]['optionValues']['LaunchGamePCRegion'].update(caseName='Steam')]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.exercise(mutation)

    def test_enabled_native_pretask_not_allowed_in_manual_mode(self):
        result, _, _, _ = self.exercise(manual=True)
        self.assertIn('maaend_disable_native_launch_task', result['blockers'])

    def test_disabled_native_task_only_allowed_with_manual_game_identity(self):
        def disable(root, instance): instance['tasks'][0]['enabled'] = False
        with self.assertRaises(ValueError): self.exercise(disable)
        result, _, _, _ = self.exercise(disable, manual=True)
        self.assertNotIn('maaend_disable_native_launch_task', result['blockers'])
        self.assertIn('manual_game_requires_exactly_one_running_instance', result['blockers'])

    def test_external_launcher_blocks_exclusivity(self):
        result, _, _, _ = self.exercise(matches=[{'pid': 8, 'image': 'launcher'}])
        self.assertIn('existing_runner_or_game_not_exclusive', result['blockers'])
