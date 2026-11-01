import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from gachaops.execution_lock import ExecutionLock

from gachaops.recovery import save, read_state
from gachaops.recovery_check import recover_workspace


class RecoveryCheckTests(unittest.TestCase):
    def fixture(self, root):
        chain = root / 'runs/gui-fixture/assistants-fixture'; node_dir = chain / 'node-01'
        node_dir.mkdir(parents=True)
        owned = [dict(pid=101, image=str(root / 'native.exe'), creation_ticks=100, in_owned_job=True)]
        node = dict(id='fixture', run_id='run', status='ownership_or_cleanup_unconfirmed',
                    reason='game_job_inheritance_unconfirmed', cleanup_verified=False,
                    owned_job_cleanup_verified=True, ownership_observations=owned,
                    cwd=str(root), argv=[str(root / 'native.exe')], game_path=str(root / 'game.exe'),
                    observation_targets=[str(root / 'native.exe'), str(root / 'game.exe')],
                    remaining_processes=dict(status='observed', matches=[], uncertain=[]))
        save(node_dir / 'summary.json', node)
        save(chain / 'summary.json', dict(run_id='run', status='stopped_cleanup_unconfirmed',
            assistants=[dict(node, node_summary=str(node_dir / 'summary.json'))]))
        journal = dict(schema_version=1, run_id='run', phase='quarantined', cleanup_verified=False,
            owned_processes=owned, supervisor_pid=99,
            supervisor_identity=dict(pid=99, image=str(root / 'supervisor.exe'), creation_ticks=90),
            chain_summary=str(chain / 'summary.json'))
        (root / 'state').mkdir(); save(root / 'state/execution.json', journal)
        return node_dir / 'summary.json', journal

    def run_recovery(self, root, identities=None, scan=None):
        with patch('gachaops.recovery_check.inspect_process_identity',
                   side_effect=lambda pid: (identities or {}).get(pid)), \
             patch('gachaops.recovery_check.find_matching_processes',
                   return_value=scan or dict(status='observed', matches=[], uncertain=[])):
            return recover_workspace(root)

    def test_restores_only_after_job_proof_and_fresh_checks_preserves_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, original = self.fixture(root); before = path.read_bytes()
            result = self.run_recovery(root)
            self.assertEqual(result['status'], 'recovered')
            self.assertEqual(read_state(root / 'state')['phase'], 'idle')
            self.assertEqual(path.read_bytes(), before)
            evidence = Path(result['evidence'])
            self.assertEqual(json.loads((evidence / 'original-journal.json').read_text()), original)
            self.assertTrue((evidence / 'fresh-inspection.json').exists())

    def test_reused_pid_is_allowed_but_original_identity_and_unknown_supervisor_are_not(self):
        for pid, identity, recovered in [(101, 200, True), (101, 100, False), (99, 90, False)]:
            with self.subTest(pid=pid, ticks=identity), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); self.fixture(root)
                result = self.run_recovery(root, identities={pid: dict(pid=pid, image=str(root / 'other.exe'),
                                                                      creation_ticks=identity)})
                self.assertEqual(result['status'], 'recovered' if recovered else 'blocked')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, journal = self.fixture(root); journal.pop('supervisor_identity')
            save(root / 'state/execution.json', journal)
            self.assertEqual(self.run_recovery(root, identities={99: dict(pid=99, image='other',
                creation_ticks=200)})['status'], 'blocked')

    def test_missing_proof_changed_identity_or_missing_targets_keeps_quarantine(self):
        for key, value in [('owned_job_cleanup_verified', False), ('run_id', 'other'),
                           ('observation_targets', []), ('game_path', None),
                           ('remaining_processes', dict(status='uncertain', matches=[], uncertain=[{}]))]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); path, journal = self.fixture(root)
                node = json.loads(path.read_text()); node[key] = value; save(path, node)
                self.assertEqual(self.run_recovery(root)['status'], 'blocked')
                self.assertEqual(read_state(root / 'state'), journal)

    def test_live_matches_uncertainty_and_access_denied_do_not_write_idle(self):
        for scan in [dict(status='observed', matches=[dict(pid=222, image='game.exe')], uncertain=[]),
                     dict(status='uncertain', matches=[], uncertain=[dict(pid=222, error=5)])]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); _, journal = self.fixture(root)
                self.assertEqual(self.run_recovery(root, scan=scan)['status'], 'blocked')
                self.assertEqual(read_state(root / 'state'), journal)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, journal = self.fixture(root)
            with patch('gachaops.recovery_check.inspect_process_identity', side_effect=PermissionError('denied')):
                self.assertEqual(recover_workspace(root)['status'], 'blocked')
            self.assertEqual(read_state(root / 'state'), journal)

    def test_no_quarantine_is_noop_and_running_journal_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(recover_workspace(root)['status'], 'already_ready')
            _, journal = self.fixture(root); journal['phase'] = 'running'; save(root / 'state/execution.json', journal)
            self.assertEqual(self.run_recovery(root)['status'], 'blocked')

    def test_lock_busy_external_evidence_and_state_drift_do_not_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, journal = self.fixture(root)
            with ExecutionLock():
                self.assertEqual(self.run_recovery(root)['status'], 'blocked')
            journal['chain_summary'] = str(root.parent / 'outside/summary.json')
            save(root / 'state/execution.json', journal)
            self.assertEqual(self.run_recovery(root)['status'], 'blocked')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.fixture(root)
            def changed(pid):
                data = read_state(root / 'state'); data['run_id'] = 'new-run'; save(root / 'state/execution.json', data)
                return None
            with patch('gachaops.recovery_check.inspect_process_identity', side_effect=changed), \
                 patch('gachaops.recovery_check.find_matching_processes',
                       return_value=dict(status='observed', matches=[], uncertain=[])):
                self.assertEqual(recover_workspace(root)['status'], 'blocked')
            self.assertEqual(read_state(root / 'state')['run_id'], 'new-run')

    def test_audit_failure_before_commit_keeps_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); _, journal = self.fixture(root)
            def failing(path, data):
                if Path(path).name == 'planned-journal.json':
                    raise OSError('audit disk full')
                save(path, data)
            with patch('gachaops.recovery_check.save', side_effect=failing):
                self.assertEqual(self.run_recovery(root)['status'], 'blocked')
            self.assertEqual(read_state(root / 'state'), journal)

    def test_recovery_cli_outputs_report_without_loading_assistant_configuration(self):
        from gachaops import __main__ as cli
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(cli, 'workspace_root', return_value=root), \
                 patch.object(cli, 'state_directory', return_value=root / 'state'), \
                 patch.object(cli, 'load_assistants', side_effect=AssertionError('no native preset load')), \
                 patch('sys.argv', ['gachaops', 'recover-assistants', '--output', str(root / 'output')]):
                self.assertEqual(cli.main(), 0)
            self.assertEqual(json.loads((root / 'output/recovery.json').read_text())['status'], 'already_ready')

    def test_historical_residual_can_be_recovered_after_fresh_identity_and_path_checks(self):
        for live in (False, True):
            with self.subTest(live=live), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); path, journal = self.fixture(root)
                residual = dict(pid=222, image=str(root / 'renamed-game.exe'), creation_ticks=220)
                node = json.loads(path.read_text())
                node['remaining_processes']['matches'] = [residual]
                node['observation_targets'].append(None)
                save(path, node); before = path.read_bytes()
                with patch('gachaops.recovery_check.inspect_process_identity',
                           side_effect=lambda pid: residual if live and pid == 222 else None) as inspect, \
                     patch('gachaops.recovery_check.find_matching_processes',
                           return_value=dict(status='observed', matches=[], uncertain=[])) as scan:
                    result = recover_workspace(root)
                self.assertEqual(result['status'], 'blocked' if live else 'recovered')
                self.assertIn(222, [call.args[0] for call in inspect.call_args_list])
                self.assertEqual(path.read_bytes(), before)
                if not live:
                    self.assertIn(residual['image'], scan.call_args.args[0])
                    self.assertNotIn(None, scan.call_args.args[0])
                else:
                    self.assertEqual(read_state(root / 'state'), journal)

    def test_malformed_historical_residuals_and_invalid_targets_stay_blocked(self):
        for change in ({'remaining_processes': dict(status='observed', matches=[{}], uncertain=[])},
                       {'remaining_processes': dict(status='observed', matches=[], uncertain=[{}])},
                       {'observation_targets': ['relative.exe']},
                       {'observation_targets': [None]},
                       {'observation_targets': 'invalid'},
                       {'borrowed_game': [{}]}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); path, journal = self.fixture(root)
                node = json.loads(path.read_text()); node.update(change); save(path, node)
                self.assertEqual(self.run_recovery(root)['status'], 'blocked')
                self.assertEqual(read_state(root / 'state'), journal)

    def test_exited_system_helper_does_not_block_on_unrelated_same_path_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, journal = self.fixture(root)
            helper = dict(pid=333, image=str(root.parent / 'system/conhost.exe'),
                          creation_ticks=330, in_owned_job=True)
            journal['owned_processes'].append(helper)
            save(root / 'state/execution.json', journal)
            node = json.loads(path.read_text()); node['ownership_observations'].append(helper)
            save(path, node)
            def scan(targets):
                return dict(status='observed', uncertain=[], matches=(
                    [dict(pid=444, image=helper['image'], creation_ticks=440)]
                    if helper['image'] in targets else []))
            with patch('gachaops.recovery_check.inspect_process_identity', return_value=None) as inspect, \
                 patch('gachaops.recovery_check.find_matching_processes', side_effect=scan):
                self.assertEqual(recover_workspace(root)['status'], 'recovered')
            self.assertIn(333, [call.args[0] for call in inspect.call_args_list])
