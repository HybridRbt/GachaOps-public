import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from gachaops.result_details import details_for


class ResultDetailsTests(unittest.TestCase):
    def test_repeat_details_preserve_failures_and_select_latest_node(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chain = root / 'summary.json'; chain.write_text(json.dumps({'run_id': 'same-run'}))
            attempts = []
            for number, status in [(1, 'partial_failure'), (2, 'success')]:
                node = root / f'node-{number:02d}'; node.mkdir()
                result = dict(id='endfield', run_id='same-run', status=status,
                              completion={'failed_applications': ['第一次失败的任务']} if number == 1 else {})
                (node / 'summary.json').write_text(json.dumps(result))
                attempts.append(dict(result, round=number, node_summary=str(node / 'summary.json')))
            latest = dict(attempts[-1], attempts=attempts)
            details = details_for(latest, chain)
            self.assertEqual(details['source'], root / 'node-02/summary.json')
            self.assertIn('第 1 轮', details['text'])
            self.assertIn('第 2 轮', details['text'])
            self.assertIn('第一次失败的任务', details['text'])
            latest['node_summary'] = str(root.parent / 'outside/summary.json')
            self.assertIsNone(details_for(latest, chain)['source'])

    def test_unknown_failures_offer_manual_checks_not_update_diagnosis(self):
        for result in [
            {'status': 'partial_failure', 'completion': {'failed_applications': ['任务A']}},
            {'status': 'ownership_or_cleanup_unconfirmed', 'reason': 'game_job_inheritance_unconfirmed',
             'completion': {'status': 'completion_unconfirmed'}},
            {'status': 'execution_error', 'error': 'previous execution has unverified cleanup',
             'cause_unknown': True},
        ]:
            with self.subTest(result=result):
                text = details_for(result)['text']
                self.assertIn('助手更新后，可能需要手动打开一次并确认更新提示', text)
                self.assertIn('游戏可能正在更新', text)
                self.assertIn('可能原因，并非已确认的诊断', text)
                self.assertIn('清理未确认时，仍需先核对并解除隔离', text)

    def test_known_errors_and_success_do_not_offer_speculative_checks(self):
        for result in [
            {'status': 'partial_failure', 'completion': {'failed_applications': ['任务A'],
             'errors': ['账号登录失败：密码错误']}},
            {'status': 'execution_error', 'error': 'configuration file missing'},
            {'status': 'success', 'completion': {}},
        ]:
            with self.subTest(result=result):
                text = details_for(result)['text']
                self.assertNotIn('助手更新后，可能需要', text)
        self.assertIn('密码错误', details_for({'status': 'failed', 'completion': {
            'errors': ['账号登录失败：密码错误']}})['text'])

    def test_native_task_names_unknown_cause_and_plain_text(self):
        result = {'id': 'endfield', 'status': 'partial_failure',
                  'completion': {'failed_applications': ['AutoCollectSchedule', '<b>任务</b>']}}
        text = details_for(result)['text']
        self.assertIn('AutoCollectSchedule', text)
        self.assertIn('<b>任务</b>', text)
        self.assertIn('具体原因未确认', text)
        self.assertNotIn('资源不足', text)
        self.assertIsNone(details_for(result)['source'])

    def test_old_report_enrichment_is_scoped_verified_and_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); node = root / 'node-06'; node.mkdir()
            chain = root / 'summary.json'; chain.write_text(json.dumps({'run_id': 'round'}))
            output = node / 'march7-output.log'
            output.write_text('| 开始运行 |\n1 | INFO | 任务无法完成: 合成\n'
                              '2 | INFO | 每日实训未完成\n| 停止运行 |\n--- 完成 ---\n', encoding='utf-8')
            launch = {'token': 'token', 'argv_candidate': ['native.exe', 'main']}
            (node / 'march7-launch.json').write_text(json.dumps(launch))
            receipt = {'schema_version': 1, 'token': 'token', 'exit_code': 0,
                       'native_argv': ['native.exe', 'main'], 'pid': 1, 'creation_ticks': 10,
                       'native_identity': {'pid': 2, 'creation_ticks': 20, 'image': 'native.exe'},
                       'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest()}
            (node / 'march7-result.json').write_text(json.dumps(receipt))
            report = {'id': 'honkai-star-rail', 'run_id': 'round', 'exit_code': 0, 'root_pid': 1,
                      'ownership_observations': [{'pid': 1, 'creation_ticks': 10, 'in_owned_job': True},
                                                 {'pid': 2, 'creation_ticks': 20, 'in_owned_job': True}]}
            path = node / 'summary.json'; path.write_text(json.dumps(report))
            before = path.read_bytes()
            result = {'id': 'honkai-star-rail', 'status': 'partial_failure', 'completion': {}}
            details = details_for(result, chain)
            self.assertIn('合成', details['text']); self.assertEqual(details['source'], output)
            self.assertEqual(path.read_bytes(), before)
            output.write_text('tampered')
            self.assertNotIn('未完成任务：', details_for(result, chain)['text'])
            self.assertIn('无法核验', details_for(result, chain)['text'])
            report['run_id'] = 'different'; path.write_text(json.dumps(report))
            self.assertIsNone(details_for(result, chain)['source'])
