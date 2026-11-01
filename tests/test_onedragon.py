import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gachaops import onedragon
from gachaops.assistants import load_assistants, run_assistants
from gachaops.planner import ConfigError
from gachaops.ok_trial import run_prepared
from gachaops.recovery import ExecutionSession, read_state


FAKE_LAUNCHER = '''
from types import SimpleNamespace as NS
from pathlib import Path
import sys
class App:
    def __init__(self, app_id):
        self.app_id = app_id
        self.op_callback = lambda result: Path('callback-preserved').write_text('yes')
class ZApplicationLauncher:
    def create_context(self):
        apps = {i: App(i) for i in ('one_dragon', 'daily')}
        return NS(run_context=NS(get_application=lambda app_id: apps[app_id]))
    def run(self):
        assert sys.argv == ['zzz_application_launcher', '--close-game']
        self.ctx = self.create_context()
        scenario = Path('scenario').read_text()
        rc = self.ctx.run_context
        for app_id in ('daily', 'one_dragon'):
            app = rc.get_application(app_id)
            assert app is rc.get_application(app_id)
            app.op_callback(NS(success=(scenario != 'partial' or app_id != 'daily')))
        if scenario == 'exception':
            raise RuntimeError('must not leak account data')
        rc.last_run_result = NS(finish_reason='FAILED' if scenario == 'failed' else 'COMPLETED',
                                app_id='one_dragon', group_id='one_dragon')
        rc.last_application_result = NS(success=scenario != 'failed')
        Path('native-shutdown').write_text('yes')
'''


