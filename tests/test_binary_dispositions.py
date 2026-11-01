import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_release_binary.py'


class BinaryDispositionTests(unittest.TestCase):
    def fixture(self, root):
        app = root / 'app'
        (app / '_internal').mkdir(parents=True)
        exe = b'synthetic executable'
        (app / 'GachaOps.exe').write_bytes(exe)
        manifest = {'schema_version': 1, 'dirty': False, 'commit': 'a' * 40,
                    'executable_sha256': hashlib.sha256(exe).hexdigest()}
        (app / 'build-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        data = ('C:' + '\\Users\\' + 'synthetic-builder\\library').encode()
        runtime = app / '_internal/runtime.dll'
        runtime.write_bytes(data)
        policy = {'schema_version': 1, 'approved': True, 'status': 'approved_exact_binary_findings',
                  'approval': {'by': 'maintainer', 'date': '2026-10-25', 'scope': 'binary_privacy_only'},
                  'candidate_code_commit': manifest['commit'],
                  'candidate_executable_sha256': manifest['executable_sha256'],
                  'findings': [{'path': '_internal/runtime.dll', 'rule': 'PERSONAL_PATH',
                                'sha256': hashlib.sha256(data).hexdigest(),
                                'classification': 'dependency_build_path'}]}
        return app, runtime, policy

    def audit(self, root, app, policy=None, text=None):
        args = [sys.executable, str(SCRIPT), '--app-dir', str(app)]
        if policy is not None or text is not None:
            path = root / 'policy.json'
            path.write_text(text if text is not None else json.dumps(policy), encoding='utf-8')
            args += ['--dispositions', str(path)]
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8')
        self.assertNotIn('synthetic-builder', result.stdout + result.stderr)
        self.assertNotIn(str(root), result.stdout + result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_exact_match_is_accepted_but_raw_findings_are_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app, _, policy = self.fixture(root)
            code, report = self.audit(root, app, policy)
            self.assertEqual(code, 0)
            self.assertEqual(report['status'], 'passed')
            self.assertEqual(len(report['raw_findings']), 1)
            self.assertEqual(len(report['accepted_findings']), 1)
            self.assertEqual(report['unresolved_findings'], [])
            self.assertFalse(report['distribution_approved'])

    def test_no_policy_keeps_raw_scanner_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app, _, _ = self.fixture(root)
            code, report = self.audit(root, app)
            self.assertEqual(code, 1)
            self.assertEqual(len(report['unresolved_findings']), 1)
            self.assertEqual(report['accepted_findings'], [])

    def test_changed_bytes_new_path_and_missing_file_block(self):
        for change in ['bytes', 'path', 'missing']:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); app, runtime, policy = self.fixture(root)
                if change == 'bytes': runtime.write_bytes(runtime.read_bytes() + b'changed')
                elif change == 'path': runtime.rename(runtime.with_name('other.dll'))
                else: runtime.unlink()
                code, report = self.audit(root, app, policy)
                self.assertNotEqual(code, 0)
                self.assertEqual(report['status'], 'blocked')

    def test_same_file_new_rule_cannot_inherit_path_acceptance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app, runtime, policy = self.fixture(root)
            runtime.write_bytes(runtime.read_bytes() + b'gh' + b'p_' + b'x' * 40)
            policy['findings'][0]['sha256'] = hashlib.sha256(runtime.read_bytes()).hexdigest()
            code, report = self.audit(root, app, policy)
            self.assertEqual(code, 1)
            self.assertEqual([r['rule'] for r in report['unresolved_findings']], ['CREDENTIAL'])

    def test_new_file_is_blocked_and_does_not_disappear_from_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app, runtime, policy = self.fixture(root)
            (app / '_internal/new.dll').write_bytes(runtime.read_bytes())
            code, report = self.audit(root, app, policy)
            self.assertEqual(code, 1)
            self.assertEqual(len(report['raw_findings']), 2)
            self.assertEqual(report['unresolved_findings'][0]['path'], '_internal/new.dll')

    def test_policy_cannot_cover_source_private_files_or_unsafe_paths(self):
        paths = ['../runtime.dll', '/runtime.dll', 'C:/runtime.dll', '_internal/../runtime.dll',
                 '_internal//runtime.dll', '_internal\\runtime.dll', '_internal/RUNTIME.dll',
                 '_internal/runtime.dll:stream', '_internal/code.py', '_internal/local.private.json',
                 'docs/runtime.dll', '_internal/*']
        for path in paths:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); app, _, policy = self.fixture(root)
                policy['findings'][0]['path'] = path
                code, report = self.audit(root, app, policy)
                self.assertNotEqual(code, 0)
                self.assertEqual(report['status'], 'blocked')

    def test_unapproved_duplicate_malformed_or_wrong_rule_policy_blocks(self):
        for case in ['unapproved', 'duplicate_entry', 'duplicate_key', 'bad_json', 'deep_json', 'rule', 'sha', 'schema', 'classification', 'approval']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); app, _, policy = self.fixture(root)
                text = None
                if case == 'unapproved': policy['approved'] = False
                elif case == 'duplicate_entry': policy['findings'] *= 2
                elif case == 'duplicate_key': text = '{"approved":false,"approved":true}'
                elif case == 'bad_json': text = '{'
                elif case == 'deep_json': text = '[' * 5000 + '0' + ']' * 5000
                elif case == 'rule': policy['findings'][0]['rule'] = 'PRIVATE_FILE'
                elif case == 'sha': policy['findings'][0]['sha256'] = '0' * 63
                elif case == 'schema': policy['schema_version'] = True
                elif case == 'classification': policy['findings'][0]['classification'] = 'anything'
                elif case == 'approval': policy['approval']['scope'] = 'all_publication'
                code, report = self.audit(root, app, policy, text)
                self.assertNotEqual(code, 0)
                self.assertEqual(report['status'], 'blocked')

    def test_candidate_executable_manifest_commit_and_dirty_are_bound(self):
        for case in ['exe', 'commit', 'dirty', 'manifest_hash', 'missing_manifest']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); app, _, policy = self.fixture(root)
                path = app / 'build-manifest.json'
                manifest = json.loads(path.read_text())
                if case == 'exe': (app / 'GachaOps.exe').write_bytes(b'changed executable')
                elif case == 'commit': manifest['commit'] = 'b' * 40
                elif case == 'dirty': manifest['dirty'] = True
                elif case == 'manifest_hash': manifest['executable_sha256'] = '0' * 64
                if case == 'missing_manifest': path.unlink()
                else: path.write_text(json.dumps(manifest), encoding='utf-8')
                code, report = self.audit(root, app, policy)
                self.assertNotEqual(code, 0)
                self.assertEqual(report['status'], 'blocked')

    def test_stale_rule_and_private_document_never_pass(self):
        for case in ['stale', 'document', 'private_file']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); app, runtime, policy = self.fixture(root)
                if case == 'stale':
                    runtime.write_bytes(b'now safe')
                    policy['findings'][0]['sha256'] = hashlib.sha256(runtime.read_bytes()).hexdigest()
                elif case == 'document': (app / 'README.md').write_bytes(runtime.read_bytes())
                else: (app / '_internal/gachaops-workspace.json').write_text('{}', encoding='utf-8')
                code, report = self.audit(root, app, policy)
                self.assertNotEqual(code, 0)
                self.assertEqual(report['status'], 'blocked')

    def test_empty_candidate_does_not_pass_without_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app = root / 'empty'; app.mkdir()
            code, report = self.audit(root, app)
            self.assertNotEqual(code, 0)
            self.assertEqual(report['status'], 'blocked')

    def test_report_cannot_overwrite_candidate_or_existing_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app, runtime, _ = self.fixture(root)
            original = runtime.read_bytes()
            for report_path in [runtime, root / 'existing.json']:
                if report_path != runtime: report_path.write_text('preserve', encoding='utf-8')
                result = subprocess.run([sys.executable, str(SCRIPT), '--app-dir', str(app),
                    '--report', str(report_path)], capture_output=True, text=True, encoding='utf-8')
                self.assertNotEqual(result.returncode, 0)
            self.assertEqual(runtime.read_bytes(), original)
            self.assertEqual((root / 'existing.json').read_text(), 'preserve')

    def test_reparse_attribute_blocks_without_python312_junction_api(self):
        import importlib.util
        import stat
        from types import SimpleNamespace
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app, runtime, policy = self.fixture(root)
            sys.path.insert(0, str(SCRIPT.parent))
            try:
                spec = importlib.util.spec_from_file_location('binary_reparse_compat', SCRIPT)
                module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                original = Path.lstat

                def reparse_stat(path, *args, **kwargs):
                    if path == runtime:
                        return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
                    return original(path, *args, **kwargs)

                with mock.patch.object(Path, 'is_junction', create=True):
                    delattr(Path, 'is_junction')  # Emulate the supported Python 3.11 API.
                    with mock.patch.object(Path, 'lstat', new=reparse_stat):
                        report = module.audit(app, policy)
                self.assertEqual(report['status'], 'blocked')
                self.assertIn('SCAN_FAILED', report['errors'])
            finally:
                sys.path.pop(0)
