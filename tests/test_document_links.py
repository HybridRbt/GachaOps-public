import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_document_links.py'


class DocumentLinksTests(unittest.TestCase):
    def test_source_cli_checks_package_templates_in_their_delivery_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = {
                'MICROSOFT_CRT_TERMS.md': b'[original](docs/release/dependency-licenses/Microsoft/original.txt)',
                'PACKAGE_MICROSOFT_CRT_TERMS.md': b'[original](THIRD_PARTY_LICENSES/Microsoft/original.txt)',
                'docs/release/dependency-licenses/Microsoft/original.txt': b'supplier original',
            }
            for name, data in files.items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            def check():
                return subprocess.run([sys.executable, str(SCRIPT), '--root', str(root)],
                                      capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(check().returncode, 0)
            for name in ('MICROSOFT_CRT_TERMS.md', 'PACKAGE_MICROSOFT_CRT_TERMS.md'):
                with self.subTest(document=name):
                    (root / name).write_bytes(b'[missing](missing.txt)')
                    self.assertEqual(check().returncode, 1)
                    (root / name).write_bytes(files[name])
            # A template must not accidentally resolve a source-only path.
            (root / 'PACKAGE_MICROSOFT_CRT_TERMS.md').write_bytes(files['MICROSOFT_CRT_TERMS.md'])
            self.assertEqual(check().returncode, 1)

    def test_local_encoded_paths_and_html_are_checked_without_escaping_root(self):
        spec = importlib.util.spec_from_file_location('document_links', SCRIPT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        files = {'docs/start.md': b'[notice](%E5%A3%B0%E6%98%8E.md) [license](../LICENSE)\n'
                 b'<img src="pic.svg"> [web](https://example.invalid/) [anchor](#part)',
                 'docs/声明.md': b'notice', 'LICENSE': b'license', 'docs/pic.svg': b'<svg/>'}
        self.assertEqual(module.check_links(files), [])
        refs = dict(files, **{'docs/start.md': b'[license][lic] [lic][] [lic]\n\n[lic]: ../LICENSE\n'
                             b'<img src=pic.svg>'})
        self.assertEqual(module.check_links(refs), [])
        for target in ('missing.md', '../../outside', '%2e%2e/%2e%2e/outside', 'file:///outside', '/absolute'):
            with self.subTest(target=target):
                broken = dict(files, **{'docs/start.md': ('[bad](' + target + ')').encode()})
                self.assertTrue(module.check_links(broken))
