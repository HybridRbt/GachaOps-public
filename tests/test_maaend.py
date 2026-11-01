from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops import maaend
from gachaops.assistants import run_assistants


def frame(message, pid=123):
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
    return f'[{stamp}][INF][Px{pid}][Tx1]{message}'


def event(kind, identity, pid=123):
    return frame('[Utils/EventDispatcher.hpp][L65][notify] !!!OnEventNotify!!! [handle=true] '
                 f'[msg=Tasker.Task.{kind}] [details=' + json.dumps({'task_id': identity, 'entry': f'task-{identity}'}) + ']', pid)


def gui(message):
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' INFO  [App] ' + message


def cycle(failed=False):
    return [frame('[Logger] MAA Process Start'), event('Starting', 1), event('Succeeded', 1),
            event('Starting', 2), event('Failed' if failed else 'Succeeded', 2)], [
                gui('实例 全套日常: 任务已提交, task_ids: [1,2]'), gui('自动执行任务完成，关闭自身')]


class MaaEndTests(unittest.TestCase):
    def test_complete_batch_and_partial_failure(self):
        for failed in (False, True):
            frames, logs = cycle(failed)
            result = maaend.evaluate(frames, logs, 0, 123, '全套日常')
            self.assertEqual(result['status'], 'partial_failure' if failed else 'success')
            self.assertEqual(result['failed_applications'], ['task-2'] if failed else [])

    def test_missing_terminal_wrong_instance_and_interruption_never_succeed(self):
        frames, logs = cycle()
        for f, g, code in [(frames[:-1], logs, 0), (frames, logs[:-1], 0),
                           (frames, [], 0), (frames, logs + logs[:1], 0),
                           (frames, logs, None), (frames, [l.replace('全套日常', '其他实例') for l in logs], 0)]:
            self.assertEqual(maaend.evaluate(f, g, code, 123, '全套日常')['status'], 'completion_unconfirmed')
        self.assertEqual(maaend.evaluate(frames, logs, 1, 123, '全套日常')['status'], 'failed')
        with self.assertRaisesRegex(ValueError, 'not owned'):
            maaend.evaluate(frames, logs, 0, 456, '全套日常')
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            maaend.evaluate(frames + frames[-1:], logs, 0, 123, '全套日常')

    def test_snapshot_clear_rotation_and_old_log_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            debug = root / 'debug';debug.mkdir()
            original = debug / 'maafw.log'
            original.write_text('old session\n', encoding='utf-8')
            output = root / 'run';output.mkdir()
            current = {'cwd': str(root), 'instance_name': '全套日常'}
            _, cursor = maaend.bind_execution(current, output)
            original.rename(debug / 'maafw.bak.fixture.log')
            frames, logs = cycle(True)
            original.write_text('\n'.join(frames) + '\n', encoding='utf-8')
            (debug / '2026-10-01-1.log').write_text('\n'.join(logs) + '\n', encoding='utf-8')
            owned = [{'pid': 123, 'in_owned_job': True}]
            self.assertEqual(maaend.observe(cursor, 0, 123, owned)['status'], 'partial_failure')
            self.assertEqual(maaend.observe(cursor, 0, 123, [])['status'], 'completion_unconfirmed')
            original.write_text('\n'.join(l.replace(l[1:24], '2000-01-01 00:00:00.000') for l in frames) + '\n', encoding='utf-8')
            self.assertEqual(maaend.observe(cursor, 0, 123, owned)['status'], 'completion_unconfirmed')

    def test_selected_gui_tasks_stay_opaque_and_mxu_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').mkdir();(root / 'maafw').mkdir()
            (root / 'maafw/MaaFramework.dll').write_bytes(b'fixture')
            (root / 'MaaEnd.exe').write_bytes(b'--autostart --instance --quit-after-run')
            (root / 'interface.json').write_text(json.dumps({'version': 'fixture'}))
            game = root / 'Endfield.exe';game.write_bytes(b'fixture')
            config = {'lastActiveInstanceId': 'daily', 'instances': [
                {'id': 'daily', 'name': '全套日常', 'controllerName': 'Win32-Front',
                 'tasks': 'opaque data belonging to native GUI',
                 'preActions': [{'enabled': True, 'program': str(game), 'useCmd': False}]}]}
            path = root / 'config/mxu-MaaEnd.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            original = path.read_bytes()
            row = {'id': 'endfield', 'adapter': 'maa-end', 'installation_root': str(root), 'enabled': True}
            with patch('gachaops.maaend.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.maaend.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.maaend.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                current = maaend.prepare(row)
                self.assertEqual(current['argv_candidate'][-4:], ['--autostart', '--instance', '全套日常', '--quit-after-run'])
                self.assertEqual(current['blockers'], [])
                self.assertEqual(path.read_bytes(), original)
                with patch('gachaops.assistants.windows_available', return_value=True), \
                     patch('gachaops.assistants.run_prepared', return_value=({'status': 'success', 'cleanup_verified': True}, root)) as launch:
                    run_assistants({'assistants': [row]}, root / 'runs', root / 'state', progress=lambda _: None)
                    self.assertIs(launch.call_args.kwargs['completion_observer'], maaend.observe)
                config['instances'].append(dict(config['instances'][0], id='other'))
                path.write_text(json.dumps(config), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'duplicate'):
                    maaend.prepare(row)

    def test_shared_log_accepts_only_owned_installation_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'debug').mkdir()
            output = root / 'run'; output.mkdir()
            _, cursor = maaend.bind_execution({'cwd': str(root), 'instance_name': '全套日常'}, output)
            frames, logs = cycle(True)
            native = root / 'debug/maafw.log'
            shared = [frame('[Logger] MAA Process Start', 456), event('Succeeded', 99, 456)]
            native.write_text('\n'.join(frames + shared) + '\n', encoding='utf-8')
            (root / 'debug/2026-10-01-1.log').write_text('\n'.join(logs) + '\n', encoding='utf-8')
            owner = {'pid': 123, 'in_owned_job': True}
            agent = {'pid': 456, 'in_owned_job': True,
                     'image': str(root / 'agent/go-service.exe'),
                     'creation_ticks': int((datetime.now().timestamp() - 10 + 11_644_473_600) * 10_000_000)}
            result = maaend.observe(cursor, 0, 123, [owner, agent])
            self.assertEqual(result['status'], 'partial_failure')
            self.assertEqual(result['task_statuses'], {'1': 'Succeeded', '2': 'Failed'})
            self.assertEqual(result['auxiliary_log_writers'], [agent])
            for changes in ({'in_owned_job': False}, {'pid': 789},
                            {'image': str(root / 'elsewhere/go-service.exe')},
                            {'creation_ticks': None},
                            {'creation_ticks': agent['creation_ticks'] + 300_000_000}):
                self.assertEqual(maaend.observe(cursor, 0, 123, [owner, dict(agent, **changes)])['status'],
                                 'completion_unconfirmed')
            # An agent terminal callback cannot substitute for the root's one.
            native.write_text('\n'.join(frames[:-1] + [event('Failed', 2, 456)]) + '\n', encoding='utf-8')
            self.assertEqual(maaend.observe(cursor, 0, 123, [owner, agent])['status'], 'completion_unconfirmed')
            # An agent initialization cannot establish root initialization.
            native.write_text('\n'.join(frames[1:] + shared) + '\n', encoding='utf-8')
            self.assertEqual(maaend.observe(cursor, 0, 123, [owner, agent])['status'], 'completion_unconfirmed')
