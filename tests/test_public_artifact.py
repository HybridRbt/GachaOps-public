import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_public_artifact.py'


class PublicArtifactTests(unittest.TestCase):
    def scan(self, root):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(root)],
                              capture_output=True, text=True, encoding='utf-8')

    def test_safe_template_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'example.json').write_text(json.dumps({'installation_root': None}), encoding='utf-8')
            result = self.scan(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['findings'], [])

    def test_private_paths_chat_binding_secret_and_nested_runtime_are_rejected_without_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private = 'C:' + '\\Users\\' + 'synthetic-owner\\game'
            chat = 'chatgpt-' + 'conversation://synthetic-conversation'
            for name, content in [('paths.md', private), ('chat.md', chat),
                                  ('gachaops-workspace.json', '{}'), ('secret.json', '{}'),
                                  ('nested/state/execution.json', '{}')]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding='utf-8')
            result = self.scan(root)
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertEqual(len({r['path'] for r in report['findings']}), 5)
            self.assertNotIn('synthetic-owner', result.stdout + result.stderr)
            self.assertNotIn('synthetic-conversation', result.stdout + result.stderr)

    def test_missing_root_and_external_symlink_fail_closed(self):
        result = self.scan(Path(tempfile.gettempdir()) / 'nonexistent-gachaops-artifact')
        self.assertNotEqual(result.returncode, 0)

    def test_export_keeps_both_crt_layouts_without_internal_determination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'; root.mkdir()
            expected = {'MICROSOFT_CRT_TERMS.md': b'source-layout CRT terms\r\n',
                        'PACKAGE_MICROSOFT_CRT_TERMS.md': b'package-layout CRT terms\r\n',
                        'PACKAGE_THIRD_PARTY_NOTICES.md': b'package-specific notice\r\n'}
            for name, data in expected.items(): (root / name).write_bytes(data)
            private = root / 'docs/release/CRT-decision.md'
            private.parent.mkdir(parents=True); private.write_bytes(b'internal decision')
            output = Path(temp) / 'public'
            result = subprocess.run([sys.executable, str(SCRIPT.with_name('stage_public_source.py')),
                '--source', str(root), '--output', str(output)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name, data in expected.items(): self.assertEqual((output / name).read_bytes(), data)
            self.assertFalse((output / 'docs/release/CRT-decision.md').exists())

    def test_history_scans_deleted_private_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args):
                return subprocess.run(['git', '-c', 'safe.directory=' + str(root),
                    '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    '-C', str(root), *args], check=True, capture_output=True)
            git('init')
            (root / 'private.md').write_text('C:' + '\\Users\\' + 'deleted-owner\\file', encoding='utf-8')
            git('add', '.')
            git('commit', '-m', 'fixture')
            (root / 'private.md').unlink()
            git('add', '-u')
            git('commit', '-m', 'remove')
            result = subprocess.run([sys.executable, str(SCRIPT), '--git-history', str(root)],
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('PERSONAL_PATH', result.stdout)
            self.assertNotIn('deleted-owner', result.stdout)

    def test_source_export_sanitizes_copy_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            root.mkdir()
            (root / 'docs/evidence').mkdir(parents=True)
            (root / 'config').mkdir()
            original = 'C:' + '\\Users\\' + 'synthetic-owner\\game'
            (root / 'README.md').write_text(original, encoding='utf-8')
            (root / 'docs/evidence/private.json').write_text('{}', encoding='utf-8')
            (root / 'config/local.private.json').write_text('{}', encoding='utf-8')
            out = Path(temp) / 'export'
            result = subprocess.run([sys.executable, str(SCRIPT.with_name('stage_public_source.py')),
                '--source', str(root), '--output', str(out)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'README.md').read_text(encoding='utf-8'), original)
            self.assertFalse((out / 'docs/evidence').exists())
            self.assertFalse((out / 'config/local.private.json').exists())
            self.assertEqual(self.scan(out).returncode, 0)

    def test_source_export_preserves_verified_material_bytes_in_git(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            material = Path('docs/release/dependency-licenses/upstream/supplier/notice.txt')
            (root / material).parent.mkdir(parents=True)
            original = b'Upstream notice\r\nVerified original bytes\r\n'
            (root / material).write_bytes(original)
            attributes = b'docs/release/dependency-licenses/upstream/** -text\n'
            (root / '.gitattributes').write_bytes(attributes)
            out = Path(temp) / 'export'
            result = subprocess.run([sys.executable, str(SCRIPT.with_name('stage_public_source.py')),
                '--source', str(root), '--output', str(out)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((out / '.gitattributes').is_file())
            self.assertEqual((out / '.gitattributes').read_bytes(), attributes)
            for args in [('init', '-q'), ('config', 'core.autocrlf', 'true'), ('add', '.')]:
                subprocess.run(['git', '-C', str(out), *args], check=True, capture_output=True)
            stored = subprocess.check_output(['git', '-C', str(out), 'show', ':' + material.as_posix()])
            self.assertEqual(stored, original)
            self.assertEqual((root / material).read_bytes(), original)

    def test_export_denies_unknown_internal_documents_but_keeps_public_and_license_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            files = {
                'docs/QUICKSTART.md': b'Public quick start',
                'docs/new-session-notes.md': b'Internal attachment names and host inventory',
                'docs/release/new-audit.json': b'{"internal_authorization": "private narrative"}',
                'docs/release/接手推进记录.md': b'Internal session transcript',
                'docs/release/dependency-licenses/upstream/vendor/LICENSE.md':
                    b'Upstream original\r\nhttps://chatgpt.com/' + b'share/supplier-example\r\n',
                'gachaops/example.py': b'# executable source is never rewritten\n',
            }
            for name, data in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            out = Path(temp) / 'export'
            result = subprocess.run([sys.executable, str(SCRIPT.with_name('stage_public_source.py')),
                '--source', str(root), '--output', str(out)], capture_output=True, text=True, encoding='utf-8')
            for name in ('docs/new-session-notes.md', 'docs/release/new-audit.json',
                         'docs/release/接手推进记录.md'):
                self.assertFalse((out / name).exists(), name)
            self.assertEqual((out / 'docs/QUICKSTART.md').read_bytes(), files['docs/QUICKSTART.md'])
            self.assertEqual((out / 'gachaops/example.py').read_bytes(), files['gachaops/example.py'])
            notice = 'docs/release/dependency-licenses/upstream/vendor/LICENSE.md'
            self.assertEqual((out / notice).read_bytes(), files[notice])
            # A private marker in supplier material blocks review; it must never be silently rewritten.
            self.assertEqual(result.returncode, 1)
            for name, data in files.items():
                self.assertEqual((root / name).read_bytes(), data)

    def test_export_keeps_public_compatibility_reference_without_internal_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            for name, data in {
                'docs/MAAEND.md': b'Private host inventory and historical sessions',
                'docs/public/MAAEND.md': b'<!-- gachaops-public-evidence-v1 -->\nPublic adapter contract and no-game evidence',
                'gachaops/assets/support_matrix.json': b'{"adapters": [{"evidence": ["docs/MAAEND.md"]}]}',
            }.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            out = Path(temp) / 'export'
            result = subprocess.run([sys.executable, str(SCRIPT.with_name('stage_public_source.py')),
                '--source', str(root), '--output', str(out)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((out / 'docs/MAAEND.md').is_file())
            self.assertEqual((out / 'docs/MAAEND.md').read_bytes(), b'<!-- gachaops-public-evidence-v1 -->\nPublic adapter contract and no-game evidence')
            self.assertEqual((root / 'docs/MAAEND.md').read_bytes(), b'Private host inventory and historical sessions')
            summary = out / 'docs/MAAEND.md'
            summary.write_bytes(summary.read_bytes().replace(b'\n', b'\r\n'))
            second = Path(temp) / 'reexport'
            rerun = subprocess.run([sys.executable, str(SCRIPT.with_name('stage_public_source.py')),
                '--source', str(out), '--output', str(second)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
            self.assertEqual((second / 'docs/MAAEND.md').read_bytes(), (out / 'docs/MAAEND.md').read_bytes())

    def test_export_blocks_missing_declared_compatibility_document(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'source'
            matrix = root / 'gachaops/assets/support_matrix.json'; matrix.parent.mkdir(parents=True)
            matrix.write_text(json.dumps({'adapters': [{'evidence': ['docs/MISSING.md']}]}), encoding='utf-8')
            result = subprocess.run([sys.executable, str(SCRIPT.with_name('stage_public_source.py')),
                '--source', str(root), '--output', str(Path(temp) / 'export')],
                capture_output=True, text=True, encoding='utf-8')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('MISSING_COMPATIBILITY_EVIDENCE', result.stdout)
