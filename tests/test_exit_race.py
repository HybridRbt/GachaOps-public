import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gachaops.ok_trial import run_prepared, post_cleanup_observation
from gachaops.ok_completion import capture_cursor
from gachaops.recovery import ExecutionSession, read_state

START = '2026-10-01 00:00:00,000 INFO MainThread start_controller:enabled task <src.task.DailyTask.DailyTask object at 0x123>'
DONE = '2026-10-01 00:00:01,000 INFO TaskExecutor DailyTask:Daily Task Completed'
EXIT = '2026-10-01 00:00:01,000 INFO TaskExecutor TaskExecutor:Successfully Executed Task, Exiting Game and App!'


class ExitRaceTests(unittest.TestCase):
    def test_checkpoint_only_on_identity_change_and_persistent_failure_still_cleans_job(self):
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log = root / 'log'
                log.write_text('')
                owned = {'pid': 123, 'creation_ticks': 20, 'image': str(root / 'game.exe'), 'in_owned_job': True}
                current = {'installation_root': str(root), 'cwd': str(root), 'game_path': owned['image'],
                           'argv_candidate': [str(root / 'python.exe')]}
                waits, closed = [], []
                class Process:
                    pid = 123
                    def __init__(self, *args, **kwargs):
                        pass
                    def __enter__(self):
                        return self
                    def wait(self, timeout):
                        waits.append(timeout)
                        if len(waits) < 3:
                            raise subprocess.TimeoutExpired('fixture', timeout)
                        return 0
                    def __exit__(self, *_):
                        closed.append(True)
                with patch('gachaops.ok_trial.WindowsProcess', Process), \
                     patch('gachaops.ok_trial.owned_processes', return_value=[owned]), \
                     patch('gachaops.ok_trial.find_matching_processes', return_value={'status': 'observed', 'matches': []}), \
                     patch('gachaops.ok_trial.verify_observed_membership', return_value=[]), \
                     patch('gachaops.ok_trial.runner_environment', return_value={}):
                    with ExecutionSession(root / 'state', 'fixture') as session:
                        with patch.object(session, 'observe_owned', wraps=session.observe_owned,
                                          side_effect=PermissionError('checkpoint denied') if failed else None) as checkpoint:
                            report, _ = run_prepared(current, session, root, capture_cursor(log), 30)
                            self.assertEqual(checkpoint.call_count, 1)
                self.assertEqual(closed, [True])
                self.assertTrue(report['owned_job_cleanup_verified'])
                if failed:
                    self.assertEqual(report['reason'], 'execution_journal_write_failed')
                    self.assertEqual(read_state(root / 'state')['phase'], 'quarantined')
                    self.assertEqual(report['ownership_observations'], [owned])
                else:
                    self.assertEqual(len(waits), 3)
                    self.assertEqual(read_state(root / 'state')['phase'], 'idle')

    def test_owned_game_teardown_wait_requires_exact_observed_identity(self):
        owned = {'pid': 5, 'creation_ticks': 10, 'image': 'fixture-game', 'in_owned_job': True}
        pending = {'status': 'observed', 'matches': [owned], 'uncertain': []}
        empty = {'status': 'observed', 'matches': [], 'uncertain': []}
        with patch('gachaops.ok_trial.find_matching_processes', side_effect=[pending, empty]), \
             patch('gachaops.ok_trial.time.sleep'):
            self.assertEqual(post_cleanup_observation([], [], {5}, owned=[owned]), empty)
        reused = {'status': 'observed', 'matches': [dict(owned, creation_ticks=11)], 'uncertain': []}
        with patch('gachaops.ok_trial.find_matching_processes', return_value=reused) as observe, \
             patch('gachaops.ok_trial.time.sleep') as sleep:
            self.assertEqual(post_cleanup_observation([], [], {5}, owned=[owned]), reused)
            self.assertEqual(observe.call_count, 1)
            sleep.assert_not_called()

    def test_exiting_root_access_denied_uses_retained_exit_code(self):
        for stage in ['snapshot', 'membership', 'owned', 'foreign', 'reused']:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log = root / 'log'
                log.write_text('')
                game = {'pid': 5, 'creation_ticks': 10, 'image': str(root / 'game.exe'), 'in_owned_job': False}
                owned = {'pid': 123, 'creation_ticks': 20, 'image': str(root / 'pythonw.exe'), 'in_owned_job': True}
                current = {'installation_root': str(root), 'cwd': str(root), 'game_path': game['image'],
                           'argv_candidate': [owned['image'], str(root / 'main.py')], 'borrowed_game': [game]}
                waits, closed = [], []
                class Process:
                    pid = 123
                    def __init__(self, *args, **kwargs):
                        pass
                    def __enter__(self):
                        with log.open('a') as stream:
                            stream.write('2026-10-01 00:00:00,000 INFO MainThread ok:ok-script init v3.6.7, [], pid=123 config: {}\n' + START + '\n')
                        return self
                    def wait(self, timeout):
                        waits.append(timeout)
                        if len(waits) == 1:
                            raise subprocess.TimeoutExpired('fixture', timeout)
                        with log.open('a') as stream:
                            stream.write(DONE + '\n' + EXIT + '\n')
                        return 0
                    def __exit__(self, *_):
                        closed.append(True)
                first = {'status': 'observed', 'matches': [game], 'uncertain': []}
                denied = {'status': 'uncertain', 'matches': [], 'uncertain': [{'pid': 999 if stage == 'foreign' else 123, 'error': 5}]}
                closing = {'status': 'observed', 'matches': [game], 'uncertain': []}
                empty = {'status': 'observed', 'matches': [], 'uncertain': []}
                reused = {'status': 'observed', 'matches': [dict(game, creation_ticks=11)], 'uncertain': []}
                snapshots = [first, reused, closing, empty] if stage == 'reused' else [first, denied, closing, empty] if stage in {'snapshot', 'foreign'} else (
                    [first, first, closing, empty] if stage == 'membership' else [first, closing, empty])
                memberships = [[game], OSError('query during teardown')] if stage in {'membership', 'reused'} else None
                owned_rows = [[owned], OSError('job root query during teardown')] if stage == 'owned' else None
                with patch('gachaops.ok_trial.WindowsProcess', Process), \
                     patch('gachaops.ok_trial.owned_processes', side_effect=owned_rows, return_value=[owned]), \
                     patch('gachaops.ok_trial.find_matching_processes', side_effect=snapshots), \
                     patch('gachaops.ok_trial.verify_observed_membership', side_effect=memberships, return_value=[game]), \
                     patch('gachaops.ok_trial.runner_environment', return_value={}), \
                     patch('gachaops.ok_trial.time.sleep'):
                    with ExecutionSession(root / 'state', 'fixture') as session:
                        report, _ = run_prepared(current, session, root, capture_cursor(log), 30)
                self.assertEqual(closed, [True])
                if stage in {'foreign', 'reused'}:
                    self.assertEqual(report['status'], 'ownership_or_cleanup_unconfirmed')
                    self.assertEqual(len(waits), 1)
                    self.assertEqual(read_state(root / 'state')['phase'], 'quarantined')
                else:
                    self.assertEqual(report['status'], 'success')
                    self.assertEqual(report['exit_code'], 0)
                    self.assertTrue(report['cleanup_verified'])
                    self.assertFalse(report['external_game_left_running'])
                    self.assertEqual(read_state(root / 'state')['phase'], 'idle')

    def test_settling_never_hides_foreign_pid_or_reused_game(self):
        game = {'pid': 5, 'creation_ticks': 10, 'image': 'fixture-game'}
        for observation in [
            {'status': 'uncertain', 'matches': [], 'uncertain': [{'pid': 999}]},
            {'status': 'observed', 'matches': [dict(game, creation_ticks=11)], 'uncertain': []},
            {'status': 'observed', 'matches': [dict(game, pid=6)], 'uncertain': []},
        ]:
            with patch('gachaops.ok_trial.find_matching_processes', return_value=observation) as observe, \
                 patch('gachaops.ok_trial.time.sleep') as sleep:
                self.assertEqual(post_cleanup_observation([], [game], {5, 123}), observation)
                self.assertEqual(observe.call_count, 1)
                sleep.assert_not_called()

    def test_settling_is_bounded_and_does_not_kill_remaining_game(self):
        game = {'pid': 5, 'creation_ticks': 10, 'image': 'fixture-game'}
        observation = {'status': 'observed', 'matches': [game], 'uncertain': []}
        with patch('gachaops.ok_trial.find_matching_processes', return_value=observation), \
             patch('gachaops.ok_trial.time.monotonic', side_effect=[0, 0, 6]), \
             patch('gachaops.ok_trial.time.sleep') as sleep:
            self.assertEqual(post_cleanup_observation([], [game], {5}, grace=5), observation)
            self.assertEqual(sleep.call_count, 1)
