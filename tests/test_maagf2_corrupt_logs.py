import json
import tempfile
import time
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch

from gachaops import maagf2
from gachaops.result_details import details_for
import test_maagf2 as fixtures

event, frame = fixtures.event, fixtures.frame


class CorruptLogTests(unittest.TestCase):
    def run_sample(self, bad_kind='agent', failed=False, missing=False, duplicate=False, remove_bad=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'debug').mkdir()
            (root / 'logs').mkdir()
            native = root / 'debug/maafw.log'
            native.write_bytes(b'')
            _, cursor = maagf2.bind_execution({'cwd': str(root), 'instance_id': 'default'}, root)
            frames, gui = fixtures.MaaGF2Tests().logs()
            stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            original_stamp = '2026-10-02 14:00:00.000'
            if failed:
                frames[-1] = event('Failed', entry='CommunityDailyHelper')
            if missing:
                frames.pop()
            if duplicate:
                frames.append(frames[-1])
            prefix = frame(77, '[msg=Node.PipelineNode.Succeeded] [details={"text":"').encode()
            bad = prefix + b'\xe7' + frame(42,
                '[AgentClient.cpp][AgentClient::handle_event_response] handle event response').replace('[INF]', '[TRC]').replace('[Logger] ', '').encode() + b'\n'
            if bad_kind == 'root':
                bad = bad.replace(b'Px77', b'Px42')
            elif bad_kind == 'unknown':
                bad = bad.replace(b'Px77', b'Px99')
            elif bad_kind == 'critical':
                bad = bad.replace(b'Node.PipelineNode.Succeeded', b'Tasker.Task.Succeeded')
            elif bad_kind == 'hidden_critical':
                bad += b'\xe7' + event('Failed').encode() + b'\n'
            elif bad_kind == 'fragment':
                bad = b'\x82unidentified fragment\n'
            elif bad_kind == 'stale':
                bad = bad.replace(original_stamp.encode(), b'2000-01-01 00:00:00.000')
            bad = bad.replace(original_stamp.encode(), stamp.encode())
            data = ('\n'.join(frames) + '\n').replace(original_stamp, stamp).encode() + bad
            native.write_bytes(data)
            (root / 'logs/log-test.log').write_text(
                ('\n'.join(gui) + '\n').replace(original_stamp, stamp), encoding='utf-8')
            owned = [{'pid': 42, 'in_owned_job': True}, {'pid': 77, 'in_owned_job': True,
                'image': str(root / 'python/python.exe'),
                'creation_ticks': int((time.time() + 11644473600 - 10) * 1e7)}]
            result = maagf2.observe(cursor, 0, 42, owned)
            if remove_bad:
                native.write_bytes(data[:-len(bad)])
                result = maagf2.observe(cursor, 0, 42, owned)
            audit = root / 'native-log-corruption.json'
            saved = json.loads(audit.read_text(encoding='utf-8')) if audit.exists() else None
            # Repeated polls must not duplicate evidence.
            maagf2.observe(cursor, 0, 42, owned)
            if audit.exists():
                self.assertEqual(saved, json.loads(audit.read_text(encoding='utf-8')))
            return result, saved

    def test_known_agent_corruption_keeps_complete_root_result_and_audit(self):
        result, audit = self.run_sample()
        self.assertEqual(result['status'], 'success')
        self.assertEqual(result['log_corruption']['skipped_lines'], 1)
        self.assertEqual(len(audit['lines']), 1)
        self.assertIn('e7', audit['lines'][0]['raw_hex'])
        self.assertTrue(audit['lines'][0]['skipped'])
        text = details_for({'id': 'girls-frontline-2', 'status': result['status'], 'completion': result})['text']
        self.assertIn('日志损坏', text)

    def test_native_failure_stays_partial_failure(self):
        result, _ = self.run_sample(failed=True)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertEqual(result['failed_applications'], ['CommunityDailyHelper'])

    def test_root_unknown_critical_and_unattributed_corruption_block(self):
        for kind in ('root', 'unknown', 'critical', 'hidden_critical', 'fragment', 'stale'):
            with self.subTest(kind=kind):
                result, audit = self.run_sample(kind)
                self.assertEqual(result['status'], 'completion_unconfirmed')
                self.assertIsNotNone(audit)
                self.assertTrue(any(not line['skipped'] for line in audit['lines']))

    def test_missing_and_duplicate_root_terminal_still_block(self):
        for kwargs in ({'missing': True}, {'duplicate': True}):
            result, _ = self.run_sample(**kwargs)
            self.assertEqual(result['status'], 'completion_unconfirmed')

    def test_audit_write_failure_blocks(self):
        real_save = maagf2.save
        def fail_audit(path, data):
            if Path(path).name == 'native-log-corruption.json':
                raise OSError('audit disk error')
            return real_save(path, data)
        with patch('gachaops.maagf2.save', side_effect=fail_audit):
            result, _ = self.run_sample()
        self.assertEqual(result['status'], 'completion_unconfirmed')

    def test_removed_unknown_damage_does_not_clear_prior_uncertainty(self):
        result, _ = self.run_sample('unknown', remove_bad=True)
        self.assertEqual(result['status'], 'completion_unconfirmed')


if __name__ == '__main__':
    unittest.main()