class OneDragonTests(unittest.TestCase):
    def registration(self, root):
        return {'schema_version': 1, 'assistants': [{'id': 'zzz', 'adapter': 'zzz-onedragon',
            'installation_root': str(root), 'game_path': str(root / 'game.exe')}]}

    def test_registration_observes_game_without_task_or_manual_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'config.json'
            data = self.registration(root)
            path.write_text(json.dumps(data))
            self.assertEqual(load_assistants(path), data)
            for changes in ({'game_path': ''}, {'game_start': 'manual'}, {'tasks': []}):
                invalid = self.registration(root)
                invalid['assistants'][0].update(changes)
                path.write_text(json.dumps(invalid))
                with self.subTest(changes=changes), self.assertRaises(ConfigError):
                    load_assistants(path)

    def fixture(self, root):
        paths = {
            '.venv/Scripts/python.exe': '', 'game.exe': '',
            '.venv/pyvenv.cfg': 'home = ' + str(root / '.install/python') + '\n',
            '.install/python/python.exe': '',
            'src/zzz_od/application/zzz_application_launcher.py': FAKE_LAUNCHER,
            'src/one_dragon/launcher/application_launcher.py':
                'def run_application():\n    close_game = after_app_shutdown\n',
            'src/one_dragon/base/operation/application/application_run_context.py':
                'def get_application():\n    last_run_result = last_application_result\n',
            'src/one_dragon/base/operation/operation.py':
                'def after_operation_done():\n    op_callback()\n',
            'src/one_dragon/version.py': "__version__ = 'fixture'\n",
            'config/01/application.yml': 'opaque future schema, not valid YAML',
        }
        for name, content in paths.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')

    def test_preflight_opaque_preset_contract_and_existing_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.fixture(root)
            with patch('gachaops.onedragon.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.onedragon.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.onedragon.find_matching_processes', return_value={'status': 'observed', 'matches': []}) as processes:
                result = onedragon.prepare(self.registration(root)['assistants'][0])
                self.assertEqual(result['blockers'], [])
                self.assertEqual(result['runner_version'], 'fixture')
                self.assertIn(str(root / '.venv/Scripts/pythonw.exe'), result['observation_targets'])
                self.assertEqual((root / 'config/01/application.yml').read_text(), 'opaque future schema, not valid YAML')
                processes.return_value = {'status': 'observed', 'matches': [{'pid': 1}]}
                self.assertIn('existing_runner_or_game_not_exclusive',
                              onedragon.prepare(self.registration(root)['assistants'][0])['blockers'])
                (root / 'src/one_dragon/base/operation/operation.py').write_text('')
                with self.assertRaisesRegex(ValueError, 'API changed'):
                    onedragon.prepare(self.registration(root)['assistants'][0])

    def test_bridge_saved_entry_callbacks_and_completion_protocol(self):
        for scenario, status in [('success', 'success'), ('partial', 'partial_failure'),
                                 ('failed', 'runner_failed'), ('exception', 'runner_failed')]:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root)
                (root / 'scenario').write_text(scenario)
                current = {'installation_root': str(root), 'argv_candidate': [sys.executable, '-B',
                    str(Path(onedragon.__file__).with_name('onedragon_bridge.py'))]}
                current, cursor = onedragon.bind_execution(current, root)
                process = subprocess.Popen(current['argv_candidate'], cwd=root)
                code = process.wait(timeout=10)
                result = onedragon.observe(cursor, code, process.pid)
                self.assertEqual(result['status'], status)
                self.assertEqual((root / 'callback-preserved').read_text(), 'yes')
                if scenario != 'exception':
                    self.assertTrue((root / 'native-shutdown').is_file())
                else:
                    self.assertNotIn('account data', cursor.path.read_text())
                self.assertEqual(onedragon.observe(cursor, code, process.pid + 1)['status'], 'completion_unconfirmed')
                with self.assertRaises(ValueError):
                    onedragon.bind_execution(current, root)
                if scenario == 'success':
                    evidence = json.loads(cursor.path.read_text())
                    owned = [{'pid': process.pid + 1, 'creation_ticks': 100, 'in_owned_job': True}]
                    evidence.update(pid=process.pid + 1, creation_ticks=100)
                    cursor.path.write_text(json.dumps(evidence))
                    self.assertEqual(onedragon.observe(cursor, code, process.pid, owned)['status'], 'success')
                    self.assertEqual(onedragon.observe(cursor, code, process.pid, [])['status'], 'completion_unconfirmed')
                    owned[0]['creation_ticks'] = 101
                    self.assertEqual(onedragon.observe(cursor, code, process.pid, owned)['status'], 'completion_unconfirmed')
                    evidence['application_results'] = []
                    cursor.path.write_text(json.dumps(evidence))
                    self.assertEqual(onedragon.observe(cursor, code, process.pid)['status'], 'completion_unconfirmed')

    def test_missing_result_and_wrong_nonce_never_success(self):
        with tempfile.TemporaryDirectory() as directory:
            cursor = onedragon.ResultCursor(Path(directory) / 'result.json', 'new-run')
            self.assertEqual(onedragon.observe(cursor, 0, 10)['status'], 'completion_unconfirmed')
            cursor.path.write_text(json.dumps({'schema_version': 1, 'token': 'old-run', 'pid': 10}))
            self.assertEqual(onedragon.observe(cursor, 0, 10)['status'], 'completion_unconfirmed')

    def test_onedragon_chain_routes_owned_monitor_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = {'installation_root': str(root), 'cwd': str(root), 'argv_candidate': ['fixture'],
                'completion_kind': 'onedragon', 'blockers': [], 'runner_version': 'fixture',
                'task_configuration': 'assistant_current_saved_preset'}
            def monitor(current, session, output, cursor, timeout, **kwargs):
                self.assertIs(kwargs['completion_observer'], onedragon.observe)
                self.assertIn(str(cursor.path), current['argv_candidate'])
                self.assertFalse(kwargs['finish_session'])
                return {'id': 'zzz', 'status': 'success', 'cleanup_verified': True}, output
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.prepare_assistant', return_value=current), \
                 patch('gachaops.assistants.run_prepared', side_effect=monitor):
                report, _ = run_assistants(self.registration(root), root / 'runs', root / 'state', progress=lambda _: None)
                self.assertEqual(report['status'], 'success')

    def test_monitor_accepts_result_only_from_observed_owned_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cursor = onedragon.ResultCursor(root / 'runner-result.json', 'fresh-token')
            game = {'pid': 200, 'creation_ticks': 30, 'image': str(root / 'game.exe'), 'in_owned_job': True}
            writer = {'pid': 101, 'creation_ticks': 20, 'image': str(root / 'base-python.exe'), 'in_owned_job': True}
            current = {'installation_root': str(root), 'cwd': str(root), 'game_path': game['image'],
                       'argv_candidate': [str(root / 'venv-python.exe')], 'observation_targets': [game['image']]}
            evidence = {'schema_version': 1, 'token': cursor.token, 'pid': writer['pid'],
                'creation_ticks': writer['creation_ticks'], 'shutdown_completed': True,
                'finish_reason': 'COMPLETED', 'operation_success': True, 'app_id': 'one_dragon',
                'group_id': 'one_dragon', 'application_results': [{'app_id': 'one_dragon', 'success': True}]}
            class Process:
                pid = 100
                def __init__(self, *args, **kwargs):
                    pass
                def __enter__(self):
                    return self
                def wait(self, timeout):
                    cursor.path.write_text(json.dumps(evidence))
                    return 0
                def __exit__(self, *_):
                    pass
            with patch('gachaops.ok_trial.WindowsProcess', Process), \
                 patch('gachaops.ok_trial.owned_processes', return_value=[writer, game]), \
                 patch('gachaops.ok_trial.find_matching_processes', return_value={'status': 'observed', 'matches': []}), \
                 patch('gachaops.ok_trial.verify_observed_membership', return_value=[]), \
                 patch('gachaops.ok_trial.runner_environment', return_value={}):
                with ExecutionSession(root / 'state', 'no-game-fixture') as session:
                    report, _ = run_prepared(current, session, root, cursor, 30, completion_observer=onedragon.observe)
            self.assertEqual(report['status'], 'success')
            self.assertEqual(report['completion']['writer_pid'], 101)
            self.assertTrue(report['cleanup_verified'])
            self.assertEqual(read_state(root / 'state')['phase'], 'idle')


if __name__ == '__main__':
    unittest.main()
