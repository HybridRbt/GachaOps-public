import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops import app_paths
from gachaops.desktop_model import worker_command


class AppPathsTests(unittest.TestCase):
    def test_unbound_frozen_uses_stable_local_data_and_bundle_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / 'readonly app'; app.mkdir()
            with patch('sys.frozen', True, create=True), patch('sys.executable', str(app / 'GachaOps.exe')), \
                 patch('sys._MEIPASS', str(root / 'bundle'), create=True), \
                 patch.dict(os.environ, LOCALAPPDATA=str(root / '用户 data')):
                paths = app_paths.resolve_app_paths()
                self.assertEqual(paths.data_root, root / '用户 data/GachaOps')
                self.assertEqual(paths.resource_root, root / 'bundle')
                self.assertEqual(paths.state_dir, paths.data_root / 'state')
                self.assertEqual(app_paths.workspace_root(), paths.data_root)
                self.assertFalse(paths.data_root.exists())

    def test_binding_remains_authoritative_and_corruption_never_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / 'app'; app.mkdir()
            old = root / '旧部署'; (old / 'config').mkdir(parents=True)
            marker = app / 'gachaops-workspace.json'
            marker.write_text(json.dumps({'schema_version': 1, 'workspace': str(old)}), encoding='utf-8')
            with patch('sys.frozen', True, create=True), patch('sys.executable', str(app / 'GachaOps.exe')):
                self.assertEqual(app_paths.resolve_app_paths().data_root, old)
                with self.assertRaises(ValueError):
                    app_paths.resolve_app_paths(root / 'new')
                marker.write_text('{', encoding='utf-8')
                with self.assertRaises(ValueError):
                    app_paths.resolve_app_paths()

    def test_standalone_explicit_root_cannot_bypass_default_quarantine(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app = root / 'app'; app.mkdir()
            default = root / 'local/GachaOps'; (default / 'state').mkdir(parents=True)
            (default / 'state/execution.json').write_text(json.dumps({'schema_version': 1,
                'run_id': 'fixture', 'phase': 'quarantined', 'cleanup_verified': False}), encoding='utf-8')
            with patch('sys.frozen', True, create=True), patch('sys.executable', str(app / 'GachaOps.exe')), \
                 patch.dict(os.environ, LOCALAPPDATA=str(root / 'local')):
                with self.assertRaises(ValueError): app_paths.resolve_app_paths(root / 'escape')

    def test_admission_rechecks_default_quarantine_after_path_resolution(self):
        from gachaops.recovery import ExecutionSession
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); app = root / 'app'; app.mkdir()
            default = root / 'local/GachaOps'; (default / 'state').mkdir(parents=True)
            with patch('sys.frozen', True, create=True), patch('sys.executable', str(app / 'GachaOps.exe')), \
                 patch.dict(os.environ, LOCALAPPDATA=str(root / 'local')):
                selected = app_paths.resolve_app_paths(root / 'new-data')
                (default / 'state/execution.json').write_text(json.dumps({'schema_version': 1,
                    'run_id': 'fixture', 'phase': 'quarantined', 'cleanup_verified': False}), encoding='utf-8')
                with self.assertRaises(ValueError):
                    with ExecutionSession(selected.state_dir, 'fixture'): pass
                self.assertFalse((selected.state_dir / 'execution.json').exists())

    def test_migration_cli_requires_external_preview_and_approved_preview_applies(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'old'; target = root / 'new'
            (source / 'config').mkdir(parents=True); (source / 'state').mkdir()
            journal = source / 'state/execution.json'
            journal.write_text(json.dumps({'schema_version': 1, 'run_id': 'fixture',
                'phase': 'idle', 'cleanup_verified': True}), encoding='utf-8')
            base = [sys.executable, '-m', 'gachaops', 'migration-preview', '--data-root', str(source),
                    '--migration-source', str(source), '--migration-target', str(target)]
            result = subprocess.run(base, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 2)
            self.assertFalse((source / 'runs').exists())
            preview = root / 'preview'
            result = subprocess.run(base + ['--output', str(preview)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            base[3] = 'migrate-data'
            result = subprocess.run(base + ['--approved-preview', str(preview / 'migration-preview.json')], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((target / 'state/execution.json').exists())

    def test_explicit_root_and_worker_share_same_absolute_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / '数据 root'
            paths = app_paths.resolve_app_paths(root)
            self.assertEqual(paths.config_dir, root / 'config')
            command = worker_command('check-assistants', root / 'config/a.json', root / 'runs', root / 'stop', root / 'log', data_root=root)
            self.assertEqual(command[command.index('--data-root') + 1], str(root))
            with self.assertRaises(ValueError):
                app_paths.resolve_app_paths(Path('relative'))

    def test_legacy_quarantine_cannot_be_escaped_by_explicit_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'config').mkdir(); (root / 'gachaops').mkdir(); (root / 'state').mkdir()
            (root / 'gachaops/__init__.py').touch()
            (root / 'state/execution.json').write_text(json.dumps({'schema_version': 1,
                'run_id': 'fixture', 'phase': 'quarantined', 'cleanup_verified': False}), encoding='utf-8')
            with patch('sys.frozen', True, create=True), patch('sys.executable', str(root / 'app.exe')):
                with self.assertRaises(ValueError): app_paths.resolve_app_paths(root.parent / 'escape')

    def test_cli_explicit_root_writes_only_its_default_runs(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = subprocess.run([sys.executable, '-m', 'gachaops', 'recover-assistants',
                '--data-root', str(root)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / 'runs/recovery.json').read_text())['status'], 'already_ready')

    def test_source_worker_runs_from_data_directory_without_source_pythonpath(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / 'config.json'; config.write_text('{"schema_version":1,"assistants":[]}', encoding='utf-8')
            command = worker_command('check-assistants', config, root / 'runs', root / 'stop', root / 'worker.log', data_root=root)
            env = dict(os.environ); env.pop('PYTHONPATH', None)
            result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / 'worker.log').read_text())['assistants'], [])

    def test_idle_migration_is_previewed_atomic_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'old'; target = root / 'new'
            (source / 'config').mkdir(parents=True)
            (source / 'state').mkdir()
            journal = {'schema_version': 1, 'run_id': 'fixture', 'phase': 'idle', 'cleanup_verified': True}
            (source / 'state/execution.json').write_text(json.dumps(journal), encoding='utf-8')
            (source / 'config/a.json').write_text('{}', encoding='utf-8')
            preview = app_paths.preview_migration(source, target)
            self.assertFalse(target.exists())
            app_paths.migrate_data(source, target, preview)
            self.assertEqual(json.loads((target / 'state/execution.json').read_text()), journal)
            self.assertTrue((source / 'config/a.json').exists())
            self.assertTrue(list((target / 'backups').glob('*')))

    def test_quarantined_conflicting_changed_or_failed_migration_keeps_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / 'old'; target = root / 'new'
            (source / 'config').mkdir(parents=True); (source / 'state').mkdir()
            path = source / 'state/execution.json'
            journal = {'schema_version': 1, 'run_id': 'fixture', 'phase': 'quarantined', 'cleanup_verified': False}
            path.write_text(json.dumps(journal), encoding='utf-8')
            with self.assertRaises(ValueError): app_paths.preview_migration(source, target)
            journal.update(phase='idle', cleanup_verified=True)
            path.write_text(json.dumps(journal), encoding='utf-8')
            preview = app_paths.preview_migration(source, target)
            with patch('gachaops.app_paths.os.replace', side_effect=PermissionError('fixture')):
                with self.assertRaises(PermissionError): app_paths.migrate_data(source, target, preview)
            self.assertFalse(target.exists())
            self.assertTrue(path.exists())
            (source / 'config/a.json').write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError): app_paths.migrate_data(source, target, preview)
            target.mkdir(); (target / 'existing').touch()
            with self.assertRaises(ValueError): app_paths.preview_migration(source, target)
