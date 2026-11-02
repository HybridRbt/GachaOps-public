import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/stage_release.py'


class ReleaseStagingTests(unittest.TestCase):
    def test_application_source_missing_stale_or_incomplete_even_with_updated_outer_hashes_blocks(self):
        for case in ('missing', 'old_inner_commit', 'bad_input_hash', 'missing_build_input', 'resource_drift'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); source, app = self.fixture(root)
                approval_path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
                approval = json.loads(approval_path.read_text())
                delivery = source / 'docs/release/source-delivery/APPLICATION_SOURCE_DELIVERY.json'
                record = json.loads(delivery.read_text())
                archive = source / record['archive']['source']
                if case == 'missing':
                    approval['materials'] = [r for r in approval['materials'] if not r['source'].endswith('APPLICATION_SOURCE_DELIVERY.json')]
                elif case == 'resource_drift':
                    matrix = app / '_internal/gachaops/assets/support_matrix.json'
                    matrix.parent.mkdir(parents=True)
                    matrix.write_bytes(b'different runtime matrix')
                else:
                    with zipfile.ZipFile(archive) as z:
                        files = {n: z.read(n) for n in z.namelist()}
                    manifest = json.loads(files['SOURCE_MANIFEST.json'])
                    if case == 'old_inner_commit': manifest['candidate_code_commit'] = 'b' * 40
                    elif case == 'bad_input_hash': manifest['git_input_files'][0]['sha256'] = '0' * 64
                    else:
                        files.pop('gachaops/maagf2.py')
                        manifest['git_input_files'] = [r for r in manifest['git_input_files'] if r['path'] != 'gachaops/maagf2.py']
                        record['original_git_inputs'] -= 1; record['archive_entries'] -= 1
                    files['SOURCE_MANIFEST.json'] = json.dumps(manifest).encode()
                    with zipfile.ZipFile(archive, 'w') as z:
                        for name, data in files.items(): z.writestr(name, data)
                    record['archive']['sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
                    record['archive']['bytes'] = archive.stat().st_size
                    delivery.write_text(json.dumps(record))
                    bundle_path = source / 'docs/release/source-delivery/SOURCE_BUNDLE_MANIFEST.json'
                    bundle = json.loads(bundle_path.read_text())
                    bundle['archives'] = [record['archive'] if row.get('scope') == 'application_source_and_build_inputs'
                                          else row for row in bundle['archives']]
                    bundle_path.write_text(json.dumps(bundle))
                    for row in approval['materials']:
                        row['sha256'] = hashlib.sha256((source / row['source']).read_bytes()).hexdigest()
                approval_path.write_text(json.dumps(approval))
                output = root / 'staged'
                self.assertNotEqual(self.stage(source, app, output).returncode, 0)
                self.assertFalse(output.exists())

    def test_wrong_application_source_identity_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            relative = 'docs/release/source-delivery/APPLICATION_SOURCE_DELIVERY.json'
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({'schema_version': 1, 'candidate_code_commit': 'b' * 40,
                                        'candidate_executable_sha256': '0' * 64}), encoding='utf-8')
            approval_path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
            approval = json.loads(approval_path.read_text())
            approval['materials'] = [r for r in approval['materials'] if r['source'] != relative]
            approval['materials'].append({'source': relative,
                'destination': 'SOURCE_DELIVERY/APPLICATION_SOURCE_DELIVERY.json',
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            approval_path.write_text(json.dumps(approval), encoding='utf-8')
            output = root / 'staged'
            result = self.stage(source, app, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())

    def fixture(self, root):
        source = root / 'source'; source.mkdir()
        (source / 'docs').mkdir()
        for path in ('LICENSE', 'PACKAGE_THIRD_PARTY_NOTICES.md', 'PACKAGE_MICROSOFT_CRT_TERMS.md', 'SECURITY.md', 'docs/QUICKSTART.md', 'docs/SUPPORT_MATRIX.md', 'docs/USAGE_NOTICE.md', 'docs/DATA_DIRECTORIES.md', 'docs/ADDING_ADAPTER.md', 'docs/VALIDATION.md', 'docs/THIRD_PARTY_ASSISTANTS.md', 'PACKAGE_README.md'):
            (source / path).write_text('Synthetic release notice', encoding='utf-8')
        app = root / 'app'; app.mkdir(); (app / '_internal').mkdir()
        (app / 'GachaOps.exe').write_bytes(b'synthetic executable fixture')
        (app / '_internal/python.dll').write_bytes(b'synthetic runtime fixture')
        manifest = {'schema_version': 1, 'dirty': False, 'commit': 'a' * 40,
                    'executable_sha256': hashlib.sha256((app / 'GachaOps.exe').read_bytes()).hexdigest()}
        (app / 'build-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        (source / 'docs/release/dependency-licenses').mkdir(parents=True)
        material = source / 'docs/release/dependency-licenses/synthetic-license.txt'
        material.write_text('Synthetic dependency notice', encoding='utf-8')
        approval = {'schema_version': 1, 'approved': True,
                    'status': 'approved_distribution_materials',
                    'candidate_code_commit': manifest['commit'],
                    'candidate_executable_sha256': manifest['executable_sha256'],
                    'checks': {key: True for key in ('dependency_notices', 'corresponding_source',
                                                    'replacement_rebuild', 'bridge_asset_rights')},
                    'required_sha256': {path: hashlib.sha256((source / path).read_bytes()).hexdigest()
                                        for path in ('LICENSE', 'PACKAGE_THIRD_PARTY_NOTICES.md', 'PACKAGE_MICROSOFT_CRT_TERMS.md', 'SECURITY.md', 'docs/QUICKSTART.md', 'docs/SUPPORT_MATRIX.md', 'docs/USAGE_NOTICE.md', 'docs/DATA_DIRECTORIES.md', 'docs/ADDING_ADAPTER.md', 'docs/VALIDATION.md', 'docs/THIRD_PARTY_ASSISTANTS.md', 'PACKAGE_README.md')},
                    'materials': [{'source': material.relative_to(source).as_posix(),
                                   'destination': 'THIRD_PARTY_LICENSES/synthetic-license.txt',
                                   'sha256': hashlib.sha256(material.read_bytes()).hexdigest()}]}
        approval['required_destinations'] = {name: name for name in approval['required_sha256']}
        approval['required_destinations']['PACKAGE_README.md'] = 'README.md'
        approval['required_destinations']['PACKAGE_THIRD_PARTY_NOTICES.md'] = 'THIRD_PARTY_NOTICES.md'
        approval['required_destinations']['PACKAGE_MICROSOFT_CRT_TERMS.md'] = 'MICROSOFT_CRT_TERMS.md'
        # A synthetic but complete application source contract, separate from
        # dependency notices and bound to this fixture's exact app identity.
        source_dir = source / 'docs/release/source-delivery'
        archive_dir = source_dir / 'archives'; archive_dir.mkdir(parents=True)
        archive = archive_dir / 'application-source.zip'
        names = ('GachaOps.spec', 'pyproject.toml', 'requirements-desktop.txt',
                 'scripts/desktop_entry.py', 'gachaops/__init__.py', 'gachaops/__main__.py',
                 'gachaops/desktop.py', 'gachaops/maagf2.py', 'gachaops/assets/support_matrix.json')
        inputs = []
        with zipfile.ZipFile(archive, 'w') as z:
            for name in names:
                data = b'synthetic source fixture'
                z.writestr(name, data)
                inputs.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                               'git_blob': hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()})
            z.writestr('SOURCE_MANIFEST.json', json.dumps({'schema_version': 1,
                'candidate_code_commit': manifest['commit'], 'git_input_files': inputs,
                'supplements': ['SOURCE_MANIFEST.json']}))
        row = {'source': archive.relative_to(source).as_posix(), 'file': archive.name,
               'scope': 'application_source_and_build_inputs', 'bytes': archive.stat().st_size,
               'sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
        delivery = source_dir / 'APPLICATION_SOURCE_DELIVERY.json'
        delivery.write_text(json.dumps({'schema_version': 1, 'candidate_code_commit': manifest['commit'],
            'candidate_executable_sha256': manifest['executable_sha256'], 'archive': row,
            'original_git_inputs': len(inputs), 'archive_entries': len(inputs) + 1}), encoding='utf-8')
        bundle = source_dir / 'SOURCE_BUNDLE_MANIFEST.json'
        bundle.write_text(json.dumps({'schema_version': 1, 'archives': [row]}), encoding='utf-8')
        for path, destination in ((archive, 'SOURCE_DELIVERY/archives/' + archive.name),
                                  (delivery, 'SOURCE_DELIVERY/APPLICATION_SOURCE_DELIVERY.json'),
                                  (bundle, 'SOURCE_DELIVERY/SOURCE_BUNDLE_MANIFEST.json')):
            approval['materials'].append({'source': path.relative_to(source).as_posix(),
                'destination': destination, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        (source / 'docs/release/DISTRIBUTION_APPROVAL.json').write_text(json.dumps(approval), encoding='utf-8')
        return source, app

    def stage(self, source, app, output, policy=None):
        args = [sys.executable, str(SCRIPT), '--source', str(source), '--app-dir', str(app), '--output', str(output)]
        if policy is not None: args += ['--binary-dispositions', str(policy)]
        return subprocess.run(args, capture_output=True, text=True, encoding='utf-8')

    def binary_policy(self, root, app):
        data = ('C:' + '\\Users\\' + 'synthetic-builder\\library').encode()
        (app / '_internal/python.dll').write_bytes(data)
        manifest = json.loads((app / 'build-manifest.json').read_text())
        policy = {'schema_version': 1, 'approved': True, 'status': 'approved_exact_binary_findings',
                  'approval': {'by': 'maintainer', 'date': '2026-10-25', 'scope': 'binary_privacy_only'},
                  'candidate_code_commit': manifest['commit'],
                  'candidate_executable_sha256': manifest['executable_sha256'],
                  'findings': [{'path': '_internal/python.dll', 'rule': 'PERSONAL_PATH',
                                'sha256': hashlib.sha256(data).hexdigest(), 'classification': 'dependency_build_path'}]}
        path = root / 'approved.json'; path.write_text(json.dumps(policy), encoding='utf-8')
        return path

    def test_crt_terms_are_delivered_exactly_and_missing_or_drifted_terms_block(self):
        for case in ('approved', 'changed_terms', 'missing_original'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); source, app = self.fixture(root)
                terms = source / 'PACKAGE_MICROSOFT_CRT_TERMS.md'
                content = b'CRT-specific terms\r\n[original](THIRD_PARTY_LICENSES/synthetic-license.txt)\r\n'
                terms.write_bytes(content)
                approval_path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
                approval = json.loads(approval_path.read_text())
                approval['required_sha256']['PACKAGE_MICROSOFT_CRT_TERMS.md'] = hashlib.sha256(content).hexdigest()
                approval_path.write_text(json.dumps(approval), encoding='utf-8')
                if case == 'changed_terms': terms.write_bytes(content + b'unreviewed change')
                if case == 'missing_original':
                    (source / 'docs/release/dependency-licenses/synthetic-license.txt').unlink()
                output = root / 'candidate'
                result = self.stage(source, app, output)
                if case == 'approved':
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual((output / 'MICROSOFT_CRT_TERMS.md').read_bytes(), content)
                    self.assertTrue((output / 'THIRD_PARTY_NOTICES.md').is_file())
                    self.assertNotIn('PACKAGE_MICROSOFT_CRT_TERMS.md', json.loads((output / 'checksums.json').read_text()))
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())

    def test_allowlisted_staging_has_checksums_not_private_build_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            result = self.stage(source, app, root / 'candidate')
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((root / 'candidate/checksums.json').read_text())
            self.assertIn('GachaOps.exe', manifest)
            self.assertIn('LICENSE', manifest)
            self.assertTrue((root / 'candidate/_internal/python.dll').is_file())
            self.assertTrue((root / 'candidate/THIRD_PARTY_LICENSES/synthetic-license.txt').is_file())
            self.assertIn('BINARY_PRIVACY_REPORT.json', manifest)
            self.assertIn('DISTRIBUTION_APPROVAL.json', manifest)
            for relative, digest in manifest.items():
                self.assertEqual(hashlib.sha256((root / 'candidate' / relative).read_bytes()).hexdigest(), digest)
            self.assertNotIn(str(root), result.stdout)

    def test_package_keeps_docs_layout_and_usage_notice_links_reachable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            quick = source / 'docs/QUICKSTART.md'
            quick.write_text('[license](../LICENSE) [usage](USAGE_NOTICE.md)', encoding='utf-8')
            notice = source / 'docs/USAGE_NOTICE.md'
            notice.write_text('Use only with explicit consent', encoding='utf-8')
            path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
            approval = json.loads(path.read_text())
            approval['required_sha256']['docs/QUICKSTART.md'] = hashlib.sha256(quick.read_bytes()).hexdigest()
            approval['required_sha256']['docs/USAGE_NOTICE.md'] = hashlib.sha256(notice.read_bytes()).hexdigest()
            path.write_text(json.dumps(approval), encoding='utf-8')
            result = self.stage(source, app, root / 'candidate')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((root / 'candidate/docs/QUICKSTART.md').is_file())
            self.assertTrue((root / 'candidate/docs/USAGE_NOTICE.md').is_file())

    def test_approved_but_broken_local_document_link_blocks_before_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
            approval = json.loads(path.read_text())
            quick = source / 'docs/QUICKSTART.md'
            quick.write_text('[missing](missing.md)', encoding='utf-8')
            approval['required_sha256']['docs/QUICKSTART.md'] = hashlib.sha256(quick.read_bytes()).hexdigest()
            path.write_text(json.dumps(approval), encoding='utf-8')
            result = self.stage(source, app, root / 'candidate')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'candidate').exists())

    def test_material_approval_must_bind_delivery_destinations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
            approval = json.loads(path.read_text())
            approval['required_destinations'] = {name: name for name in approval['required_sha256']}
            approval['required_destinations']['docs/QUICKSTART.md'] = 'QUICKSTART.md'
            path.write_text(json.dumps(approval), encoding='utf-8')
            self.assertNotEqual(self.stage(source, app, root / 'candidate').returncode, 0)
            self.assertFalse((root / 'candidate').exists())

    def test_reference_and_unquoted_html_links_cannot_bypass_package_gate(self):
        for text in ('[notice][usage]\n\n[usage]: missing.md',
                     '[usage][]\n\n[usage]: missing.md',
                     '[usage]\n\n[usage]: ../../outside.md',
                     '<a href=missing.md>notice</a>'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); source, app = self.fixture(root)
                quick = source / 'docs/QUICKSTART.md'; quick.write_text(text, encoding='utf-8')
                path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
                approval = json.loads(path.read_text())
                approval['required_sha256']['docs/QUICKSTART.md'] = hashlib.sha256(quick.read_bytes()).hexdigest()
                path.write_text(json.dumps(approval), encoding='utf-8')
                self.assertNotEqual(self.stage(source, app, root / 'candidate').returncode, 0)
                self.assertFalse((root / 'candidate').exists())

    def test_missing_license_private_binding_and_existing_target_block(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            (source / 'LICENSE').unlink()
            self.assertNotEqual(self.stage(source, app, root / 'missing-license').returncode, 0)
            (source / 'LICENSE').write_text('Synthetic', encoding='utf-8')
            (app / '_internal/gachaops-workspace.json').write_text('{}', encoding='utf-8')
            self.assertNotEqual(self.stage(source, app, root / 'private').returncode, 0)
            existing = root / 'existing'; existing.mkdir(); (existing / 'preserve').touch()
            self.assertNotEqual(self.stage(source, app, existing).returncode, 0)
            self.assertTrue((existing / 'preserve').exists())

    def test_explicit_binary_policy_is_used_before_and_after_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            policy = self.binary_policy(root, app)
            self.assertNotEqual(self.stage(source, app, root / 'raw').returncode, 0)
            self.assertFalse((root / 'raw').exists())
            result = self.stage(source, app, root / 'accepted', policy)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((root / 'accepted/BINARY_PRIVACY_REPORT.json').read_text())
            self.assertEqual(len(report['raw_findings']), 1)
            self.assertEqual(len(report['accepted_findings']), 1)
            self.assertFalse(report['distribution_approved'])
            self.assertTrue((root / 'accepted/BINARY_DISPOSITIONS.json').is_file())

    def test_distribution_approval_remains_separate_from_binary_acceptance(self):
        for case in ['missing', 'unapproved', 'checks', 'candidate', 'notice', 'material', 'duplicate', 'destination', 'reserved_destination']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); source, app = self.fixture(root)
                policy = self.binary_policy(root, app)
                path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
                approval = json.loads(path.read_text())
                if case == 'missing': path.unlink()
                else:
                    if case == 'unapproved': approval['approved'] = False
                    elif case == 'checks': approval['checks']['replacement_rebuild'] = False
                    elif case == 'candidate': approval['candidate_executable_sha256'] = '0' * 64
                    elif case == 'notice': (source / 'LICENSE').write_text('changed', encoding='utf-8')
                    elif case == 'material': (source / approval['materials'][0]['source']).write_text('changed', encoding='utf-8')
                    elif case == 'duplicate': approval['materials'] *= 2
                    elif case == 'destination': approval['materials'][0]['destination'] = '../escape.txt'
                    elif case == 'reserved_destination': approval['materials'][0]['destination'] = 'THIRD_PARTY_LICENSES/CON.txt'
                    path.write_text(json.dumps(approval), encoding='utf-8')
                result = self.stage(source, app, root / 'candidate', policy)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((root / 'candidate').exists())
                self.assertEqual(json.loads(result.stdout)['gate'], 'distribution_materials')

    def test_source_document_is_not_covered_by_binary_policy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            policy = self.binary_policy(root, app)
            notice = source / 'LICENSE'
            notice.write_bytes((app / '_internal/python.dll').read_bytes())
            path = source / 'docs/release/DISTRIBUTION_APPROVAL.json'
            approval = json.loads(path.read_text())
            approval['required_sha256']['LICENSE'] = hashlib.sha256(notice.read_bytes()).hexdigest()
            path.write_text(json.dumps(approval), encoding='utf-8')
            result = self.stage(source, app, root / 'candidate', policy)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'candidate').exists())

    def test_copy_time_runtime_change_is_rejected_without_success_checksums(self):
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            policy = self.binary_policy(root, app)
            sys.path.insert(0, str(SCRIPT.parent))
            try:
                spec = importlib.util.spec_from_file_location('staging_copy_race', SCRIPT)
                module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                original = module.shutil.copytree

                def changed_copy(*args, **kwargs):
                    result = original(*args, **kwargs)
                    runtime = Path(args[1]) / 'python.dll'
                    runtime.write_bytes(runtime.read_bytes() + b'changed during copy')
                    return result

                with mock.patch.object(module.shutil, 'copytree', side_effect=changed_copy):
                    with self.assertRaises(ValueError):
                        module.stage_release(source, app, root / 'candidate', policy)
                self.assertFalse((root / 'candidate/checksums.json').exists())
            finally:
                sys.path.pop(0)

    def test_change_when_checksums_are_written_is_rejected(self):
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source, app = self.fixture(root)
            policy = self.binary_policy(root, app)
            sys.path.insert(0, str(SCRIPT.parent))
            try:
                spec = importlib.util.spec_from_file_location('staging_checksum_race', SCRIPT)
                module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                original = Path.write_text

                def mutate_after_checksums(path, *args, **kwargs):
                    result = original(path, *args, **kwargs)
                    if path == root / 'candidate/checksums.json':
                        runtime = root / 'candidate/_internal/python.dll'
                        runtime.write_bytes(runtime.read_bytes() + b'changed after scan')
                    return result

                with mock.patch.object(Path, 'write_text', new=mutate_after_checksums):
                    with self.assertRaises(ValueError):
                        module.stage_release(source, app, root / 'candidate', policy)
                self.assertFalse((root / 'candidate/checksums.json').exists())
            finally:
                sys.path.pop(0)

    def test_final_tree_is_bound_to_original_app_and_frozen_materials(self):
        import unittest.mock as mock
        for case in ['notice_after_report', 'new_after_checksums', 'delete_after_checksums', 'clean_runtime_during_copy']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); source, app = self.fixture(root)
                (app / '_internal/unflagged.dll').write_bytes(b'original clean library')
                policy = self.binary_policy(root, app)
                sys.path.insert(0, str(SCRIPT.parent))
                try:
                    spec = importlib.util.spec_from_file_location('staging_final_tree', SCRIPT)
                    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
                    original_write, original_copy = Path.write_text, module.shutil.copytree

                    def changed_write(path, *args, **kwargs):
                        result = original_write(path, *args, **kwargs)
                        if path == root / 'candidate/BINARY_PRIVACY_REPORT.json' and case == 'notice_after_report':
                            (root / 'candidate/LICENSE').write_bytes(b'changed safe notice')
                        if path == root / 'candidate/checksums.json':
                            if case == 'new_after_checksums':
                                (root / 'candidate/_internal/untracked.dll').write_bytes(b'new safe library')
                            elif case == 'delete_after_checksums': (root / 'candidate/LICENSE').unlink()
                        return result

                    def changed_copy(*args, **kwargs):
                        result = original_copy(*args, **kwargs)
                        if case == 'clean_runtime_during_copy':
                            (Path(args[1]) / 'unflagged.dll').write_bytes(b'changed clean library')
                        return result

                    with mock.patch.object(Path, 'write_text', new=changed_write), mock.patch.object(module.shutil, 'copytree', side_effect=changed_copy):
                        with self.assertRaises((ValueError, OSError)):
                            module.stage_release(source, app, root / 'candidate', policy)
                    self.assertFalse((root / 'candidate/checksums.json').exists())
                finally:
                    sys.path.pop(0)
