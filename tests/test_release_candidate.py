import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from tests import test_release_staging


class ReleaseCandidateTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        self.module = importlib.import_module('release_candidate')
        self.addCleanup(sys.path.pop, 0)

    def fixture(self, root):
        source, app = test_release_staging.ReleaseStagingTests().fixture(root)
        manifest = json.loads((app / 'build-manifest.json').read_text())
        manifest['version'] = '0.1.0'
        (app / 'build-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        cache = root / 'cache'; cache.mkdir()
        archive = cache / 'source.zip'; archive.write_bytes(b'synthetic matching source archive')
        rel = 'docs/release/source-delivery/archives/source.zip'
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        bundle = source / 'docs/release/source-delivery/SOURCE_BUNDLE_MANIFEST.json'
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle_data = json.loads(bundle.read_text())
        bundle_data['archives'].append({'source': rel, 'file': 'source.zip', 'bytes': archive.stat().st_size, 'sha256': digest})
        bundle.write_text(json.dumps(bundle_data), encoding='utf-8')
        path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
        approval = json.loads(path.read_text())
        approval['materials'] = [r for r in approval['materials'] if r['source'] != bundle.relative_to(source).as_posix()]
        approval['materials'] += [
            {'source': rel, 'destination': 'SOURCE_DELIVERY/archives/source.zip', 'sha256': digest},
            {'source': bundle.relative_to(source).as_posix(), 'destination': 'SOURCE_DELIVERY/SOURCE_BUNDLE_MANIFEST.json',
             'sha256': hashlib.sha256(bundle.read_bytes()).hexdigest()}]
        path.write_text(json.dumps(approval), encoding='utf-8')
        review = self.module.capture(app)
        self.assertFalse(review['approved'])
        review['approved'] = True  # Synthetic maintainer decision; never copied to production.
        review['sidecar_sha256'] = {
            'distribution_approval': hashlib.sha256(path.read_bytes()).hexdigest(),
            'source_bundle_manifest': hashlib.sha256(bundle.read_bytes()).hexdigest(),
            'binary_dispositions': None}
        return source, app, cache, review

    def test_sidecar_root_alias_normalizes_without_accepting_changed_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve(); source, app, cache, review = self.fixture(root)
            alias = source / '..' / source.name
            self.module.verify_sidecars(alias, review)
            path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
            path.write_bytes(path.read_bytes() + b' ')
            with self.assertRaisesRegex(ValueError, 'review sidecar changed'):
                self.module.verify_sidecars(alias, review)

    def test_reviewed_fixture_prepares_stages_and_verifies_complete_zip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app, cache, review = self.fixture(root)
            result = self.module.finalize(source, app, review, root / 'result', [cache])
            self.assertEqual(result['status'], 'candidate_verified')
            self.assertFalse(result['public_release_approved'])
            package = root / 'result/GachaOps-candidate.zip'
            with zipfile.ZipFile(package) as z:
                self.assertIsNone(z.testzip())
                self.assertIn('docs/USAGE_NOTICE.md', z.namelist())
                self.assertIn('SOURCE_DELIVERY/archives/source.zip', z.namelist())
                hashes = json.loads(z.read('checksums.json'))
                self.assertEqual(set(z.namelist()), set(hashes) | {'checksums.json'})
                for name, digest in hashes.items():
                    self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(), digest)
            summary = root / 'result/release-summary.json'
            self.assertEqual(self.module.verify_package(package, json.loads(summary.read_text()))['status'], 'candidate_verified')
            for key, changed in [('version', '9.9.9'), ('candidate_code_commit', 'b' * 40), ('zip_sha256', '0' * 64)]:
                bad = dict(result, **{key: changed})
                with self.subTest(summary_field=key), self.assertRaises(ValueError):
                    self.module.verify_package(package, bad)

    def test_unapproved_or_drifted_input_never_produces_success_zip(self):
        for case in ('unapproved', 'new_file', 'removed_file', 'clean_runtime', 'exe', 'commit',
                     'material_approval', 'bundle', 'missing_archive', 'wrong_archive', 'unlisted_archive'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); source, app, cache, review = self.fixture(root)
                if case == 'unapproved': review['approved'] = False
                elif case == 'new_file': (app / '_internal/new.dll').write_bytes(b'new safe file')
                elif case == 'removed_file': (app / '_internal/python.dll').unlink()
                elif case == 'clean_runtime': (app / '_internal/python.dll').write_bytes(b'changed safe runtime')
                elif case == 'exe': (app / 'GachaOps.exe').write_bytes(b'changed executable')
                elif case == 'commit':
                    p = app / 'build-manifest.json'; d = json.loads(p.read_text()); d['commit'] = 'b' * 40
                    p.write_text(json.dumps(d), encoding='utf-8')
                elif case == 'material_approval':
                    p = source / 'docs/release/DISTRIBUTION_APPROVAL.json'; p.write_bytes(p.read_bytes() + b' ')
                elif case == 'bundle':
                    p = source / 'docs/release/source-delivery/SOURCE_BUNDLE_MANIFEST.json'; p.write_bytes(p.read_bytes() + b' ')
                elif case == 'missing_archive': (cache / 'source.zip').unlink()
                elif case == 'wrong_archive': (cache / 'source.zip').write_bytes(b'wrong source bytes')
                elif case == 'unlisted_archive':
                    p = source / 'docs/release/DISTRIBUTION_APPROVAL.json'; d = json.loads(p.read_text())
                    d['materials'] = [r for r in d['materials'] if not r['source'].endswith('/source.zip')]
                    p.write_text(json.dumps(d), encoding='utf-8')
                    review['sidecar_sha256']['distribution_approval'] = hashlib.sha256(p.read_bytes()).hexdigest()
                with self.assertRaises(ValueError):
                    self.module.finalize(source, app, review, root / 'result', [cache])
                self.assertFalse((root / 'result/GachaOps-candidate.zip').exists())

    def test_zip_verifier_rejects_extra_duplicate_or_changed_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for case in ('extra', 'duplicate', 'changed'):
                p = root / (case + '.zip')
                with zipfile.ZipFile(p, 'w') as z:
                    z.writestr('file.txt', b'changed' if case == 'changed' else b'original')
                    if case == 'extra': z.writestr('new.txt', b'new')
                    if case == 'duplicate':
                        with self.assertWarns(UserWarning):
                            z.writestr('file.txt', b'original')
                with self.assertRaises(ValueError):
                    self.module.verify_zip(p, {'file.txt': hashlib.sha256(b'original').hexdigest()})

    def test_drift_after_staging_cannot_become_a_new_packaging_baseline(self):
        for case in ('extra_runtime', 'changed_notice', 'changed_checksum'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); source, app, cache, review = self.fixture(root)
                original = self.module.stage_release
                def drift(*args, **kwargs):
                    hashes = original(*args, **kwargs)
                    staged = Path(args[2])
                    if case == 'extra_runtime': (staged / '_internal/new.dll').write_bytes(b'new unreviewed file')
                    elif case == 'changed_notice': (staged / 'LICENSE').write_bytes(b'changed unreviewed notice')
                    else: (staged / 'checksums.json').write_bytes(b'{}')
                    return hashes
                with patch.object(self.module, 'stage_release', side_effect=drift):
                    with self.assertRaises(ValueError):
                        self.module.finalize(source, app, review, root / 'result', [cache])
                self.assertFalse((root / 'result/GachaOps-candidate.zip').exists())
