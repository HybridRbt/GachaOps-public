import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch

from gachaops import maagf2
from gachaops.assistants import load_assistants, prepare_assistant


def frame(pid, message):
    return f'[2026-10-02 14:00:00.000][INF][Px{pid}][Tx1][Logger] {message}'


def event(kind, task=1, entry='StartGame'):
    return frame(42, '[msg=Tasker.Task.' + kind + '] [details=' +
                 json.dumps({'task_id': task, 'entry': entry}) + ']')


def gui(message, scoped=True):
    scope = '[inst=配置 1/default][src=Monitor][op=MonitorLog] ' if scoped else ''
    return '[2026-10-02 14:00:00.000][INF] [cfg=Default]' + scope + message


class MaaGF2Tests(unittest.TestCase):
    def boot_logs(self):
        return [gui('窗口句柄：77', False),
                gui('启动后连接在第 1 次尝试时成功，继续执行前等待 5 秒。', False),
                gui('任务已全部完成！')]

    def test_bootstrap_needs_owned_root_matching_window_and_finished_startup(self):
        frames = [frame(42, '[Logger] MAA Process Start')]
        logs = self.boot_logs()
        self.assertTrue(maagf2.bootstrap_ready(frames, logs, 42, 'default', 77))
        for bad_frames, bad_logs, instance, hwnd in (
                ([], logs, 'default', 77), (frames, logs[:-1], 'default', 77),
                (frames, logs, 'other', 77), (frames, logs, 'default', 0),
                (frames, logs, 'default', 88), (frames, [logs[0], logs[2]], 'default', 77)):
            self.assertFalse(maagf2.bootstrap_ready(bad_frames, bad_logs, 42, instance, hwnd))
        for message in ('任务失败：连接', '已放弃本次任务', '准备执行任务队列：任务数量=13'):
            with self.assertRaises(ValueError):
                maagf2.bootstrap_ready(frames, logs[:2] + [gui(message)], 42, 'default', 77)
            with self.assertRaises(ValueError):
                maagf2.bootstrap_ready(frames, logs + [gui(message)], 42, 'default', 77)
        self.assertFalse(maagf2.bootstrap_ready(frames, logs + [gui('窗口句柄：0', False)], 42, 'default', 77))

    def test_bootstrap_success_is_not_business_completion(self):
        frames, logs = self.logs()
        logs = self.boot_logs() + [x for x in logs if 'autostart' not in x and '全部完成' not in x]
        self.assertEqual(maagf2.evaluate(frames, logs, 0, 42, 'default', command_verified=True)['status'],
                         'completion_unconfirmed')
        self.assertEqual(maagf2.evaluate(frames, logs + [gui('任务已全部完成！')], 0, 42, 'default',
                                        command_verified=True)['status'], 'success')

    def test_native_command_sent_once_only_after_connection_and_logged_intent(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            current = {'cwd': directory, 'instance_id': 'default', 'native_startup_timeout': 120,
                       'game_path': 'game.exe', 'startup_window_filter': {}, 'command_pipe_key': 'key'}
            _, cursor = maagf2.bind_execution(current, directory)
            root = {'pid': 42, 'image': 'assistant.exe', 'creation_ticks': 10, 'in_owned_job': True}
            process = SimpleNamespace(pid=42)
            receipt = {'status': 'acknowledged', 'root_pid': 42, 'pipe_server_pid': 42,
                       'instance_id': 'default', 'force_start': False, 'requests': 1}
            def send(*args, **kwargs):
                intent = json.loads((Path(directory) / 'native-start-command.json').read_text())
                self.assertEqual(intent['status'], 'attempted')
                return receipt
            with patch('gachaops.maagf2.request_foreground', return_value={'status': 'foreground_denied', 'hwnd': 77}), \
                 patch('gachaops.maagf2.read_events', return_value=([], [])), \
                 patch('gachaops.maagf2.bootstrap_ready', side_effect=[False, True]), \
                 patch('gachaops.maagf2.instance_key', return_value='key'), \
                 patch('gachaops.maagf2.send_start', side_effect=send) as command:
                self.assertEqual(maagf2.advance_startup(current, cursor, process, [root], directory, 5)['status'],
                                 'waiting_for_native_connection')
                command.assert_not_called()
                self.assertEqual(maagf2.advance_startup(current, cursor, process, [root], directory, 7)['status'], 'acknowledged')
                maagf2.advance_startup(current, cursor, process, [root], directory, 9)
                command.assert_called_once()
            _, fresh = maagf2.bind_execution(current, directory)
            with self.assertRaisesRegex(ValueError, 'timeout'):
                maagf2.advance_startup(current, fresh, process, [root], directory, 121)
            cursor.command['acknowledged_monotonic'] -= 31
            with patch('gachaops.maagf2.read_events', return_value=([], [])), \
                 patch('gachaops.maagf2.send_start') as command:
                with self.assertRaisesRegex(ValueError, 'never started'):
                    maagf2.advance_startup(current, cursor, process, [root], directory, 38)
                command.assert_not_called()

    def test_missing_acknowledgement_keeps_attempted_command_and_never_resends(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            current = {'cwd': directory, 'instance_id': 'default', 'native_startup_timeout': 120,
                       'game_path': 'game.exe', 'startup_window_filter': {}, 'command_pipe_key': 'key'}
            _, cursor = maagf2.bind_execution(current, directory)
            root = {'pid': 42, 'image': 'assistant.exe', 'creation_ticks': 10, 'in_owned_job': True}
            with patch('gachaops.maagf2.request_foreground', return_value={'status': 'foreground', 'hwnd': 77}), \
                 patch('gachaops.maagf2.read_events', return_value=([], [])), \
                 patch('gachaops.maagf2.bootstrap_ready', return_value=True), \
                 patch('gachaops.maagf2.instance_key', return_value='key'), \
                 patch('gachaops.maagf2.send_start', side_effect=ValueError('ack timeout')) as command:
                with self.assertRaisesRegex(ValueError, 'ack timeout'):
                    maagf2.advance_startup(current, cursor, SimpleNamespace(pid=42), [root], directory, 7)
                self.assertEqual(cursor.command['status'], 'attempted')
                maagf2.advance_startup(current, cursor, SimpleNamespace(pid=42), [root], directory, 9)
                command.assert_called_once()

    def logs(self):
        frames = [frame(42, '[Logger] MAA Process Start'), event('Starting'), event('Succeeded')]
        logs = [gui('启动参数：数量=3，内容=[autostart, instance=default, quit-after-run]', False),
                gui('准备执行任务队列：任务数量=1'), gui('开始任务：启动少女前线2'),
                gui('任务已全部完成！'), gui(' 应用已关闭', False)]
        return frames, logs

    def test_global_start_requires_full_business_queue_before_owned_stop(self):
        frames, logs = self.logs()
        marker = gui(' 全局启动完成：已启动 1 个，跳过 0 个，失败 0 个', False)
        logs = [marker] + logs[1:-1]  # native GUI remains open after daily completion
        result = maagf2.evaluate(frames, logs, None, 42, 'default', native_global=True)
        self.assertTrue(result['task_queue_verified'])
        self.assertEqual(result['status'], 'completion_unconfirmed')
        self.assertEqual(maagf2.evaluate(frames, logs, None, 42, 'default', native_global=True,
                                        owned_stop=True)['status'], 'success')
        failed = [frames[0], frames[1], event('Failed')]
        self.assertEqual(maagf2.evaluate(failed, logs, None, 42, 'default', native_global=True,
                                        owned_stop=True)['status'], 'partial_failure')
        for bad_frames, bad_logs in ((frames[:-1], logs), (frames, logs[1:]),
                (frames, logs + [marker]), (frames, logs + [gui('已放弃本次任务')]),
                (frames, [x.replace('任务数量=1', '任务数量=2') for x in logs]),
                (frames, [x.replace('default', 'other') for x in logs]),
                (frames, logs + [gui('准备执行任务队列：任务数量=1')])):
            self.assertFalse(maagf2.evaluate(bad_frames, bad_logs, None, 42, 'default',
                                           native_global=True, owned_stop=True)['task_queue_verified'])

    def test_global_start_never_sends_an_extra_command(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            _, cursor = maagf2.bind_execution({'cwd': directory, 'instance_id': 'default',
                                             'native_startup_mode': 'global'}, directory)
            process = SimpleNamespace(pid=42)
            current = {'native_startup_timeout': 120}
            with patch('gachaops.maagf2.observe', side_effect=[{}, {'native_global_started': True},
                         {'native_global_started': True, 'task_queue_verified': True}]), \
                 patch('gachaops.maagf2.send_start') as command:
                self.assertEqual(maagf2.advance_startup(current, cursor, process, [], directory, 2)['status'],
                                 'waiting_for_native_auto_start')
                self.assertEqual(maagf2.advance_startup(current, cursor, process, [], directory, 4)['requests'], 0)
                self.assertTrue(maagf2.advance_startup(current, cursor, process, [], directory, 150)['task_queue_verified'])
                command.assert_not_called()
            self.assertFalse((Path(directory) / 'native-start-command.json').exists())
            with patch('gachaops.maagf2.observe', return_value={}):
                with self.assertRaisesRegex(ValueError, 'deadline'):
                    maagf2.advance_startup(current, cursor, process, [], directory, 121)

    def test_supervisor_closes_only_owned_job_after_verified_global_queue(self):
        import subprocess
        from gachaops.ok_trial import run_prepared
        from gachaops.recovery import ExecutionSession, read_state
        from gachaops.windows_process import CleanupError
        for cleanup_failure in (False, True):
            with self.subTest(cleanup_failure=cleanup_failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                current = {'cwd': directory, 'installation_root': directory, 'instance_id': 'default',
                           'native_startup_mode': 'global', 'native_startup': True,
                           'argv_candidate': [str(root / 'assistant.exe')], 'game_path': str(root / 'game.exe')}
                _, cursor = maagf2.bind_execution(current, root)
                owned = {'pid': 42, 'creation_ticks': 20, 'image': current['game_path'], 'in_owned_job': True}
                closed = []
                class Process:
                    pid = 42
                    def __init__(self, *args, **kwargs): pass
                    def __enter__(self): return self
                    def wait(self, timeout): raise subprocess.TimeoutExpired('fixture', timeout)
                    def __exit__(self, *_):
                        closed.append(True)
                        if cleanup_failure: raise CleanupError('fixture cleanup failure')
                frames, logs = self.logs()
                logs = [gui(' 全局启动完成：已启动 1 个，跳过 0 个，失败 0 个', False)] + logs[1:-1]
                with patch('gachaops.ok_trial.WindowsProcess', Process), \
                     patch('gachaops.ok_trial.owned_processes', return_value=[owned]), \
                     patch('gachaops.ok_trial.find_matching_processes', return_value={'status': 'observed', 'matches': [], 'uncertain': []}), \
                     patch('gachaops.ok_trial.verify_observed_membership', return_value=[]), \
                     patch('gachaops.ok_trial.request_foreground', return_value={'status': 'waiting_for_window'}), \
                     patch('gachaops.ok_trial.allow_foreground'), \
                     patch('gachaops.ok_trial.runner_environment', return_value={}), \
                     patch('gachaops.maagf2.read_events', return_value=(frames, logs)), \
                     patch('gachaops.maagf2.send_start') as command:
                    with ExecutionSession(root / 'state', 'fixture') as session:
                        report, _ = run_prepared(current, session, root, cursor, 30, completion_observer=maagf2.observe)
                command.assert_not_called()
                self.assertEqual(closed, [True])
                self.assertIsNone(report['exit_code'])
                self.assertTrue((root / 'native-owned-stop.json').exists())
                if cleanup_failure:
                    self.assertEqual(report['status'], 'ownership_or_cleanup_unconfirmed')
                    self.assertFalse(report['cleanup_verified'])
                    self.assertNotEqual(read_state(root / 'state')['phase'], 'idle')
                else:
                    self.assertEqual(report['status'], 'success')
                    self.assertTrue(report['cleanup_verified'])
                    self.assertEqual(read_state(root / 'state')['phase'], 'idle')

    def test_completed_callbacks_and_selected_queue_required(self):
        frames, logs = self.logs()
        self.assertEqual(maagf2.evaluate(frames, logs, 0, 42, 'default')['status'], 'success')
        for changed_frames, changed_logs, code in (
                (frames[:-1], logs, 0), (frames, logs[:-1], 0),
                (frames, [x for x in logs if '全部完成' not in x], 0),
                (frames, [x.replace('default', 'other') for x in logs], 0),
                (frames, logs + [gui('已放弃本次任务')], 0),
                (frames, [x.replace('任务数量=1', '任务数量=2') for x in logs], 0),
                (frames, logs, None)):
            with self.subTest(logs=changed_logs):
                self.assertEqual(maagf2.evaluate(changed_frames, changed_logs, code, 42, 'default')['status'],
                                 'completion_unconfirmed')
        self.assertEqual(maagf2.evaluate(frames, logs, 5, 42, 'default')['status'], 'failed')

    def test_quit_after_run_can_precede_async_completion_toast(self):
        frames, logs = self.logs()
        stop = gui('停止前状态：SUCCEEDED').replace('[src=Monitor][op=MonitorLog]', '[src=Worker][op=StopTask]')
        logs = [x for x in logs if '全部完成' not in x]
        logs.insert(-1, stop)
        self.assertEqual(maagf2.evaluate(frames, logs, 0, 42, 'default', command_verified=True)['status'], 'success')
        for bad_frames, bad_logs, code, verified in (
                (frames[:-1], logs, 0, True), (frames, logs, None, True), (frames, logs, 0, False),
                (frames, [x.replace('SUCCEEDED', 'STOPPED') for x in logs], 0, True),
                (frames, [x.replace('任务数量=1', '任务数量=2') for x in logs], 0, True),
                (frames, [logs[0], stop] + logs[1:-2] + logs[-1:], 0, True)):
            self.assertEqual(maagf2.evaluate(bad_frames, bad_logs, code, 42, 'default',
                                            command_verified=verified)['status'], 'completion_unconfirmed')
        self.assertEqual(maagf2.evaluate([frames[0], frames[1], event('Failed')], logs, 0, 42,
                                         'default', command_verified=True)['status'], 'partial_failure')

    def test_copy_and_truncate_rotation_authenticates_old_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'debug').mkdir(); (root / 'logs').mkdir()
            native = root / 'debug/maafw.log'; ui = root / 'logs/log-20261002.log'
            old = frame(99, '[Logger] MAA Process Start') + '\n'
            native.write_text(old, encoding='utf-8'); ui.write_text('', encoding='utf-8')
            _, cursor = maagf2.bind_execution({'cwd': str(root), 'instance_id': 'default'}, root)
            frames, logs = self.logs()
            stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            frames = [x.replace('2026-10-02 14:00:00.000', stamp) for x in frames]
            logs = [x.replace('2026-10-02 14:00:00.000', stamp) for x in logs]
            archive = root / 'debug/maafw.bak.copy.log'
            archive.write_text(old + '\n'.join(frames[:2]) + '\n', encoding='utf-8')
            native.write_text(frames[2] + '\n', encoding='utf-8')
            ui.write_text('\n'.join(logs) + '\n', encoding='utf-8')
            owned = [{'pid': 42, 'in_owned_job': True}]
            self.assertEqual(maagf2.observe(cursor, 0, 42, owned)['status'], 'success')
            # A copied archive with an unauthenticated prefix still blocks.
            archive.write_text(old.replace('Px99', 'Px98') + '\n'.join(frames[:2]) + '\n', encoding='utf-8')
            self.assertIn('old events', maagf2.observe(cursor, 0, 42, owned)['reason'])

    def test_partial_failures_cancellation_callbacks_and_duplicate_writers(self):
        frames, logs = self.logs()
        failed = [frames[0], frames[1], event('Failed')]
        result = maagf2.evaluate(failed, logs, 0, 42, 'default')
        self.assertEqual(result['status'], 'partial_failure')
        self.assertEqual(result['failed_applications'], ['StartGame'])
        stop = [frames[0], event('Starting', entry='MaaTaskerPostStop'),
                event('Succeeded', entry='MaaTaskerPostStop')]
        self.assertEqual(maagf2.evaluate(stop, logs, 0, 42, 'default')['status'], 'completion_unconfirmed')
        for bad in (frames + [frames[-1]], [frames[0], frames[-1]],
                    [x.replace('Px42', 'Px99') for x in frames]):
            with self.assertRaises(ValueError):
                maagf2.evaluate(bad, logs, 0, 42, 'default')

    def test_readonly_prepare_and_disabled_unconfigured_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ('MaaGF2Exilium.exe', 'libs/MFAAvalonia.Core.dll',
                         'runtimes/win-x64/native/MaaFramework.dll', 'game.exe'):
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes('autostart quit-after-run instance'.encode('utf-16le'))
            (root / 'interface.json').write_text(json.dumps({'version': 'v2.7.2', 'controller': [
                {'name': '桌面端', 'type': 'Win32', 'win32': {'class_regex': '.*', 'window_regex': '^EXILIUM$'}}]}))
            p = root / 'config/instances/default.json'
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'InstanceName': 'Preset', 'CurrentController': 1,
                                    'BeforeTask': 'StartupSoftware', 'SoftwarePath': str(root / 'game.exe'),
                                    'TaskItems': {'opaque': 'must remain unchanged'}}))
            (root / 'config/config.json').write_text('{}')
            before = p.read_bytes()
            row = {'id': 'gf2', 'adapter': 'maa-gf2', 'installation_root': str(root), 'enabled': True}
            with patch('gachaops.maagf2.executable_product_name', return_value='MFAAvalonia'), \
                 patch('gachaops.maagf2.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.maagf2.SUPPORTED_CORE_SHA256', __import__('hashlib').sha256((root / 'libs/MFAAvalonia.Core.dll').read_bytes()).hexdigest()), \
                 patch('gachaops.maagf2.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.maagf2.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                current = prepare_assistant(row)
                self.assertEqual(current['argv_candidate'][1:], ['--instance', 'default'])
                self.assertFalse(current['blockers'])
                with patch('gachaops.maagf2.SUPPORTED_CORE_SHA256', 'unknown'):
                    self.assertIn('native_command_protocol_unverified', prepare_assistant(row)['blockers'])
                # A compatible update is accepted only after its precise Core
                # fingerprint has been audited; unknown bytes still block.
                with patch('gachaops.maagf2.hashlib.sha256') as digest:
                    digest.return_value.hexdigest.return_value = '4cc6a10ef5c7cb6c728070d8e28730ea0fa79e104f77d2781314fd55d917c8e7'
                    self.assertNotIn('native_command_protocol_unverified', prepare_assistant(row)['blockers'])
                    digest.return_value.hexdigest.return_value = '4cc6a10ef5c7cb6c728070d8e28730ea0fa79e104f77d2781314fd55d917c8e8'
                    self.assertIn('native_command_protocol_unverified', prepare_assistant(row)['blockers'])
                self.assertTrue(current['native_startup'])
                self.assertNotIn('additional_roots', current)
                self.assertEqual(current['startup_window_filter']['window_regex'], '^EXILIUM$')
                game_dir = root / 'GF2 Game'
                game_dir.mkdir()
                (game_dir / 'game.exe').write_bytes(b'fixture')
                instance_data = json.loads(p.read_text())
                instance_data['SoftwarePath'] = str(game_dir / 'game.exe')
                p.write_text(json.dumps(instance_data))
                launcher = root / 'PCLauncher.exe'
                launcher.write_bytes(b'fixture')
                observed = prepare_assistant(row)
                self.assertEqual(observed['official_launcher'], str(launcher))
                self.assertIn(str(launcher), observed['observation_targets'])
                self.assertNotIn(str(launcher), observed['argv_candidate'])
                with patch('gachaops.maagf2.find_matching_processes', return_value={
                        'status': 'observed', 'matches': [{'pid': 99, 'image': str(launcher)}]}):
                    self.assertIn('existing_runner_or_game_not_exclusive', prepare_assistant(row)['blockers'])
                p.write_bytes(before)
                global_path = root / 'appsettings.json'
                global_path.write_text(json.dumps({'GlobalStartEnabled': 'True', 'NoAutoStart': 'False'}))
                self.assertEqual(prepare_assistant(row)['native_startup_mode'], 'global')
                self.assertFalse(prepare_assistant(row)['blockers'])
                global_before = global_path.read_bytes()
                for settings, blocker in (({'GlobalStartEnabled': 'True', 'GlobalExtraLaunchCount': '1'},
                                           'native_global_start_must_target_only_saved_instance'),
                                          ({'NoAutoStart': 'True'}, 'native_single_instance_startup_required')):
                    global_path.write_text(json.dumps(settings))
                    self.assertIn(blocker, prepare_assistant(row)['blockers'])
                global_path.write_bytes(global_before)
                (root / 'config/config.json').write_text(json.dumps({'Instances.LastActive': 'default'}))
                (root / 'config/instances/second.json').write_bytes(before)
                self.assertIn('native_global_start_must_target_only_saved_instance', prepare_assistant(row)['blockers'])
                self.assertEqual(global_path.read_bytes(), global_before)
                (root / 'config/config.json').write_text('{}')
                with self.assertRaisesRegex(ValueError, 'ambiguous'):
                    prepare_assistant(row)
            self.assertEqual(p.read_bytes(), before)
            row.update(enabled=False, installation_root=None, entry_executable=None)
            config = root / 'assistants.json'
            config.write_text(json.dumps({'schema_version': 1, 'assistants': [row]}))
            self.assertEqual(load_assistants(config)['assistants'][0]['installation_root'], None)

    def test_old_and_foreign_logs_cannot_prove_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'debug').mkdir()
            (root / 'logs').mkdir()
            frames, logs = self.logs()
            (root / 'debug/maafw.log').write_text('\n'.join(frames) + '\n', encoding='utf-8')
            (root / 'logs/log-20261002.log').write_text('\n'.join(logs) + '\n', encoding='utf-8')
            current = {'cwd': str(root), 'instance_id': 'default'}
            _, cursor = maagf2.bind_execution(current, root)
            owned = [{'pid': 42, 'in_owned_job': True}]
            self.assertEqual(maagf2.observe(cursor, 0, 42, owned)['status'], 'completion_unconfirmed')
            # Replacing a log with old success is rejected by timestamp.
            (root / 'debug/maafw.log').write_text('\n'.join(frames) + '\nextra\n', encoding='utf-8')
            from dataclasses import replace
            new_cursor = replace(cursor, baseline={}, captured_at_ns=time.time_ns() + 86400 * 10**9)
            result = maagf2.observe(new_cursor, 0, 42, owned)
            self.assertIn('old events', result['reason'])

    def test_fresh_append_owned_agent_and_foreign_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'debug').mkdir()
            (root / 'logs').mkdir()
            native = root / 'debug/maafw.log'
            ui = root / 'logs/log-20261002.log'
            native.write_text('old output\n')
            ui.write_text('old output\n')
            _, cursor = maagf2.bind_execution({'cwd': str(root), 'instance_id': 'default'}, root)
            frames, logs = self.logs()
            stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            frames = [x.replace('2026-10-02 14:00:00.000', stamp) for x in frames]
            logs = [x.replace('2026-10-02 14:00:00.000', stamp) for x in logs]
            native.write_text('old output\n' + '\n'.join(frames) + '\n', encoding='utf-8')
            ui.write_text('old output\n' + '\n'.join(logs) + '\n', encoding='utf-8')
            owned = [{'pid': 42, 'in_owned_job': True}]
            self.assertEqual(maagf2.observe(cursor, 0, 42, owned)['status'], 'success')
            with native.open('a', encoding='utf-8') as stream:
                stream.write(frames[0].replace('Px42', 'Px99') + '\n')
            self.assertEqual(maagf2.observe(cursor, 0, 42, owned)['status'], 'completion_unconfirmed')
            agent = {'pid': 99, 'in_owned_job': True, 'image': str(root / 'python/python.exe'),
                     'creation_ticks': int((time.time() + 11644473600 - 10) * 1e7)}
            self.assertEqual(maagf2.observe(cursor, 0, 42, owned + [agent])['status'], 'success')


if __name__ == '__main__':
    unittest.main()
