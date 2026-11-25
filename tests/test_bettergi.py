import json
from pathlib import Path
import tempfile
import subprocess
import time
import unittest
from unittest.mock import patch

from gachaops import bettergi
from gachaops.assistants import prepare_assistant, run_assistants
from gachaops.configuration import compose_configuration
from gachaops.desktop_model import bettergi_inspection
from gachaops.registry import RegistrationStore
from gachaops.result_details import details_for
from gachaops.ok_trial import run_prepared
from gachaops.recovery import ExecutionSession
from gachaops.windows_process import CleanupError


def event(message, source=bettergi.FLOW, level='INF', pid=42, token=None):
    return {'pid': pid, 'token': token, 'source': source, 'level': level, 'lines': [message]}


def log(events):
    return ''.join(f"[12:00:00.000] [{e['level']}] [Primary:S1:P{e['pid']}:T{e['token']}] {e['source']}\n"
                   + '\n'.join(e['lines']) + '\n\n' for e in events)


class BetterGITests(unittest.TestCase):
    def install(self, root):
        # Native process observations use canonical paths. CI temp roots may be
        # Windows 8.3 aliases or macOS /var symlinks, unlike the local temp root.
        root = root.resolve(strict=True)
        (root / 'User/OneDragon').mkdir(parents=True)
        (root / 'log').mkdir()
        (root / 'BetterGI.exe').write_bytes(b'fixture')
        game = root / 'GenshinImpact.exe'; game.touch()
        config = {'selectedOneDragonFlowConfigName': '默认配置', 'genshinStartConfig': {
            'installPath': str(game), 'autoEnterGameEnabled': True, 'startGameWithCmd': False,
            'genshinStartArgs': ''}, 'scriptConfig': {'autoUpdateBeforeCommandLineRun': False}}
        self.config_path = root / 'User/config.json'
        self.config_path.write_text(json.dumps(config), encoding='utf-8')
        self.preset = {'Name': '默认配置', 'CompletionAction': '关闭游戏和软件', 'NextTaskId': '',
            'TaskEnabledList': {'a': True, 'b': True}, 'TaskOrder': ['b', 'a'],
            'TaskDefinitions': {'a': '领取邮件', 'b': '合成树脂'}}
        self.preset_path = root / 'User/OneDragon/默认配置.json'
        self.save_preset()
        return {'id': 'genshin', 'adapter': 'bettergi', 'installation_root': str(root),
                'entry_executable': str(root / 'BetterGI.exe'), 'enabled': False}

    def save_preset(self):
        self.preset_path.write_text(json.dumps(self.preset), encoding='utf-8')

    def prepare(self, row):
        with patch.object(bettergi, 'file_version', return_value=bettergi.SUPPORTED_VERSION), \
             patch.object(bettergi, 'elevation_report', return_value={'elevated': True}), \
             patch.object(bettergi, 'desktop_report', return_value={'status': 'observed_available'}), \
             patch.object(bettergi, 'find_matching_processes', return_value={'status': 'observed', 'matches': []}):
            return prepare_assistant(row)

    def fixture_events(self, token):
        return [event(m, token=token) for m in ['启用一条龙配置：默认配置', '启用任务总数量: 2',
            '一条龙任务执行: 1/2', '一条龙任务执行: 2/2', '一条龙和配置组任务结束']] + [
                event('主窗体退出', source='BetterGenshinImpact.View.MainWindow', level='DBG', token=token)]

    def evaluate(self, events, code=0):
        now = time.time_ns() // 1000000
        events = [dict(e, token=now if e['token'] is None else e['token']) for e in events]
        return bettergi.evaluate(events, code, 42, now * 10000 + 116444736000000000,
                                 '默认配置', ('合成树脂', '领取邮件'), now * 1000000)

    def test_saved_native_preset_and_order_are_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self.install(Path(tmp))
            before = self.preset_path.read_bytes(), self.config_path.read_bytes()
            prepared = self.prepare(row)
            self.assertEqual(prepared['argv_candidate'], [row['entry_executable'], '--startOneDragon', '默认配置'])
            self.assertEqual(prepared['task_names'], ['合成树脂', '领取邮件'])
            self.assertEqual(prepared['blockers'], [])
            self.assertEqual(before, (self.preset_path.read_bytes(), self.config_path.read_bytes()))
            self.assertFalse(row['enabled'])

    def test_version_resume_custom_tasks_unsafe_completion_and_duplicate_names_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self.install(Path(tmp))
            with patch.object(bettergi, 'file_version', return_value='0.67.0.0'):
                with self.assertRaisesRegex(ValueError, 'not verified'): prepare_assistant(row)
            for key, value in [('CompletionAction', '关机'), ('NextTaskId', 'a'),
                               ('TaskDefinitions', {'a': '自定义脚本组', 'b': '合成树脂'}),
                               ('TaskOrder', ['a', 'a'])]:
                original = self.preset[key]; self.preset[key] = value; self.save_preset()
                with self.assertRaises(ValueError): self.prepare(row)
                self.preset[key] = original; self.save_preset()
            (self.preset_path.parent / '同名.json').write_bytes(self.preset_path.read_bytes())
            with self.assertRaisesRegex(ValueError, 'duplicate'): self.prepare(row)

    def test_registration_writes_global_library_only_and_compose_merges_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            row = self.install(root)
            store = RegistrationStore(root); store.create()
            with patch.object(bettergi, 'prepare', return_value=self.prepare(row)):
                inspection = bettergi_inspection(row['entry_executable'])
            self.assertEqual(inspection.blockers, ())
            updated = store.commit('bettergi', inspection, store.read()['revision'])
            self.assertEqual(updated['assistants']['bettergi'],
                             {'installation_root': str(root), 'entry_executable': str(root / 'BetterGI.exe')})
            # The library never touches profiles: run rows stay path-free and
            # composition merges registration fields on demand.
            profile = {'repeat_limit': 0, 'assistants': [
                {'id': 'other', 'adapter': 'bettergi', 'enabled': False,
                 'game_start': 'assistant', 'timeout_seconds': 1800},
                {'id': 'genshin', 'adapter': 'bettergi', 'enabled': True,
                 'game_start': 'assistant', 'timeout_seconds': 1800}]}
            snapshot = compose_configuration(profile, updated)
            self.assertEqual([r['id'] for r in snapshot['assistants']], ['other', 'genshin'])
            self.assertEqual(snapshot['assistants'][1]['installation_root'], str(root))
            self.assertEqual(snapshot['assistants'][1]['entry_executable'], str(root / 'BetterGI.exe'))
            self.assertEqual(profile['assistants'][1], {'id': 'genshin', 'adapter': 'bettergi', 'enabled': True,
                                                        'game_start': 'assistant', 'timeout_seconds': 1800})

    def test_end_and_exit_zero_alone_are_insufficient(self):
        events = self.fixture_events(None)
        self.assertEqual(self.evaluate(events)['status'], 'success')
        for bad in (events[1:], events[:-1], events[:3] + events[-1:], events + events,
                    [event('一条龙和配置组任务结束', source='Notify')]):
            self.assertEqual(self.evaluate(bad)['status'], 'completion_unconfirmed')
        self.assertEqual(self.evaluate(events, 3)['status'], 'failed')
        self.assertEqual(self.evaluate(events, None)['status'], 'completion_unconfirmed')

    def test_errors_map_to_dispatched_task_and_user_details(self):
        events = self.fixture_events(None)
        events.insert(3, event('未能找到合成台', source='BetterGenshinImpact.GameTask.TaskRunner', level='ERR'))
        result = self.evaluate(events)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertEqual(result['failed_applications'], ['合成树脂'])
        details = details_for({'id': 'genshin', 'status': result['status'], 'completion': result})['text']
        self.assertIn('合成树脂', details); self.assertIn('未能找到合成台', details)

    def test_warnings_and_info_failure_remain_uncertain_cancellation_not_success(self):
        for e in (event('识别未完成'), event('识别超时', level='WRN'), event('任务中断:任务被取消')):
            events = self.fixture_events(None); events.insert(3, e)
            self.assertEqual(self.evaluate(events)['status'], 'completion_unconfirmed')

    def test_foreign_notification_and_pid_reuse_cannot_prove_completion(self):
        events = self.fixture_events(None)
        events[-2] = event('一条龙和配置组任务结束', source='Notify')
        self.assertEqual(self.evaluate(events)['status'], 'completion_unconfirmed')
        with self.assertRaises(ValueError): self.evaluate(self.fixture_events(1))
        bad = self.fixture_events(None); bad[-1]['token'] = 1
        with self.assertRaises(ValueError): self.evaluate(bad)
        foreign = self.fixture_events(None)
        for e in foreign: e['pid'] = 99
        with self.assertRaises(ValueError): self.evaluate(foreign)

    def test_final_reward_check_failure_not_assigned_to_last_task(self):
        runner = 'BetterGenshinImpact.GameTask.TaskRunner'
        events = self.fixture_events(None)
        events[3:3] = [event('→ "任务结束"', source=runner)]
        events[-2:-2] = [event('→ "任务结束"', source=runner),
            event('→ "任务启动！"', source=runner), event('每日奖励检查异常', source=runner, level='ERR')]
        self.assertEqual(self.evaluate(events)['failed_applications'], ['结束前奖励检查'])

    def test_multiline_record_does_not_absorb_foreign_instance(self):
        native = log([event('异常', level='ERR', token=1)])
        native += '[12:00:00.000] [INF] [Script:S1:P99:T1] other\n一条龙和配置组任务结束\n\n'
        parsed = list(bettergi.records(native))
        self.assertEqual(len(parsed), 1)
        self.assertNotIn('一条龙和配置组任务结束', parsed[0]['lines'])

    def test_cursor_only_reads_new_owned_run_and_archives_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); row = self.install(root); node = root / 'node'; node.mkdir()
            path = root / 'log/better-genshin-impact20261005.log'
            path.write_text(log(self.fixture_events(1)), encoding='utf-8')
            _, cursor = bettergi.bind_execution(self.prepare(row), node)
            now = time.time_ns() // 1000000
            owned = [{'pid': 42, 'image': row['entry_executable'], 'creation_ticks': now * 10000 + 116444736000000000,
                      'in_owned_job': True}]
            with path.open('a', encoding='utf-8') as stream: stream.write(log(self.fixture_events(now)))
            self.assertEqual(bettergi.observe(cursor, 0, 42, owned)['status'], 'success')
            archived = (node / 'bettergi-output.log').read_text(encoding='utf-8')
            self.assertNotIn(':T1]', archived)
            self.assertIn(f':T{now}]', archived)
            owned[0]['in_owned_job'] = False
            self.assertEqual(bettergi.observe(cursor, 0, 42, owned)['status'], 'completion_unconfirmed')
            owned[0]['in_owned_job'] = True
            path.write_text(log(self.fixture_events(now)), encoding='utf-8')
            self.assertEqual(bettergi.observe(cursor, 0, 42, owned)['status'], 'completion_unconfirmed')

    def test_path_alias_registration_uses_canonical_owned_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            row = self.install(root)
            alias = root / '..' / root.name
            row.update(installation_root=str(alias), entry_executable=str(alias / 'BetterGI.exe'))
            prepared = self.prepare(row)
            self.assertEqual(prepared['argv_candidate'],
                             [str(root / 'BetterGI.exe'), '--startOneDragon', '默认配置'])
            node = root / 'node'; node.mkdir()
            _, cursor = bettergi.bind_execution(prepared, node)
            now = time.time_ns() // 1000000
            owned = [dict(pid=42, image=prepared['launcher'],
                          creation_ticks=now * 10000 + 116444736000000000, in_owned_job=True)]
            (root / 'log/better-genshin-impact20261005.log').write_text(
                log(self.fixture_events(now)), encoding='utf-8')
            self.assertEqual(bettergi.observe(cursor, 0, 42, owned)['status'], 'success')
            owned[0]['image'] = str(root / 'other.exe')
            self.assertEqual(bettergi.observe(cursor, 0, 42, owned)['status'], 'completion_unconfirmed')

    def test_preset_drift_blocks_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self.install(Path(tmp)); prepared = self.prepare(row)
            self.preset['NextTaskId'] = 'a'; self.save_preset()
            with self.assertRaisesRegex(ValueError, 'changed'):
                bettergi.bind_execution(prepared, tmp)

    def test_midnight_new_log_continues_same_owned_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); row = self.install(root); node = root / 'node'; node.mkdir()
            first = root / 'log/better-genshin-impact20261005.log'; first.touch()
            _, cursor = bettergi.bind_execution(self.prepare(row), node)
            now = time.time_ns() // 1000000
            owned = [{'pid': 42, 'image': row['entry_executable'], 'creation_ticks': now * 10000 + 116444736000000000,
                      'in_owned_job': True}]
            events = self.fixture_events(now)
            first.write_text(log(events[:3]), encoding='utf-8')
            (root / 'log/better-genshin-impact20261006.log').write_text(log(events[3:]), encoding='utf-8')
            self.assertEqual(bettergi.observe(cursor, 0, 42, owned)['status'], 'success')

    def test_chain_passes_bettergi_observer_and_preserves_real_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); row = self.install(root); row['enabled'] = True
            data = {'assistants': [row, dict(row, id='next', enabled=False)]}
            prepared = self.prepare(row)
            result = {'id': 'genshin', 'status': 'partial_failure', 'cleanup_verified': True,
                      'game_execution': True, 'completion': {'failed_applications': ['合成树脂']}}
            with patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.prepare_assistant', return_value=prepared), \
                 patch('gachaops.assistants.run_prepared', return_value=(result, root)) as launch:
                report, _ = run_assistants(data, root / 'runs', root / 'state', progress=lambda _: None)
            self.assertEqual(launch.call_count, 1)
            self.assertIs(launch.call_args.kwargs['completion_observer'], bettergi.observe)
            self.assertEqual(report['assistants'][0]['completion']['failed_applications'], ['合成树脂'])
            self.assertEqual(report['assistants'][1]['status'], 'disabled')

    def test_bettergi_reuses_timeout_desktop_loss_and_cleanup_quarantine(self):
        for mode, expected in [('timeout', 'timeout'), ('desktop', 'desktop_environment_interrupted'),
                               ('cleanup', 'ownership_or_cleanup_unconfirmed')]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); row = self.install(root); node = root / 'node'; node.mkdir()
                current, cursor = bettergi.bind_execution(self.prepare(row), node)
                now = time.time_ns() // 1000000
                owned = [dict(pid=42, image=row['entry_executable'], creation_ticks=now * 10000 + 116444736000000000,
                              in_owned_job=True),
                         dict(pid=43, image=current['game_path'], creation_ticks=now * 10000 + 116444736000000001,
                              in_owned_job=True)]
                cleaned = []
                class Process:
                    pid = 42
                    def __init__(self, *args, **kwargs): pass
                    def __enter__(self): return self
                    def __exit__(self, *_):
                        cleaned.append(True)
                        if mode == 'cleanup': raise CleanupError('fixture cleanup failed')
                    def wait(self, *_):
                        if mode == 'timeout':
                            time.sleep(.003)
                            raise subprocess.TimeoutExpired('fixture', .003)
                        return 0
                checks = iter([{'status': 'observed_available'}, {'status': 'blocked', 'reason': 'locked'}])
                desktop = (lambda: next(checks)) if mode == 'desktop' else (lambda: {'status': 'observed_available'})
                with patch('gachaops.ok_trial.WindowsProcess', Process), \
                     patch('gachaops.ok_trial.runner_environment', return_value={}), \
                     patch('gachaops.ok_trial.allow_foreground'), patch('gachaops.ok_trial.request_foreground', return_value={}), \
                     patch('gachaops.ok_trial.owned_processes', return_value=owned), \
                     patch('gachaops.ok_trial.find_matching_processes', return_value={'status': 'observed', 'matches': owned}), \
                     patch('gachaops.ok_trial.verify_observed_membership', return_value=owned), \
                     patch('gachaops.ok_trial.post_cleanup_observation', return_value={'status': 'observed', 'matches': []}):
                    with ExecutionSession(root / 'state', 'bettergi_fixture') as session:
                        result, _ = run_prepared(current, session, node, cursor, .002,
                                                 completion_observer=bettergi.observe, desktop_check=desktop)
                self.assertEqual(result['status'], expected)
                self.assertEqual(cleaned, [True])
                self.assertEqual(result['cleanup_verified'], mode != 'cleanup')
                self.assertEqual(result['retries'], 0)


if __name__ == '__main__':
    unittest.main()
