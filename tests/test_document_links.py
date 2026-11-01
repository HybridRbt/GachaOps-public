import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_document_links.py'


class DocumentLinksTests(unittest.TestCase):
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
