import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.assistants import load_assistants, run_assistants
from gachaops.planner import ConfigError
from gachaops.recovery import read_state
from gachaops.windows_observation import ProcessObservationError


class RepeatTests(unittest.TestCase):
    def test_manual_preflight_error_cannot_skip_to_next_game(self):
        report, calls, _ = self.exercise({}, limit=0, game_start='manual',
            blockers=['maaend_disable_launch_preactions'])
        self.assertEqual(calls, [])
        self.assertEqual(report['status'], 'stopped_preflight_blocked')
        self.assertEqual(len(report['assistants'][0]['attempts']), 1)

    def test_single_round_exclusivity_also_stops_before_next_game(self):
        report, calls, journal = self.exercise({}, limit=0,
            blockers=['existing_runner_or_game_not_exclusive'])
        self.assertEqual(calls, [])
        self.assertEqual(report['status'], 'stopped_preflight_blocked')
        self.assertEqual(len(report['assistants'][0]['attempts']), 1)
        self.assertEqual(journal['phase'], 'idle')

    def exercise(self, outcomes, limit=2, stop_requested=None, desktops=None, blockers=None, prepare_error=None, game_start='assistant'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'logs').mkdir()
            (root / 'logs/ok-script.log').write_text('')
            data = dict(schema_version=1, repeat_limit=limit, assistants=[
                dict(id=i, adapter='ok-ww', installation_root=str(root), enabled=i != 'skip', game_start=game_start)
                for i in ('one', 'skip', 'two')])
            current = dict(cwd=str(root), blockers=blockers or [], runner_version='fixture', task_configuration='saved')
            calls = []
            def run(current, session, output, cursor, timeout, **kwargs):
                identity = kwargs['metadata']['id']
                calls.append(identity)
                outcome = outcomes[identity].pop(0)
                result = dict(id=identity, run_id=session.record['run_id'], cleanup_verified=True,
                              status=outcome) if isinstance(outcome, str) else dict(outcome, id=identity,
                              run_id=session.record['run_id'])
                (output / 'summary.json').write_text(json.dumps(result))
                return result, output
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.runtime_desktop_report', side_effect=desktops,
                       return_value={'status': 'observed_available'}), \
                 patch('gachaops.assistants.prepare_assistant', return_value=current, side_effect=prepare_error), \
                 patch('gachaops.assistants.run_prepared', side_effect=run):
                report, path = run_assistants(data, root / 'runs', root / 'state',
                    progress=lambda _: None, stop_requested=(lambda: stop_requested(calls)) if stop_requested else None)
            saved = json.loads((path / 'summary.json').read_text())
            self.assertEqual(saved, report)
            attempts = {r['id']: r.get('attempts', []) for r in report['assistants']}
            for history in attempts.values():
                for attempt in history:
                    node = json.loads(Path(attempt['node_summary']).read_text())
                    self.assertEqual(node['status'], attempt['status'])
            return report, calls, read_state(root / 'state')

    def test_repeat_only_unsuccessful_preserving_order_and_history(self):
        report, calls, journal = self.exercise({'one': ['partial_failure', 'success'], 'two': ['success']})
        self.assertEqual(calls, ['one', 'two', 'one'])
        self.assertEqual(report['status'], 'success')
        self.assertEqual(report['current_round'], 2)
        self.assertEqual([r['id'] for r in report['assistants']], ['one', 'skip', 'two'])
        history = report['assistants'][0]['attempts']
        self.assertEqual([r['status'] for r in history], ['partial_failure', 'success'])
        self.assertEqual(len({r['node_summary'] for r in history}), 2)
        self.assertEqual(journal['phase'], 'idle')

    def test_limit_is_additional_attempts_and_timeout_repeats(self):
        report, calls, _ = self.exercise({'one': ['timeout'] * 3, 'two': ['partial_failure'] * 3})
        self.assertEqual(calls, ['one', 'two'] * 3)
        self.assertEqual(report['status'], 'completed_with_failures')
        self.assertTrue(report['repeat_limit_reached'])
        self.assertEqual(len(report['rounds']), 3)

    def test_zero_and_success_never_repeat(self):
        report, calls, _ = self.exercise({'one': ['partial_failure'], 'two': ['success']}, limit=0)
        self.assertEqual(calls, ['one', 'two'])
        report, calls, _ = self.exercise({'one': ['success'], 'two': ['success']})
        self.assertEqual(calls, ['one', 'two'])
        self.assertEqual(report['current_round'], 1)

    def test_unsafe_cleanup_desktop_cancel_external_game_stop_all_rounds(self):
        for status, extra, expected, phase in [
            ('partial_failure', dict(cleanup_verified=False), 'stopped_cleanup_unconfirmed', 'quarantined'),
            ('desktop_environment_interrupted', {}, 'desktop_environment_interrupted', 'idle'),
            ('cancelled', {}, 'cancelled', 'idle'),
            ('partial_failure', dict(external_game_left_running=True), 'stopped_external_game_running', 'idle')]:
            with self.subTest(status=status, extra=extra):
                report, calls, journal = self.exercise({'one': [dict(status=status, cleanup_verified=True, **extra)
                    if 'cleanup_verified' not in extra else dict(status=status, **extra)], 'two': []})
                self.assertEqual(calls, ['one'])
                self.assertEqual(report['status'], expected)
                self.assertEqual(journal['phase'], phase)

    def test_stop_between_rounds_prevents_retry(self):
        report, calls, journal = self.exercise({'one': ['partial_failure'], 'two': ['success']},
                                             stop_requested=lambda calls: len(calls) >= 2)
        self.assertEqual(calls, ['one', 'two'])
        self.assertEqual(report['status'], 'cancelled')
        self.assertEqual(journal['phase'], 'idle')

    def test_desktop_lost_between_rounds_prevents_retry(self):
        report, calls, journal = self.exercise({'one': ['partial_failure'], 'two': ['success']},
            desktops=[{'status': 'observed_available'}, {'status': 'blocked', 'reason': 'input_desktop_not_default'}])
        self.assertEqual(calls, ['one', 'two'])
        self.assertEqual(report['status'], 'desktop_environment_interrupted')
        self.assertEqual(report['desktop_interruption']['reason'], 'input_desktop_not_default')
        self.assertEqual(journal['phase'], 'idle')

    def test_repeat_limit_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for value in (-1, True, 1.5, '2', None):
                path.write_text(json.dumps(dict(schema_version=1, repeat_limit=value, assistants=[
                    dict(id='one', adapter='ok-ww', enabled=False, installation_root=None)])))
                with self.subTest(value=value), self.assertRaises(ConfigError):
                    load_assistants(path)

    def test_exclusivity_blocker_stops_repeat_flow_before_launch(self):
        for blocker in ('existing_runner_or_game_not_exclusive', 'existing_runner_not_exclusive',
                        'manual_game_requires_exactly_one_running_instance'):
            with self.subTest(blocker=blocker):
                report, calls, journal = self.exercise({}, blockers=[blocker])
                self.assertEqual(calls, [])
                self.assertEqual(report['status'], 'stopped_preflight_blocked')
                self.assertEqual(len(report['rounds']), 1)
                self.assertEqual(len(report['assistants'][0]['attempts']), 1)
                self.assertEqual(journal['phase'], 'idle')

    def test_process_observation_failure_stops_but_missing_config_repeats(self):
        report, calls, _ = self.exercise({}, prepare_error=ProcessObservationError('Access denied'))
        self.assertEqual(calls, [])
        self.assertEqual(report['status'], 'stopped_preflight_blocked')
        self.assertEqual(len(report['rounds']), 1)
        report, calls, _ = self.exercise({}, prepare_error=FileNotFoundError('Missing config'))
        self.assertEqual(calls, [])
        self.assertEqual(report['status'], 'completed_with_failures')
        self.assertEqual(len(report['rounds']), 3)
