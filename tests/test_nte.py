import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.assistants import prepare_assistant, run_assistants
from gachaops.ok_completion import capture_cursor
from gachaops.nte_completion import evaluate, observe


def log(source, message, thread='TaskExecutor', level='INFO'):
    return f'2026-10-01 12:00:00,000 {level} {thread} {source}:{message}'


def cycle(failed=None):
    return [log('ok', 'ok-script init fixture, pid=123 config:', 'MainThread'),
            log('start_controller', 'enabled task <src.tasks.daily.DailyRoutineTask.DailyRoutineTask object at 0xABC>', 'MainThread'),
            log('DailyRoutineTask', '开始执行日常任务'),
            log('DailyRoutineTask', "info_set success ['日常领取']"),
            log('DailyRoutineTask', 'info_set failed ' + repr(failed or [])),
            log('DailyRoutineTask', "info_set skipped ['影院约会']"),
            log('DailyRoutineTask', '结束执行日常任务'),
            log('TaskExecutor', 'Successfully Executed Task, Exiting Game and App!')]


class NteTests(unittest.TestCase):
    def test_native_partial_result_survives_successful_exit(self):
        result = evaluate(cycle(['一咖舍']), 0, writer_verified=True)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertEqual(result['failed_applications'], ['一咖舍'])
        self.assertEqual(evaluate(cycle(), 0, writer_verified=True)['status'], 'success')

    def test_missing_reordered_duplicate_or_foreign_result_is_not_success(self):
        rows = cycle()
        for lines, code, writer in [(rows, None, True), (rows, 0, False),
                                     (rows[:4] + rows[5:], 0, True),
                                     (rows[:-2], 0, True),
                                     (rows + rows[1:], 0, True),
                                     (rows[:6] + [rows[3]] + rows[6:], 0, True),
                                     (rows[:3] + rows[6:7] + rows[3:6] + rows[7:], 0, True)]:
            with self.subTest(lines=lines, code=code, writer=writer):
                self.assertEqual(evaluate(lines, code, writer_verified=writer)['status'], 'completion_unconfirmed')
        self.assertEqual(evaluate(rows, 1, writer_verified=True)['status'], 'failed')

    def test_empty_selection_is_reported_explicitly(self):
        rows = cycle()[:2] + [log('DailyRoutineTask', '日常任务没有已选任务，跳过执行'), cycle()[-1]]
        result = evaluate(rows, 0, writer_verified=True)
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['empty_preset'])

    def test_old_log_wrong_writer_and_rewrite_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ok-script.log'
            path.write_text('\n'.join(cycle()) + '\n', encoding='utf-8')
            cursor = capture_cursor(path)
            self.assertEqual(observe(cursor, 0, 123, [])['status'], 'completion_unconfirmed')
            with path.open('a', encoding='utf-8') as stream:
                stream.write('\n'.join(cycle()) + '\n')
            self.assertEqual(observe(cursor, 0, 123, [])['status'], 'success')
            self.assertEqual(observe(cursor, 0, 456, [])['status'], 'completion_unconfirmed')
            path.write_text('changed\n', encoding='utf-8')
            self.assertEqual(observe(cursor, 0, 123, [])['status'], 'completion_unconfirmed')

    def test_dynamic_cli_index_opaque_presets_and_observer_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app = root / 'data/apps/ok-nte'
            working = app / 'working'
            (working / 'src').mkdir(parents=True)
            (working / 'configs').mkdir()
            (working / 'logs').mkdir()
            (working / 'logs/ok-script.log').write_text('')
            (app / 'python').mkdir()
            (app / 'python/pythonw.exe').write_bytes(b'fixture')
            (working / 'main.py').write_text('')
            (app / 'app.json').write_text(json.dumps({'current_version': 'fixture', 'current_profile': 'Global',
                                                     'profiles': [{'name': 'Global', 'admin': True}]}))
            game = root / 'game.exe'
            game.write_bytes(b'fixture')
            official_launcher = root / 'official-launcher.exe'
            official_launcher.write_bytes(b'fixture')
            launcher_config = working / 'configs/LauncherTask.json'
            launcher_config.write_text(json.dumps({'Launcher Path': str(official_launcher)}))
            (working / 'configs/devices.json').write_text(json.dumps({'pc_full_path': str(game)}))
            row = {'id': 'nte', 'adapter': 'ok-nte', 'installation_root': str(root), 'enabled': True}
            with patch('gachaops.assistants.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.assistants.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.assistants.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                for tasks in [[['other', 'Other'], ['src.tasks.daily.DailyRoutineTask', 'DailyRoutineTask']],
                              [['src.tasks.daily.DailyRoutineTask', 'DailyRoutineTask'], ['other', 'Other']]]:
                    (working / 'src/config.py').write_text('config=' + repr({'onetime_tasks': tasks}))
                    preset = working / 'configs/DailyRoutineTask.json'
                    preset.write_bytes(b'not JSON; this must stay opaque')
                    current = prepare_assistant(row)
                    self.assertEqual(current['argv_candidate'][-3:], ['-t', str(tasks.index(['src.tasks.daily.DailyRoutineTask', 'DailyRoutineTask']) + 1), '-e'])
                    self.assertEqual(current['blockers'], [])
                    self.assertEqual(current['additional_roots'], [[str(official_launcher)]])
                    self.assertIn(str(official_launcher), current['observation_targets'])
                    self.assertEqual(preset.read_bytes(), b'not JSON; this must stay opaque')
                with patch('gachaops.assistants.find_matching_processes', return_value={
                    'status': 'observed', 'matches': [{'pid': 987, 'image': str(official_launcher)}]}):
                    self.assertIn('existing_runner_or_game_not_exclusive', prepare_assistant(row)['blockers'])
                launcher_config.write_text(json.dumps({'Launcher Path': 'relative.exe'}))
                self.assertIn('official_launcher_path_unverified', prepare_assistant(row)['blockers'])
                launcher_config.write_text(json.dumps({'Launcher Path': str(official_launcher)}))
                with patch('gachaops.assistants.windows_available', return_value=True), \
                     patch('gachaops.assistants.run_prepared', return_value=({'status': 'success', 'cleanup_verified': True}, root)) as launch:
                    run_assistants({'assistants': [row]}, root / 'runs', root / 'state', progress=lambda _: None)
                    self.assertIs(launch.call_args.kwargs['completion_observer'], observe)
                (working / 'src/config.py').write_text('config=' + repr({'onetime_tasks': [tasks[0], tasks[0]]}))
                with self.assertRaisesRegex(ValueError, 'ambiguous'):
                    prepare_assistant(row)
