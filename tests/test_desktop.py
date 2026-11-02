import json
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from gachaops.app_paths import workspace_root
from gachaops.desktop_model import selected_configuration, save_configuration, worker_command
from gachaops.assistants import run_assistants
from gachaops.ok_trial import run_prepared
from gachaops.ok_completion import capture_cursor
from gachaops.recovery import ExecutionSession, read_state
from gachaops.windows_process import kernel_api
from gachaops.ok_adapter import runner_environment


class DesktopTests(unittest.TestCase):
    def test_selection_is_a_snapshot_and_pending_cannot_be_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = {'schema_version': 1, 'assistants': [
                {'id': 'first', 'adapter': 'ok-ww', 'installation_root': str(root), 'enabled': True},
                {'id': 'second', 'adapter': 'ok-ww', 'installation_root': str(root), 'enabled': False},
                {'id': 'pending', 'adapter': 'bettergi', 'installation_root': None, 'enabled': False}]}
            snapshot = selected_configuration(data, ['second', 'first', 'pending'], ['second'])
            self.assertEqual([r['id'] for r in snapshot['assistants']], ['second', 'first', 'pending'])
            self.assertEqual([r['id'] for r in data['assistants']], ['first', 'second', 'pending'])
            self.assertTrue(data['assistants'][0]['enabled'])
            self.assertEqual(save_configuration(root / 'gui/config.json', snapshot), snapshot)
            with self.assertRaises(ValueError): selected_configuration(data, ['first', 'first', 'pending'], [])
            with self.assertRaises(ValueError): selected_configuration(data, ['first', 'second', 'pending'], ['pending'])

    def test_packaged_paths_preserve_legacy_journal_and_unbound_copy_uses_local_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir(); (root / 'gachaops').mkdir()
            (root / 'gachaops/__init__.py').touch(); (root / 'dist').mkdir()
            with patch('sys.frozen', True, create=True), patch('sys.executable', str(root / 'dist/GachaOps.exe')):
                self.assertEqual(workspace_root(), root.resolve())
                command = worker_command('check-assistants', root / 'config/a file.json', root, root / 'stop', root / 'log')
                self.assertEqual(command[:2], [str(root / 'dist/GachaOps.exe'), '--worker'])
                self.assertIn(str(root / 'config/a file.json'), command)
            with tempfile.TemporaryDirectory() as isolated, patch('sys.frozen', True, create=True), \
                 patch('sys.executable', str(Path(isolated) / 'GachaOps.exe')), \
                 patch.dict(os.environ, LOCALAPPDATA=str(Path(isolated) / 'local-data')):
                self.assertEqual(workspace_root(), (Path(isolated) / 'local-data/GachaOps').resolve())
            other = root / 'isolated'; other.mkdir()
            (other / 'gachaops-workspace.json').write_text(json.dumps({'schema_version': 1, 'workspace': str(root)}))
            with patch('sys.frozen', True, create=True), patch('sys.executable', str(other / 'GachaOps.exe')):
                self.assertEqual(workspace_root(), root.resolve())

    def test_packaged_qt_and_bootloader_settings_do_not_leak_to_assistants(self):
        source = dict(os.environ, SYSTEMROOT=str(Path(tempfile.gettempdir()).resolve()),
                      QT_PLUGIN_PATH='bundled-qt', QML2_IMPORT_PATH='bundled-qml',
                      _PYI_ARCHIVE_FILE='desktop.exe', PYINSTALLER_RESET_ENVIRONMENT='1')
        with patch('sys.frozen', True, create=True):
            env = runner_environment(source)
        for key in ('QT_PLUGIN_PATH', 'QML2_IMPORT_PATH', '_PYI_ARCHIVE_FILE', 'PYINSTALLER_RESET_ENVIRONMENT'):
            self.assertNotIn(key, env)
        self.assertEqual(source['QT_PLUGIN_PATH'], 'bundled-qt')

    def test_stop_before_launch_does_not_prepare_or_start_an_assistant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = {'assistants': [{'id': 'first', 'enabled': True}]}
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.prepare_assistant') as prepare:
                result, _ = run_assistants(data, root / 'runs', root / 'state', stop_requested=lambda: True)
            prepare.assert_not_called()
            self.assertEqual(result['status'], 'cancelled')
            self.assertEqual(read_state(root / 'state')['phase'], 'idle')

    @unittest.skipUnless(os.name == 'nt', 'native Windows cancellation fixture')
    def test_cooperative_stop_cleans_real_fixture_child_and_unlocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); native = root / 'native.log'; native.touch()
            pid_file = root / 'child.json'
            current = {'installation_root': str(root), 'cwd': str(root), 'game_path': sys.executable,
                       'argv_candidate': [sys.executable, str(Path(__file__).parent / 'fixtures/process_tree.py'),
                                          str(pid_file), 'hold']}
            api = kernel_api(); api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            api.OpenProcess.restype = w.HANDLE
            retained = []
            def stop():
                if not pid_file.exists(): return False
                try: child = json.loads(pid_file.read_text())['child_pid']
                except ValueError: return False
                handle = api.OpenProcess(0x100000, False, child)
                self.assertTrue(handle); retained.append(handle)
                return True
            try:
                # Only matching-process discovery is isolated from the test
                # interpreter. Process creation, Job membership and cleanup are real.
                with patch('gachaops.ok_trial.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                    with ExecutionSession(root / 'state', 'desktop_stop_fixture') as session:
                        result, _ = run_prepared(current, session, root, capture_cursor(native), 10, stop_requested=stop)
                self.assertEqual(result['status'], 'cancelled'); self.assertTrue(result['cleanup_verified'])
                self.assertTrue(retained); self.assertEqual(api.WaitForSingleObject(retained[0], 5000), 0)
                self.assertEqual(read_state(root / 'state')['phase'], 'idle')
            finally:
                for handle in retained: api.CloseHandle(handle)

    def test_desktop_interruption_cleans_owned_job_and_stops_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); log = root / 'native.log'; log.touch()
            current = {'installation_root': str(root), 'cwd': str(root), 'game_path': str(root / 'game.exe'),
                       'argv_candidate': [str(root / 'assistant.exe')]}
            cleaned = []
            class Process:
                pid = 123
                def __init__(self, *args, **kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *_): cleaned.append(True)
                def wait(self, *_): raise AssertionError('desktop loss must precede input/wait')
            with patch('gachaops.ok_trial.WindowsProcess', Process), \
                 patch('gachaops.ok_trial.find_matching_processes', return_value={'status': 'observed', 'matches': []}), \
                 patch('gachaops.ok_trial.runner_environment', return_value={}), \
                 patch('gachaops.ok_trial.request_foreground') as focus:
                with ExecutionSession(root / 'state', 'desktop_fixture') as session:
                    checks = iter([{'status': 'observed_available'},
                                   {'status': 'blocked', 'reason': 'input_desktop_inaccessible', 'error': 5}])
                    result, _ = run_prepared(current, session, root, capture_cursor(log), 30,
                                             desktop_check=lambda: next(checks))
                focus.assert_not_called()
            self.assertEqual(cleaned, [True])
            self.assertEqual(result['status'], 'desktop_environment_interrupted')
            self.assertTrue(result['cleanup_verified'])
            self.assertEqual(result['desktop_interruption']['error'], 5)
            self.assertEqual(read_state(root / 'state')['phase'], 'idle')
            (root / 'logs').mkdir(); (root / 'logs/ok-script.log').touch()
            prepared = dict(current, blockers=[], runner_version='fixture',
                            task_configuration='assistant_current_saved_preset')
            data = {'assistants': [{'id': 'first', 'enabled': True}, {'id': 'second', 'enabled': True}]}
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.prepare_assistant', return_value=prepared), \
                 patch('gachaops.assistants.run_prepared', return_value=(result, root)) as launch:
                chain, _ = run_assistants(data, root / 'runs', root / 'state', progress=lambda _: None)
            self.assertEqual(chain['status'], 'desktop_environment_interrupted')
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(read_state(root / 'state')['phase'], 'idle')
            with patch('gachaops.ok_trial.WindowsProcess') as process:
                with ExecutionSession(root / 'state', 'prelaunch_desktop_fixture') as session:
                    result, _ = run_prepared(current, session, root, capture_cursor(log), 30,
                                             desktop_check=lambda: {'status': 'blocked', 'reason': 'locked'})
                process.assert_not_called()
            self.assertFalse(result['game_execution'])
            self.assertTrue(result['cleanup_verified'])
            self.assertEqual(result['game_path'], current['game_path'])
            self.assertIn(current['game_path'], result['observation_targets'])
            self.assertIn(current['argv_candidate'][0], result['observation_targets'])

    def test_stop_running_node_verifies_cleanup_and_stops_the_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); log = root / 'native.log'; log.touch()
            owned = {'pid': 123, 'image': str(root / 'fixture.exe'), 'creation_ticks': 10, 'in_owned_job': True}
            current = {'installation_root': str(root), 'cwd': str(root), 'game_path': owned['image'],
                       'argv_candidate': [owned['image']]}
            cleaned = []
            class Process:
                pid = 123
                def __init__(self, *args, **kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *_): cleaned.append(True)
                def wait(self, *_): raise AssertionError('stop should precede wait')
            with patch('gachaops.ok_trial.WindowsProcess', Process), \
                 patch('gachaops.ok_trial.owned_processes', return_value=[owned]), \
                 patch('gachaops.ok_trial.find_matching_processes', return_value={'status': 'observed', 'matches': []}), \
                 patch('gachaops.ok_trial.verify_observed_membership', return_value=[]), \
                 patch('gachaops.ok_trial.runner_environment', return_value={}):
                with ExecutionSession(root / 'state', 'fixture') as session:
                    stops = iter([False, True])
                    result, _ = run_prepared(current, session, root, capture_cursor(log), 30,
                                             stop_requested=lambda: next(stops))
            self.assertEqual(cleaned, [True]); self.assertEqual(result['status'], 'cancelled')
            self.assertTrue(result['cleanup_verified']); self.assertEqual(read_state(root / 'state')['phase'], 'idle')
            (root / 'working/logs').mkdir(parents=True); (root / 'working/logs/ok-script.log').touch()
            data = {'assistants': [{'id': 'first', 'enabled': True}, {'id': 'second', 'enabled': True}]}
            prepared = {'cwd': str(root / 'working'), 'blockers': [], 'runner_version': 'fixture',
                        'task_configuration': 'assistant_current_saved_preset'}
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.prepare_assistant', return_value=prepared), \
                 patch('gachaops.assistants.run_prepared', return_value=(result, root)) as launch:
                chain, _ = run_assistants(data, root / 'runs', root / 'state', progress=lambda _: None,
                                          stop_requested=lambda: False)
            self.assertEqual(chain['status'], 'cancelled'); self.assertEqual(launch.call_count, 1)
            self.assertEqual(read_state(root / 'state')['phase'], 'idle')
