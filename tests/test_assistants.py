import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.assistants import load_assistants, prepare_assistant, run_assistants, check_assistants
from gachaops.planner import ConfigError
from gachaops.recovery import read_state, RecoveryRequired
from gachaops.execution_lock import ExecutionLock, ExecutionBusy


class AssistantTests(unittest.TestCase):
    def test_future_entries_can_be_disabled_unknown_and_never_prepared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = {'schema_version': 1, 'assistants': [
                {'id': adapter, 'adapter': adapter, 'installation_root': None,
                 'entry_executable': None, 'enabled': False}
                for adapter in ('bettergi',)]}
            path = root / 'assistants.json'
            path.write_text(json.dumps(data))
            loaded = load_assistants(path)
            with patch('gachaops.assistants.prepare_assistant') as prepare, \
                 patch('gachaops.assistants.run_prepared') as launch, \
                 patch('gachaops.assistants.windows_available', return_value=True):
                checked = check_assistants(loaded)
                report, _ = run_assistants(loaded, root / 'runs', root / 'state', progress=lambda _: None)
                prepare.assert_not_called()
                launch.assert_not_called()
            self.assertEqual(report['status'], 'nothing_enabled')
            self.assertTrue(all(r['integration_status'] == 'implemented' and r['status'] == 'disabled'
                                for r in checked['assistants']))
            data['assistants'][0]['enabled'] = True
            path.write_text(json.dumps(data))
            with self.assertRaises(ConfigError):
                load_assistants(path)

    def test_enabling_registered_future_adapter_blocks_before_native_read_or_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = {'schema_version': 1, 'assistants': [
                {'id': 'future', 'adapter': 'bettergi', 'installation_root': str(root),
                 'entry_executable': str(root / 'MaaGF2Exilium.exe'), 'enabled': True}]}
            path = root / 'assistants.json'
            path.write_text(json.dumps(data))
            loaded = load_assistants(path)
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.run_prepared') as launch:
                checked = check_assistants(loaded)
                report, _ = run_assistants(loaded, root / 'runs', root / 'state', progress=lambda _: None)
                launch.assert_not_called()
            self.assertEqual(checked['assistants'][0]['status'], 'blocked')
            self.assertIn('BetterGI', checked['assistants'][0]['error'])
            self.assertFalse(report['assistants'][0]['game_execution'])
            self.assertEqual(read_state(root / 'state')['phase'], 'idle')
            loaded['assistants'][0]['entry_executable'] = str(root.parent / 'outside.exe')
            path.write_text(json.dumps(loaded))
            with self.assertRaises(ConfigError):
                load_assistants(path)

    def data(self, root):
        return {'schema_version': 1, 'assistants': [
            {'id': 'one', 'adapter': 'ok-ww', 'installation_root': str(root), 'enabled': True},
            {'id': 'skip', 'adapter': 'ok-ww', 'installation_root': str(root), 'enabled': False},
            {'id': 'two', 'adapter': 'ok-ww', 'installation_root': str(root), 'enabled': True}]}

    def test_registration_rejects_internal_task_options_and_shell_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            data = self.data(Path(directory))
            for field, value in [('tasks', ['merge']), ('argv', ['cmd', '/c']),
                                 ('enabled', 'false'), ('timeout_seconds', float('nan')),
                                 ('timeout_seconds', 0), ('timeout_seconds', float('inf')),
                                 ('adapter', 'unknown')]:
                invalid = json.loads(json.dumps(data))
                invalid['assistants'][0][field] = value
                path.write_text(json.dumps(invalid), encoding='utf-8')
                with self.subTest(field=field), self.assertRaises(ConfigError):
                    load_assistants(path)
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(load_assistants(path), data)
            data['assistants'][0]['timeout_seconds'] = 10800
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(load_assistants(path), data)
            data['assistants'][2]['id'] = 'one'
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaises(ConfigError):
                load_assistants(path)

    def test_gui_preset_is_opaque_and_never_read_or_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'data/apps/ok-ww'
            working = app / 'working'
            (working / 'configs').mkdir(parents=True)
            (app / 'python').mkdir()
            (app / 'python/pythonw.exe').write_bytes(b'fixture')
            (working / 'main.py').write_text('')
            (working / 'config.py').write_text("config={'onetime_tasks': [['src.task.DailyTask', 'DailyTask']]}")
            (app / 'app.json').write_text(json.dumps({'current_version': 'future-version', 'current_profile': 'Global',
                                                     'profiles': [{'name': 'Global', 'admin': True}]}))
            game = root / 'game.exe'
            game.write_bytes(b'fixture')
            (working / 'configs/devices.json').write_text(json.dumps({'pc_full_path': str(game)}))
            preset = working / 'configs/DailyTask.json'
            with patch('gachaops.assistants.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.assistants.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.assistants.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                # Missing and syntactically invalid presets must not affect orchestration.
                first = prepare_assistant(self.data(root)['assistants'][0])
                for content in [b'invalid JSON internal to assistant', b'{"new_gui_schema": "any"}']:
                    preset.write_bytes(content)
                    second = prepare_assistant(self.data(root)['assistants'][0])
                    self.assertEqual(first, second)
                    self.assertEqual(preset.read_bytes(), content)
                self.assertEqual(first['blockers'], [])
                self.assertEqual(first['runner_version'], 'future-version')

    def test_chain_order_failure_continue_and_unknown_cleanup_stop(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log = root / 'logs/ok-script.log'
                log.parent.mkdir()
                log.write_text('old log\n')
                current = {'cwd': str(root), 'blockers': [], 'runner_version': 'fixture',
                           'task_configuration': 'assistant_current_saved_preset'}
                order = []
                def monitor(current, session, output, cursor, timeout, **kwargs):
                    with self.assertRaises(ExecutionBusy):
                        with ExecutionLock():
                            self.fail('chain lock lost')
                    identity = kwargs['metadata']['id']
                    order.append(identity)
                    return {'id': identity, 'status': 'partial_failure' if identity == 'one' else 'success',
                            'cleanup_verified': not uncertain}, output
                with patch('gachaops.assistants.windows_available', return_value=True), \
                     patch('gachaops.assistants.prepare_assistant', return_value=current), \
                     patch('gachaops.assistants.run_prepared', side_effect=monitor):
                    report, _ = run_assistants(self.data(root), root / 'runs', root / 'state', progress=lambda _: None)
                self.assertEqual(order, ['one'] if uncertain else ['one', 'two'])
                self.assertEqual(report['status'], 'stopped_cleanup_unconfirmed' if uncertain else 'completed_with_failures')
                self.assertEqual(read_state(root / 'state')['phase'], 'quarantined' if uncertain else 'idle')
                if uncertain:
                    with self.assertRaises(RecoveryRequired):
                        with patch('gachaops.assistants.windows_available', return_value=True):
                            run_assistants(self.data(root), root / 'runs', root / 'state', progress=lambda _: None)

    def test_prelaunch_blocked_node_continues_without_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.prepare_assistant', side_effect=ValueError('missing entry')), \
                 patch('gachaops.assistants.run_prepared') as monitor:
                report, _ = run_assistants(self.data(root), root / 'runs', root / 'state', progress=lambda _: None)
                monitor.assert_not_called()
            self.assertEqual([r['status'] for r in report['assistants']], ['blocked', 'disabled', 'blocked'])
            self.assertEqual(read_state(root / 'state')['phase'], 'idle')

    def test_interrupt_preserves_chain_evidence_and_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'logs').mkdir()
            (root / 'logs/ok-script.log').write_text('')
            current = {'cwd': str(root), 'blockers': [], 'runner_version': 'fixture', 'task_configuration': 'saved'}
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.prepare_assistant', return_value=current), \
                 patch('gachaops.assistants.run_prepared', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_assistants(self.data(root), root / 'runs', root / 'state', progress=lambda _: None)
            summary = next((root / 'runs').glob('assistants-*/summary.json'))
            self.assertEqual(json.loads(summary.read_text())['status'], 'interrupted')
            self.assertEqual(read_state(root / 'state')['phase'], 'quarantined')
