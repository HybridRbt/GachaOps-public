import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gachaops.assistants import prepare_assistant, run_assistants
from gachaops.ok_trial import run_prepared
from gachaops.ok_completion import capture_cursor
from gachaops.recovery import ExecutionSession, read_state

START = '2026-10-01 00:00:00,000 INFO MainThread start_controller:enabled task <src.task.DailyTask.DailyTask object at 0x123>'
DONE = '2026-10-01 00:00:01,000 INFO TaskExecutor DailyTask:Daily Task Completed'
EXIT = '2026-10-01 00:00:01,000 INFO TaskExecutor TaskExecutor:Successfully Executed Task, Exiting Game and App!'


class ManualGameTests(unittest.TestCase):
    def test_manual_preflight_requires_game_and_disabled_auto_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'data/apps/ok-ww'
            working = app / 'working'
            (working / 'configs').mkdir(parents=True)
            (app / 'python').mkdir()
            (app / 'python/pythonw.exe').write_bytes(b'fixture')
            (working / 'main.py').write_text('')
            (working / 'config.py').write_text("config={'onetime_tasks': [['src.task.DailyTask', 'DailyTask']]}")
            (app / 'app.json').write_text(json.dumps({'current_profile': 'Global', 'profiles': [{'name': 'Global', 'admin': True}]}))
            game = root / 'game.exe'
            game.write_bytes(b'fixture')
            (working / 'configs/devices.json').write_text(json.dumps({'pc_full_path': str(game)}))
            options = working / 'configs/Basic Options.json'
            options.write_text(json.dumps({'Auto Start Game When App Starts': False}))
            game_row = {'pid': 5, 'creation_ticks': 10, 'image': str(game)}
            row = {'installation_root': str(root), 'game_start': 'manual'}
            with patch('gachaops.assistants.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.assistants.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.assistants.find_matching_processes') as observe:
                observe.return_value = {'status': 'observed', 'matches': [game_row]}
                result = prepare_assistant(row)
                self.assertEqual(result['borrowed_game'], [game_row])
                self.assertEqual(result['blockers'], [])
                self.assertEqual(result['argv_candidate'][-3:], ['-t', 'DailyTask', '-e'])
                options.write_text(json.dumps({'Auto Start Game When App Starts': True}))
                self.assertIn('disable_auto_start_game_in_assistant_gui', prepare_assistant(row)['blockers'])
                observe.return_value = {'status': 'observed', 'matches': []}
                self.assertIn('manual_game_requires_exactly_one_running_instance', prepare_assistant(row)['blockers'])
                observe.return_value = {'status': 'observed', 'matches': [game_row, dict(game_row, pid=6)]}
                self.assertIn('manual_game_requires_exactly_one_running_instance', prepare_assistant(row)['blockers'])
                observe.return_value = {'status': 'observed', 'matches': [game_row, {'image': str(app / 'python/pythonw.exe')}]}
                self.assertIn('existing_runner_not_exclusive', prepare_assistant(row)['blockers'])

    def test_borrowed_game_never_recorded_owned_and_replaced_pid_rejected(self):
        for mode in ['success', 'left_running', 'pid_reused', 'new_game', 'timeout', 'interrupt']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'logs').mkdir()
                log = root / 'logs/ok-script.log'
                log.write_text('old log\n')
                game = {'pid': 5, 'creation_ticks': 10, 'image': str(root / 'game.exe'), 'in_owned_job': False}
                owned = {'pid': 123, 'creation_ticks': 20, 'image': str(root / 'pythonw.exe'), 'in_owned_job': True}
                current = {'installation_root': str(root), 'cwd': str(root), 'game_path': game['image'],
                           'argv_candidate': [owned['image'], str(root / 'main.py')], 'borrowed_game': [game]}
                checked = dict(game)
                if mode == 'pid_reused':
                    checked['creation_ticks'] = 11
                if mode == 'new_game':
                    checked['pid'] = 6
                closed = []
                class Process:
                    pid = 123
                    def __init__(self, *args, **kwargs):
                        pass
                    def __enter__(self):
                        with log.open('a') as stream:
                            stream.write('2026-10-01 00:00:00,000 INFO MainThread ok:ok-script init v3.6.7, [], pid=123 config: {}\n' + START + '\n')
                        return self
                    def wait(self, timeout):
                        if mode == 'interrupt':
                            raise KeyboardInterrupt
                        with log.open('a') as stream:
                            stream.write(DONE + '\n' + EXIT + '\n')
                        return 0
                    def __exit__(self, *_):
                        closed.append(True)
                post_rows = [game] if mode in {'left_running', 'timeout'} else []
                clock = iter([0, 4000, 4000])
                observed = [{'status': 'observed', 'matches': [checked]}, {'status': 'observed', 'matches': post_rows}]
                with patch('gachaops.ok_trial.WindowsProcess', Process), \
                     patch('gachaops.ok_trial.owned_processes', return_value=[owned]), \
                     patch('gachaops.ok_trial.find_matching_processes', side_effect=observed), \
                     patch('gachaops.ok_trial.verify_observed_membership', return_value=[checked]), \
                     patch('gachaops.ok_trial.runner_environment', return_value={}), \
                     patch('gachaops.ok_trial.time.monotonic', side_effect=(lambda: next(clock)) if mode == 'timeout' else __import__('time').monotonic):
                    if mode == 'interrupt':
                        with self.assertRaises(KeyboardInterrupt), ExecutionSession(root / 'state', 'fixture') as session:
                            run_prepared(current, session, root, capture_cursor(log), 30, exit_grace=0)
                        self.assertEqual(read_state(root / 'state')['phase'], 'quarantined')
                        interrupted = json.loads((root / 'summary.json').read_text())
                        self.assertTrue(interrupted['owned_job_cleanup_verified'])
                        self.assertEqual(interrupted['status'], 'interrupted')
                    else:
                        with ExecutionSession(root / 'state', 'fixture') as session:
                            report, _ = run_prepared(current, session, root, capture_cursor(log), 30, exit_grace=0)
                        self.assertTrue(all(r['pid'] != game['pid'] for r in report['ownership_observations']))
                        if mode in {'pid_reused', 'new_game'}:
                            self.assertEqual(report['status'], 'ownership_or_cleanup_unconfirmed')
                            self.assertEqual(read_state(root / 'state')['phase'], 'quarantined')
                        else:
                            self.assertEqual(report['status'], 'timeout' if mode == 'timeout' else 'success')
                            self.assertTrue(report['game_observed_external'])
                            self.assertEqual(report['external_game_left_running'], bool(post_rows))
                self.assertEqual(closed, [True])

    def test_remaining_manual_game_stops_chain_without_forcing_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'logs').mkdir()
            (root / 'logs/ok-script.log').write_text('')
            data = {'assistants': [{'id': 'one'}, {'id': 'two'}]}
            current = {'cwd': str(root), 'blockers': [], 'runner_version': 'fixture', 'task_configuration': 'saved'}
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.prepare_assistant', return_value=current), \
                 patch('gachaops.assistants.run_prepared', return_value=({'status': 'success', 'cleanup_verified': True,
                                                                       'external_game_left_running': True}, root)) as monitor:
                report, _ = run_assistants(data, root / 'runs', root / 'state', progress=lambda _: None)
            self.assertEqual(monitor.call_count, 1)
            self.assertEqual(report['status'], 'stopped_external_game_running')
            self.assertEqual(read_state(root / 'state')['phase'], 'idle')
