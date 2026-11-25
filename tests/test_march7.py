import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from gachaops import march7
from gachaops.assistants import load_assistants, run_assistants


COMPLETE = '| 开始运行 |\n--- 完成 ---\n| 停止运行 |\n--- 完成 ---\n'


class March7Tests(unittest.TestCase):
    def test_reviewed_versions_prepare_but_unknown_versions_block_in_chinese(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); row = self.installation(root)
            version = root / 'assets/config/version.txt'
            with patch.object(march7, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(march7, 'desktop_report', return_value={'status': 'observed_available'}), \
                 patch.object(march7, 'find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                for value in ('v2026.9.30', 'v2026.10.16', 'v2026.11.8'):
                    with self.subTest(version=value):
                        version.write_text(value, encoding='utf-8')
                        result = march7.prepare(row)
                        self.assertEqual(result['runner_version'], value)
                        self.assertEqual(result['blockers'], [])
                version.write_text('v2026.10.99', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, '尚未核验.*v2026.10.99.*GachaOps'):
                    march7.prepare(row)

    def test_worker_rejects_version_drift_even_between_reviewed_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.installation(root)
            descriptor = dict(installation_root=str(root), cwd=str(root), token='t',
                argv_candidate=[str(root / 'March7th Assistant.exe'), 'main'],
                config_sha256=hashlib.sha256((root / 'config.yaml').read_bytes()).hexdigest())
            launch = root / 'march7-launch.json'
            for prepared, installed in (('v2026.10.16', 'v2026.9.30'),
                                         ('v2026.9.30', 'v2026.10.16'),
                                         ('v2026.10.16', 'unknown'), (None, 'v2026.9.30'),
                                         ('v2026.11.8', 'v2026.9.30')):
                with self.subTest(prepared=prepared, installed=installed):
                    descriptor['runner_version'] = prepared
                    launch.write_text(json.dumps(descriptor), encoding='utf-8')
                    (root / 'assets/config/version.txt').write_text(installed, encoding='utf-8')
                    with patch.object(march7.subprocess, 'Popen', side_effect=AssertionError('must not start')) as spawn, \
                         patch.object(march7, 'creation_ticks', return_value=10):
                        with self.assertRaisesRegex(ValueError, '版本.*变化|未核验'):
                            march7.run_child(['--launch', str(launch)])
                        spawn.assert_not_called()

    def test_daily_failures_and_explicit_retry_recovery(self):
        text = ('| 开始运行 |\n'
                '16:33:41 | WARNING | 切换到 主界面 超时，准备重试\n'
                '16:33:42 | INFO | 当前界面：手机菜单\n'
                '16:33:44 | INFO | 切换到：主界面\n'
                '16:34:00 | INFO | 当前累计分数：200/500\n'
                '16:34:10 | INFO | 完成任务: 委托 (+100分)，当前分数: 300/500\n'
                '16:34:10 | INFO | 任务无法完成: 累计消耗120点开拓力\n'
                '16:34:10 | INFO | 任务无法完成: 使用支援角色并获得战斗胜利1次\n'
                '16:34:10 | INFO | 任务无法完成: 使用1次「万能合成机」\n'
                '16:34:10 | INFO | 任务无法完成: 完成1次「差分宇宙」或「货币战争」\n'
                '16:34:34 | INFO | 每日实训未完成\n'
                '--- 每日实训奖励完成 ---\n| 停止运行 |\n--- 完成 ---\n')
        result = march7.evaluate(text, 0)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertEqual(len(result['failed_applications']), 4)
        self.assertEqual(result['daily_training_score'], '300/500')
        self.assertTrue(result['daily_training_incomplete'])
        self.assertEqual(result['failure_warnings'], [])
        self.assertEqual(len(result['recovered_warnings']), 1)
        self.assertEqual(march7.evaluate(text.replace('切换到：主界面', '切换到：委托'), 0)['recovered_warnings'], [])
        self.assertEqual(march7.evaluate(text.replace('| 停止运行 |', ''), 0)['status'], 'completion_unconfirmed')

    def test_recovered_warning_alone_and_later_task_completion(self):
        body = ('| 开始运行 |\n1 | WARNING | 切换到 主界面 超时，准备重试\n'
                '2 | INFO | 切换到：主界面\n3 | INFO | 任务无法完成: 委托\n'
                '4 | INFO | 完成任务: 委托 (+100分)，当前分数: 500/500\n'
                '| 停止运行 |\n--- 完成 ---\n')
        self.assertEqual(march7.evaluate(body, 0)['status'], 'success')
        self.assertEqual(march7.evaluate(body.replace('2 | INFO', '2 | WARNING'), 0)['status'], 'partial_failure')
        self.assertEqual(march7.evaluate(body.replace('4 | INFO | 完成任务:', '4 | INFO | 未完成任务:'), 0)['status'], 'partial_failure')

    def test_known_shutdown_footer_is_accepted_but_unexpected_output_is_not(self):
        footer = ('+----+\n| 欢迎使用 March7th Assistant |\n'
                  '| https://github.com/moesnow/March7thAssistant |\n|  |\n'
                  '| 此程序为免费开源项目 如果你付了钱请立刻退款 |\n+----+\n')
        self.assertEqual(march7.evaluate(COMPLETE + footer, 0)['status'], 'success')
        self.assertEqual(march7.evaluate(COMPLETE + footer + 'unexpected\n', 0)['status'], 'completion_unconfirmed')
        self.assertEqual(march7.evaluate(COMPLETE.replace('| 停止运行 |', '| 未停止 |') + footer, 0)['status'], 'completion_unconfirmed')
        self.assertEqual(march7.evaluate(COMPLETE + footer, 1)['status'], 'runner_failed')
        self.assertEqual(march7.evaluate(COMPLETE + 'INFO | ERROR | failed\n' + footer, 0)['status'], 'completion_unconfirmed')

    def installation(self, root, finish='Exit'):
        (root / 'assets/config').mkdir(parents=True)
        (root / 'assets/config/version.txt').write_text(march7.SUPPORTED_VERSION)
        for name in ('March7th Assistant.exe', 'March7th Launcher.exe', 'StarRail.exe'):
            (root / name).write_bytes(b'fixture')
        (root / 'config.yaml').write_text(
            f"game_path: '{root / 'StarRail.exe'}' # location\ncloud_game_enable: false\n"
            f"after_finish: {finish}\nauto_update: true\nlog_level: INFO\n"
            'tasks: opaque native options\n', encoding='utf-8')
        return {'id': 'honkai-star-rail', 'adapter': 'march7', 'installation_root': str(root),
                'enabled': True, 'game_start': 'assistant'}

    def test_saved_preset_opaque_and_dispatch_uses_private_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); row = self.installation(root)
            config = root / 'config.yaml'; before = config.read_bytes()
            path = root / 'assistants.json';path.write_text(json.dumps({'schema_version': 1, 'assistants': [row]}))
            with patch('gachaops.march7.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.march7.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.march7.find_matching_processes', return_value={'status': 'observed', 'matches': []}), \
                 patch('gachaops.assistants.windows_available', return_value=True), \
                 patch('gachaops.assistants.run_prepared', return_value=({'status': 'success', 'cleanup_verified': True}, root)) as run:
                current = march7.prepare(row)
                self.assertEqual(current['argv_candidate'], [str(root / 'March7th Assistant.exe'), 'main'])
                self.assertEqual(current['blockers'], [])
                run_assistants(load_assistants(path), root / 'runs', root / 'state', progress=lambda _: None)
                self.assertIs(run.call_args.kwargs['completion_observer'], march7.observe)
            self.assertEqual(before, config.read_bytes())

    def test_system_actions_loop_cloud_unknown_version_and_duplicate_metadata_block(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); row = self.installation(root)
            with patch('gachaops.march7.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.march7.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.march7.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                config=root/'config.yaml'; original=config.read_text(encoding='utf-8')
                for finish in ('Shutdown', 'Sleep', 'Restart', 'RunScript', 'Loop', 'None'):
                    config.write_text(original.replace('after_finish: Exit', 'after_finish: '+finish), encoding='utf-8')
                    self.assertIn('march7_select_exit_in_assistant_gui', march7.prepare(row)['blockers'])
                config.write_text(original.replace('cloud_game_enable: false', 'cloud_game_enable: true'), encoding='utf-8')
                self.assertIn('march7_local_game_required', march7.prepare(row)['blockers'])
                config.write_text(original+'game_path: other\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'duplicate'): march7.prepare(row)
                config.write_text(original, encoding='utf-8')
                (root/'assets/config/version.txt').write_text('unknown')
                with self.assertRaisesRegex(ValueError, '核验'): march7.prepare(row)

    def test_result_requires_full_cycle_and_keeps_failure(self):
        self.assertEqual(march7.evaluate(COMPLETE, 0)['status'], 'success')
        self.assertEqual(march7.evaluate(COMPLETE, 1)['status'], 'runner_failed')
        for text in ('', COMPLETE.replace('| 停止运行 |', ''), COMPLETE+COMPLETE,
                     COMPLETE.rsplit('--- 完成 ---', 1)[0], '| 停止运行 |\n| 开始运行 |\n--- 完成 ---'):
            self.assertEqual(march7.evaluate(text, 0)['status'], 'completion_unconfirmed')
        self.assertEqual(march7.evaluate(COMPLETE, None)['status'], 'completion_unconfirmed')
        for line in ('2026-10-01 | ERROR | 任务失败', '2026-10-01 | WARNING | 超时'):
            self.assertEqual(march7.evaluate(line+'\n'+COMPLETE, 0)['status'], 'partial_failure')
        self.assertEqual(march7.evaluate('\x1b[31m'+COMPLETE+'\x1b[0m', 0)['status'], 'success')

    def test_receipt_token_both_owned_identities_and_output_digest_required(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp); cursor=march7.Cursor(temp, 'token', 'native.exe')
            output=directory/'march7-output.log';output.write_text(COMPLETE, encoding='utf-8')
            receipt={'schema_version':1, 'token':'token', 'pid':1, 'creation_ticks':10,
                     'native_identity':{'pid':2, 'creation_ticks':20, 'image':'native.exe'},
                     'native_argv':['native.exe','main'], 'exit_code':0,
                     'output_sha256':hashlib.sha256(output.read_bytes()).hexdigest()}
            result=directory/'march7-result.json'
            result.write_text(json.dumps(receipt))
            owned=[{'pid':1,'creation_ticks':10,'in_owned_job':True},
                   {'pid':2,'creation_ticks':20,'in_owned_job':True}]
            self.assertEqual(march7.observe(cursor, 0, 1, owned)['status'], 'success')
            for rows in ([], owned[:1], [dict(r, creation_ticks=99) for r in owned]):
                self.assertEqual(march7.observe(cursor, 0, 1, rows)['status'], 'completion_unconfirmed')
            output.write_text(COMPLETE+'tampered', encoding='utf-8')
            self.assertEqual(march7.observe(cursor, 0, 1, owned)['status'], 'completion_unconfirmed')
            output.write_text(COMPLETE, encoding='utf-8')
            receipt['token']='old';result.write_text(json.dumps(receipt))
            self.assertEqual(march7.observe(cursor, 0, 1, owned)['status'], 'completion_unconfirmed')

    def test_worker_only_changes_launch_environment_and_detects_config_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); self.installation(root)
            descriptor={'installation_root':temp,'cwd':temp,'argv_candidate':[str(root/'March7th Assistant.exe'),'main'],
                        'config_sha256':hashlib.sha256((root/'config.yaml').read_bytes()).hexdigest(), 'token':'t',
                        'runner_version': 'v2026.9.30'}
            launch=root/'march7-launch.json';launch.write_text(json.dumps(descriptor))
            child=MagicMock();child.pid=2;child._handle=42;child.wait.return_value=0
            popen=MagicMock();popen.return_value.__enter__.return_value=child
            with patch('gachaops.march7.runner_environment', return_value={'MARCH7TH_CLOUD_GAME_ENABLE':'true','MARCH7TH_CONTROL_FILE':'stale'}), \
                 patch('gachaops.march7.creation_ticks', return_value=10), \
                 patch('gachaops.march7.observation_api'), \
                 patch('gachaops.march7.process_identity', return_value={'pid':2,'creation_ticks':20,'image':descriptor['argv_candidate'][0]}), \
                 patch('gachaops.march7.subprocess.Popen', popen), \
                 patch('gachaops.march7.subprocess.CREATE_NO_WINDOW', 0, create=True):
                for version in ('v2026.9.30', 'v2026.10.16', 'v2026.11.8'):
                    (root / 'assets/config/version.txt').write_text(version, encoding='utf-8')
                    descriptor['runner_version'] = version
                    launch.write_text(json.dumps(descriptor), encoding='utf-8')
                    output = root / 'march7-output.log'
                    if output.exists(): output.unlink()
                    self.assertEqual(march7.run_child(['--launch',str(launch)]),0)
                    self.assertEqual(popen.call_args.kwargs['env'], {'MARCH7TH_GUI_STARTED':'true'})
                popen.reset_mock()
                (root/'config.yaml').write_text('changed')
                self.assertEqual(march7.worker_main(['--launch',str(launch)]),2)
                popen.assert_not_called()
                self.assertTrue((root/'march7-error.txt').exists())


    def test_v2026_11_24_prepare_worker_and_platform_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = self.installation(root)
            version = root / 'assets/config/version.txt'
            config = root / 'config.yaml'
            original = config.read_text(encoding='utf-8')
            with patch.object(march7, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(march7, 'desktop_report', return_value={'status': 'observed_available'}), \
                 patch.object(march7, 'find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                # The new tag is admitted; old configs without the platform
                # keys keep working with no new blockers.
                version.write_text('v2026.11.24', encoding='utf-8')
                self.assertEqual(march7.prepare(row)['blockers'], [])
                # Local explicit backend wins over cloud_game_enable.
                config.write_text(original + 'ui_profile: pc_1080\ncontrol_backend: local\n'
                                  'local_control_mode: foreground\nno_run_immediately: false\n',
                                  encoding='utf-8')
                self.assertEqual(march7.prepare(row)['blockers'], [])
                config.write_text(original.replace('cloud_game_enable: false', 'cloud_game_enable: true')
                                  + 'control_backend: local\n', encoding='utf-8')
                self.assertNotIn('march7_local_game_required', march7.prepare(row)['blockers'])
                # Fail-closed guards, each with its user-facing blocker code.
                for addition, expected in (
                        ('control_backend: cloud\n', 'march7_local_backend_required'),
                        ('control_backend: adb\n', 'march7_local_backend_required'),
                        ('control_backend: teleport\n', 'march7_local_backend_required'),
                        ('ui_profile: touch_720\n', 'march7_local_pc_profile_required'),
                        ('ui_profile: arcade\n', 'march7_local_pc_profile_required'),
                        ('local_control_mode: background\n', 'march7_local_foreground_control_required'),
                        ('no_run_immediately: true\n', 'march7_run_immediately_required')):
                    config.write_text(original + addition, encoding='utf-8')
                    with self.subTest(addition=addition.strip()):
                        self.assertIn(expected, march7.prepare(row)['blockers'])

    def test_worker_accepts_v2026_11_24_when_prepared_version_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); self.installation(root)
            (root / 'assets/config/version.txt').write_text('v2026.11.24', encoding='utf-8')
            descriptor = dict(installation_root=str(root), cwd=str(root), token='t',
                argv_candidate=[str(root / 'March7th Assistant.exe'), 'main'],
                runner_version='v2026.11.24',
                config_sha256=hashlib.sha256((root / 'config.yaml').read_bytes()).hexdigest())
            launch = root / 'march7-launch.json'
            launch.write_text(json.dumps(descriptor), encoding='utf-8')
            identity = {'image': str(root / 'March7th Assistant.exe'), 'pid': 4242,
                        'creation_ticks': 10}
            with patch.object(march7.subprocess, 'Popen') as popen, \
                 patch.object(march7, 'creation_ticks', return_value=10), \
                 patch.object(march7, 'observation_api', return_value=object()), \
                 patch.object(march7, 'process_identity', return_value=identity):
                instance = popen.return_value.__enter__.return_value
                instance.wait.return_value = 0
                instance.pid = 4242
                march7.run_child(['--launch', str(launch)])
            self.assertTrue(popen.called)
            receipt = json.loads((root / 'march7-result.json').read_text(encoding='utf-8'))
            self.assertEqual(receipt['exit_code'], 0)
            self.assertEqual(receipt['native_identity']['pid'], 4242)

    def test_v2026_11_24_training_report_text_is_parsed_both_ways(self):
        # 活跃度不满：plain claim line means the daily training is incomplete
        # even though start/stop markers and the exit code are clean.
        partial = ('| 开始运行 |\n'
                   '16:34:30 | WARNING | 实训未完成：清除冗余单元\n'
                   '16:34:40 | WARNING | 实训「模拟宇宙」未完成后无法返回实训页：超时\n'
                   '16:35:00 | INFO | 已领取实训奖励，活跃度 200/500\n'
                   '| 停止运行 |\n--- 完成 ---\n')
        result = march7.evaluate(partial, 0)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertIn('清除冗余单元', result['failed_applications'])
        self.assertIn('每日实训活跃度', result['failed_applications'])
        self.assertEqual(result['daily_training_score'], '200/500')
        self.assertTrue(result['daily_training_incomplete'])
        self.assertTrue(any('无法返回实训页' in line for line in result['failure_warnings']))
        self.assertEqual(result['evidence'], 'owned_native_full_run_and_stop_completed')
        # A healthy new-format run stays a clean success.
        healthy = ('| 开始运行 |\n| 停止运行 |\n--- 完成 ---\n')
        self.assertEqual(march7.evaluate(healthy, 0)['status'], 'success')
        self.assertFalse(march7.evaluate(healthy, 0)['daily_training_incomplete'])
        # Non-zero exit still dominates.
        self.assertEqual(march7.evaluate(partial, 1)['status'], 'runner_failed')


    def _prepare_with_config(self, root, row, addition, cloud=False, replace=None):
        config = root / 'config.yaml'
        pristine = getattr(self, '_pristine_config', None)
        if pristine is None or self._pristine_root != str(root):
            self._pristine_root = str(root)
            pristine = self._pristine_config = config.read_text(encoding='utf-8')
        if cloud:
            pristine = pristine.replace('cloud_game_enable: false', 'cloud_game_enable: true')
        if replace:
            pristine = pristine.replace(*replace)
        config.write_text(pristine + addition, encoding='utf-8')
        with patch('gachaops.march7.elevation_report', return_value={'elevated': True}), \
             patch('gachaops.march7.desktop_report', return_value={'status': 'observed_available'}), \
             patch('gachaops.march7.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
            return march7.prepare(row)['blockers']

    def test_quoted_keys_go_through_the_guards_like_upstream(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); row = self.installation(root)
            body = (root / 'config.yaml').read_text(encoding='utf-8')
            # Quoted key spellings are legal YAML; upstream reads their values,
            # so the guards must fire on them instead of silently skipping.
            blockers = self._prepare_with_config(root, row, '"control_backend": adb\n')
            self.assertIn('march7_local_backend_required', blockers)
            blockers = self._prepare_with_config(root, row, "'control_backend': cloud\n")
            self.assertIn('march7_local_backend_required', blockers)
            blockers = self._prepare_with_config(root, row, '"ui_profile": touch_720\n')
            self.assertIn('march7_local_pc_profile_required', blockers)
            blockers = self._prepare_with_config(root, row, '"no_run_immediately": "false"\n')
            self.assertIn('march7_run_immediately_required', blockers)
            # Quoted keys still count for duplicate detection.
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                self._prepare_with_config(root, row, '"game_path": other\n')
            # A quoted or spaced-colon spelling of a needed required key must
            # be read, not treated as missing. Spelled in place: the same key
            # twice (even quoted) is a duplicate upstream.
            config = root / 'config.yaml'
            config.write_text(body.replace('log_level: INFO', "'log_level' : DEBUG"), encoding='utf-8')
            with patch('gachaops.march7.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.march7.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.march7.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                self.assertNotIn('march7_info_logging_required', march7.prepare(row)['blockers'])

    def test_values_keep_yaml_types_like_upstream(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); row = self.installation(root)
            # Empty, ~ and null resolve to YAML null upstream; the reviewed
            # fallback then comes from cloud_game_enable (false in the
            # fixture), so no cloud/backend blocker may appear.
            for spelling in ('control_backend:\n', 'control_backend:  # 设备后端\n',
                             'control_backend: ~\n'):
                blockers = self._prepare_with_config(root, row, spelling)
                self.assertNotIn('march7_local_backend_required', blockers, spelling)
                self.assertNotIn('march7_local_game_required', blockers, spelling)
            # Bare null is YAML null upstream: the cloud_game_enable fallback
            # decides, so with the false fixture nothing blocks.
            blockers = self._prepare_with_config(root, row, 'control_backend: null\n')
            self.assertNotIn('march7_local_backend_required', blockers)
            self.assertNotIn('march7_local_game_required', blockers)
            # With the cloud switch on, the same null falls back to cloud.
            blockers = self._prepare_with_config(root, row, 'control_backend: null\n',
                                                 cloud=True)
            self.assertIn('march7_local_game_required', blockers)
            # The quoted "null" is a real string upstream and an unknown
            # backend there, so it blocks regardless of the cloud switch.
            blockers = self._prepare_with_config(root, row, 'control_backend: "null"\n')
            self.assertIn('march7_local_backend_required', blockers)
            # YAML 1.2 booleans are case-insensitive; FALSE is a false boolean,
            # so the run-immediately guard must not fire on it.
            blockers = self._prepare_with_config(root, row, 'no_run_immediately: FALSE\n')
            self.assertNotIn('march7_run_immediately_required', blockers)
            blockers = self._prepare_with_config(root, row, 'no_run_immediately: 0\n')
            self.assertNotIn('march7_run_immediately_required', blockers)
            # A quoted "false" is a truthy string upstream: cloud game enabled.
            blockers = self._prepare_with_config(root, row, '',
                                                 replace=('cloud_game_enable: false',
                                                          'cloud_game_enable: "false"'))
            self.assertIn('march7_local_game_required', blockers)
            blockers = self._prepare_with_config(root, row, '',
                                                 replace=('cloud_game_enable: false',
                                                          'cloud_game_enable: False'))
            self.assertNotIn('march7_local_game_required', blockers)
            # Quoted "true" is the same string upstream as bare true.
            blockers = self._prepare_with_config(root, row, '',
                                                 replace=('auto_update: true', 'auto_update: "true"'))
            self.assertNotIn('march7_complete_native_first_setup', blockers)
            # A null game path stays unverifiable instead of crashing.
            blockers = self._prepare_with_config(root, row, '',
                                                 replace=("game_path: '" + str(root / 'StarRail.exe') + "' # location",
                                                          'game_path:'))
            self.assertIn('game_path_unverified', blockers)

    def test_uniform_indent_spaced_colon_and_crlf_are_resolved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); row = self.installation(root)
            config = root / 'config.yaml'
            body = config.read_text(encoding='utf-8')
            indented = ''.join('  ' + line + '\n' for line in body.splitlines())
            config.write_text(indented, encoding='utf-8')
            with patch('gachaops.march7.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.march7.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.march7.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                self.assertEqual(march7.prepare(row)['blockers'], [])
            # A space before the colon is the same key upstream.
            config.write_text(body.replace('log_level: INFO', 'log_level : DEBUG'), encoding='utf-8')
            with patch('gachaops.march7.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.march7.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.march7.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                self.assertNotIn('march7_info_logging_required', march7.prepare(row)['blockers'])
            # CRLF endings must not change anything.
            config.write_text(body.replace('\n', '\r\n'), encoding='utf-8')
            with patch('gachaops.march7.elevation_report', return_value={'elevated': True}), \
                 patch('gachaops.march7.desktop_report', return_value={'status': 'observed_available'}), \
                 patch('gachaops.march7.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                self.assertEqual(march7.prepare(row)['blockers'], [])

    def test_unresolvable_needed_entries_are_rejected_with_the_setting_named(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); row = self.installation(root)
            for spelling in ('control_backend: [local]\n', 'control_backend: *anchor\n',
                             'control_backend: &b local\n', 'control_backend: !!str local\n',
                             'control_backend :local\n', 'control_backend:#x\n',
                             'control_backend\n'):
                blockers = self._prepare_with_config(root, row, spelling)
                with self.subTest(spelling=spelling.strip()):
                    self.assertTrue(any('无法识别' in b for b in blockers), blockers)
            # A nested or block-spelled value under game_path (replacing the
            # fixture's own entry, not duplicating it) is equally undecidable.
            config = root / 'config.yaml'
            body = config.read_text(encoding='utf-8')
            for spelling in ('game_path:\n  sub: x', "game_path: |\n  C:\\somewhere"):
                lines = [spelling if line.startswith('game_path:') else line
                         for line in body.splitlines()]
                config.write_text('\n'.join(lines) + '\n', encoding='utf-8')
                with patch('gachaops.march7.elevation_report', return_value={'elevated': True}), \
                     patch('gachaops.march7.desktop_report', return_value={'status': 'observed_available'}), \
                     patch('gachaops.march7.find_matching_processes', return_value={'status': 'observed', 'matches': []}):
                    blockers = march7.prepare(row)['blockers']
                with self.subTest(spelling=spelling.splitlines()[0]):
                    self.assertTrue(any('无法识别' in b for b in blockers), blockers)
            # A needed key inside a multi-line flow belongs to that flow, not
            # to the top level; upstream stores it nested, so nothing blocks.
            blockers = self._prepare_with_config(root, row, 'webhook: [a,\n  control_backend: adb\n]\n')
            self.assertNotIn('march7_local_backend_required', blockers)
            self.assertFalse(any('无法识别' in b for b in blockers), blockers)
            # An unterminated quote leaves the document structure unknown.
            blockers = self._prepare_with_config(root, row, "note: \"never closed\n")
            self.assertTrue(any('无法识别' in b for b in blockers), blockers)

    def test_activity_progress_line_stands_alone_without_a_claim(self):
        body = ('| 开始运行 |\n16:34:30 | INFO | 每日实训活跃度 {score}\n'
                '| 停止运行 |\n--- 完成 ---\n')
        result = march7.evaluate(body.format(score='200/500'), 0)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertEqual(result['daily_training_score'], '200/500')
        self.assertTrue(result['daily_training_incomplete'])
        self.assertIn('每日实训活跃度', result['failed_applications'])
        # Full activity without any claim line is a clean success.
        result = march7.evaluate(body.format(score='500/500'), 0)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['daily_training_score'], '500/500')
        self.assertFalse(result['daily_training_incomplete'])
        # Both lines, in either order: the later evidence wins, a full number
        # completes the training even when an earlier line was partial.
        both = ('| 开始运行 |\n16:34:30 | INFO | 每日实训活跃度 200/500\n'
                '16:35:00 | INFO | 已领取实训奖励，活跃度 500/500\n| 停止运行 |\n--- 完成 ---\n')
        result = march7.evaluate(both, 0)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['daily_training_score'], '500/500')
        reversed_both = ('| 开始运行 |\n16:34:30 | INFO | 已领取实训奖励，活跃度 200/500\n'
                         '16:35:00 | INFO | 每日实训活跃度 500/500\n| 停止运行 |\n--- 完成 ---\n')
        result = march7.evaluate(reversed_both, 0)
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['daily_training_score'], '500/500')

    def test_full_activity_claim_keeps_other_task_failures(self):
        body = ('| 开始运行 |\n16:34:00 | INFO | 任务无法完成: 委托\n'
                '16:34:30 | INFO | 已领取实训奖励，活跃度 500/500\n'
                '| 停止运行 |\n--- 完成 ---\n')
        result = march7.evaluate(body, 0)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertIn('委托', result['failed_applications'])
        # The training's own partial entry is settled by a later full claim,
        # while the unrelated failure stays.
        settling = ('| 开始运行 |\n16:34:00 | INFO | 任务无法完成: 委托\n'
                    '16:34:10 | INFO | 已领取实训奖励，活跃度 200/500\n'
                    '16:34:30 | INFO | 已领取实训奖励，活跃度 500/500\n'
                    '| 停止运行 |\n--- 完成 ---\n')
        result = march7.evaluate(settling, 0)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertEqual(result['failed_applications'], ['委托'])
        self.assertFalse(result['daily_training_incomplete'])
        progress_body = body.replace('已领取实训奖励，活跃度 500/500', '每日实训活跃度 500/500')
        result = march7.evaluate(progress_body, 0)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertIn('委托', result['failed_applications'])



if __name__ == '__main__':
    unittest.main()
