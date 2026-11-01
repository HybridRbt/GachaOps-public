import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


class DiagnosticsTests(unittest.TestCase):
    def test_preview_and_zip_allowlist_preserve_failure_without_private_content(self):
        from gachaops.diagnostics import build_diagnostic_preview, export_diagnostics
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'run'; root.mkdir()
            source = root / 'summary.json'
            source.write_text(json.dumps({'status': 'completion_unconfirmed', 'cleanup_verified': False,
                'id': 'genshin', 'argv': ['secret-token'], 'error': 'private-user-path',
                'unknown': {'token': 'secret-token', 'raw_hex': 'deadbeef'},
                'completion': {'status': 'partial_failure', 'tasks': [{'status': 'failed', 'name': 'private-account'}]}}), encoding='utf-8')
            original = source.read_bytes()
            preview = build_diagnostic_preview(root)
            self.assertEqual(preview.files, ('diagnostics.json', 'manifest.json'))
            output = export_diagnostics(preview, Path(temp) / 'diagnostic.zip')
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), set(preview.files))
                text = archive.read('diagnostics.json').decode('utf-8')
                self.assertIn('completion_unconfirmed', text)
                self.assertIn('partial_failure', text)
                for private in ('secret-token', 'private-user', 'deadbeef', 'private-account', 'argv'):
                    self.assertNotIn(private, text)
            self.assertEqual(source.read_bytes(), original)

    def test_changed_source_nested_destination_and_write_failure_do_not_publish(self):
        from gachaops.diagnostics import build_diagnostic_preview, export_diagnostics
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'run'; root.mkdir()
            source = root / 'summary.json'; source.write_text('{"status":"quarantined"}', encoding='utf-8')
            preview = build_diagnostic_preview(root)
            with self.assertRaises(ValueError): export_diagnostics(preview, root / 'nested.zip')
            source.write_text('{"status":"success"}', encoding='utf-8')
            with self.assertRaises(ValueError): export_diagnostics(preview, Path(temp) / 'changed.zip')
            preview = build_diagnostic_preview(root)
            with patch('gachaops.diagnostics.os.replace', side_effect=PermissionError('fixture')):
                with self.assertRaises(PermissionError): export_diagnostics(preview, Path(temp) / 'failed.zip')
            self.assertFalse((Path(temp) / 'failed.zip').exists())

    def test_missing_corrupt_and_oversized_inputs_are_explicit(self):
        from gachaops.diagnostics import build_diagnostic_preview
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertIn('summary.json', build_diagnostic_preview(root).missing)
            (root / 'summary.json').write_text('{', encoding='utf-8')
            with self.assertRaises(ValueError): build_diagnostic_preview(root)
            (root / 'summary.json').write_bytes(b'x' * (1024 * 1024 + 1))
            with self.assertRaises(ValueError): build_diagnostic_preview(root)
