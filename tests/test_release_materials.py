import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prepare_release_materials.py'


class ReleaseMaterialsTests(unittest.TestCase):
    def fixture(self, root):
        source = root / 'source'; source.mkdir()
        cache = root / 'cache'; cache.mkdir()
        required = {}
        for name in ('LICENSE', 'PACKAGE_THIRD_PARTY_NOTICES.md', 'PACKAGE_MICROSOFT_CRT_TERMS.md', 'SECURITY.md', 'docs/QUICKSTART.md', 'docs/SUPPORT_MATRIX.md', 'docs/USAGE_NOTICE.md', 'docs/DATA_DIRECTORIES.md', 'docs/ADDING_ADAPTER.md', 'docs/VALIDATION.md', 'docs/THIRD_PARTY_ASSISTANTS.md', 'PACKAGE_README.md'):
            p = source / name; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'synthetic required notice')
            required[name] = hashlib.sha256(p.read_bytes()).hexdigest()
        relative = 'docs/release/source-delivery/archives/qtbase.tar.gz'
        data = b'synthetic source archive copied as opaque verified bytes'
        (cache / 'qtbase.tar.gz').write_bytes(data)
        row = {'source': relative, 'destination': 'SOURCE_DELIVERY/archives/qtbase.tar.gz',
               'sha256': hashlib.sha256(data).hexdigest()}
        proposal = {'schema_version': 1, 'approved': False, 'required_sha256': required, 'materials': [row]}
        proposal['required_destinations'] = {name: name for name in required}
        proposal['required_destinations']['PACKAGE_README.md'] = 'README.md'
        proposal['required_destinations']['PACKAGE_THIRD_PARTY_NOTICES.md'] = 'THIRD_PARTY_NOTICES.md'
        proposal['required_destinations']['PACKAGE_MICROSOFT_CRT_TERMS.md'] = 'MICROSOFT_CRT_TERMS.md'
        release = source / 'docs/release'; release.mkdir(parents=True, exist_ok=True)
        (release / 'DISTRIBUTION_DELIVERY_PROPOSAL.json').write_text(json.dumps(proposal), encoding='utf-8')
        actual = {'schema_version': 1, 'approved': False, 'status': 'blocked_pending_distribution_material_review'}
        (release / 'DISTRIBUTION_APPROVAL.json').write_text(json.dumps(actual), encoding='utf-8')
        manifest = {'schema_version': 1, 'archives': [{'source': relative, 'file': 'qtbase.tar.gz',
                                                    'sha256': row['sha256'], 'bytes': len(data)}]}
        (release / 'source-delivery').mkdir(exist_ok=True)
        (release / 'source-delivery/SOURCE_BUNDLE_MANIFEST.json').write_text(json.dumps(manifest), encoding='utf-8')
        return source, cache, row, actual

    def run_prepare(self, source, cache, output):
        return subprocess.run([sys.executable, str(SCRIPT), '--source', str(source), '--archive-dir', str(cache),
                               '--output', str(output), '--proposal'], capture_output=True, text=True, encoding='utf-8')

    def test_hydrates_exact_bytes_and_preserves_real_unapproved_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, cache, row, actual = self.fixture(root)
            output = root / 'prepared'
            result = self.run_prepare(source, cache, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((output / row['source']).read_bytes(), (cache / 'qtbase.tar.gz').read_bytes())
            self.assertEqual(json.loads((output / 'docs/release/DISTRIBUTION_APPROVAL.json').read_text()), actual)
            self.assertFalse(json.loads(result.stdout)['distribution_approved'])

    def test_missing_archive_blocks_without_partial_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, cache, _, _ = self.fixture(root)
            (cache / 'qtbase.tar.gz').unlink()
            result = self.run_prepare(source, cache, root / 'prepared')
            self.assertEqual(result.returncode, 1)
            self.assertFalse((root / 'prepared').exists())

    def test_wrong_archive_hash_blocks_without_partial_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, cache, _, _ = self.fixture(root)
            (cache / 'qtbase.tar.gz').write_bytes(b'wrong source input')
            result = self.run_prepare(source, cache, root / 'prepared')
            self.assertEqual(result.returncode, 1)
            self.assertFalse((root / 'prepared').exists())

    def test_changed_required_notice_does_not_fall_back_to_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, cache, _, _ = self.fixture(root)
            (source / 'LICENSE').write_bytes(b'changed required notice')
            result = self.run_prepare(source, cache, root / 'prepared')
            self.assertEqual(result.returncode, 1)
            self.assertFalse((root / 'prepared').exists())

    def test_material_cannot_read_outside_allowed_source_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, cache, row, _ = self.fixture(root)
            path = source / 'docs/release/DISTRIBUTION_DELIVERY_PROPOSAL.json'
            data = json.loads(path.read_text())
            data['materials'][0]['source'] = '../outside.txt'
            (root / 'outside.txt').write_bytes(b'not a release material')
            path.write_text(json.dumps(data), encoding='utf-8')
            result = self.run_prepare(source, cache, root / 'prepared')
            self.assertEqual(result.returncode, 1)
            self.assertFalse((root / 'prepared').exists())

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, cache, _, _ = self.fixture(root)
            output = root / 'prepared'; output.mkdir()
            (output / 'keep.txt').write_bytes(b'keep original')
            result = self.run_prepare(source, cache, output)
            self.assertEqual(result.returncode, 1)
            self.assertEqual((output / 'keep.txt').read_bytes(), b'keep original')

    def test_pending_proposal_does_not_inherit_historical_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, cache, _, actual = self.fixture(root)
            actual['approved'] = True
            path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
            path.write_text(json.dumps(actual), encoding='utf-8')
            before = path.read_bytes()
            result = self.run_prepare(source, cache, root / 'prepared')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(json.loads(result.stdout)['distribution_approved'])
            self.assertEqual((root / 'prepared/docs/release/DISTRIBUTION_APPROVAL.json').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
