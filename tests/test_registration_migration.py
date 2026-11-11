"""Offline migration/import regressions with fixture directories only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.migration_journal import backup_root, journal_path, read_journal
from gachaops.planner import ConfigError
from gachaops.profiles import ProfileStore
from gachaops.registration_migration import (
    SourceChanged, commit_registration_import, commit_registration_migration,
    continue_registration_transaction, initialize_fresh, preview_registration_import,
    preview_registration_migration, startup_state)
from gachaops.registry import RegistrationStore


def write_profile(root, identity, document, name=None, created='2026-01-01T00:00:00+00:00'):
    directory = Path(root) / 'config/profiles' / identity
    directory.mkdir(parents=True)
    (directory / 'config.json').write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    (directory / 'name.json').write_text(json.dumps({'name': name or identity, 'created': created}),
                                         encoding='utf-8')


def schema1(root, rows):
    return {'schema_version': 1, 'repeat_limit': 0, 'assistants': rows}


def ok_row(root, enabled=False, **extra):
    return dict({'id': 'wuthering-waves', 'adapter': 'ok-ww', 'installation_root': str(root),
                 'enabled': enabled}, **extra)


def write_registry(root, assistants, revision=1):
    document = {'schema_version': 1, 'kind': 'assistant_registry', 'revision': revision,
                'assistants': assistants}
    path = Path(root) / 'config/assistant-registry.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


UUID_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
UUID_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


class StartupStateTests(unittest.TestCase):
    def test_fresh_ready_and_migration_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(startup_state(root)['status'], 'fresh')
            registry = initialize_fresh(root)
            self.assertEqual(registry['assistants'], {})
            self.assertEqual(startup_state(root)['status'], 'ready')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_profile(root, UUID_A, schema1(root, [ok_row(root)]), name='旧')
            self.assertEqual(startup_state(root)['status'], 'migration_required')
            write_registry(root, {})
            self.assertEqual(startup_state(root)['status'], 'migration_required')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ProfileStore(root); store.list_profiles()
            # Schema 2 profiles without a registry are a broken half-state:
            # startup must demand the migration service, not invent a library.
            self.assertEqual(startup_state(root)['status'], 'migration_required')
            initialize_fresh(root)
            self.assertEqual(startup_state(root)['status'], 'ready')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_profile(root, UUID_A, schema1(root, [ok_row(root, enabled=True)]), name='坏')
            (root / f'config/profiles/{UUID_A}/config.json').write_text('{"nope": 1}', encoding='utf-8')
            with self.assertRaises(ConfigError) as caught:
                startup_state(root)
            self.assertIn(UUID_A, str(caught.exception))


class MigrationPreviewTests(unittest.TestCase):
    def test_candidates_dedupe_by_full_tuple_and_registry_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_profile(root, UUID_A, schema1(root, [ok_row(root, enabled=True)]), name='A')
            write_profile(root, UUID_B, schema1(root, [
                ok_row(root, enabled=True),  # same tuple: dedup across profiles
                {'id': 'genshin', 'adapter': 'bettergi', 'installation_root': str(root),
                 'entry_executable': str(root / 'entry.exe'), 'enabled': False}]), name='B')
            write_registry(root, {'ok-ww': {'installation_root': str(root / 'other')}})
            preview = preview_registration_migration(root)
            adapters = {entry['adapter']: entry for entry in preview['adapters']}
            self.assertTrue(adapters['ok-ww']['conflict'])
            sources = adapters['ok-ww']['candidates'][0]['sources']
            self.assertIn('当前全局登记', sources)
            self.assertFalse(adapters['bettergi']['conflict'])
            self.assertEqual(len(adapters['bettergi']['candidates']), 1)
            self.assertEqual(preview['registry'], {'exists': True, 'revision': 1})
            self.assertEqual({p['schema'] for p in preview['profiles']}, {1})
            self.assertTrue(any(s['path'].endswith('name.json') for s in preview['sources']))

    def test_empty_registrations_never_shadow_and_missing_optional_entry_differs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_profile(root, UUID_A, schema1(root, [
                dict(ok_row(root), id='ww-empty', installation_root=None),
                ok_row(root)]), name='A')
            preview = preview_registration_migration(root)
            entry = next(e for e in preview['adapters'] if e['adapter'] == 'ok-ww')
            self.assertEqual(len(entry['candidates']), 1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_profile(root, UUID_A, schema1(root, [
                {'id': 'g1', 'adapter': 'bettergi', 'installation_root': str(root), 'enabled': False},
                {'id': 'g2', 'adapter': 'bettergi', 'installation_root': str(root),
                 'entry_executable': str(root / 'entry.exe'), 'enabled': False}]), name='A')
            entry = next(e for e in preview_registration_migration(root)['adapters']
                         if e['adapter'] == 'bettergi')
            self.assertTrue(entry['conflict'])  # missing optional entry is a different tuple


class MigrationCommitTests(unittest.TestCase):
    def test_conflict_free_migration_preserves_identity_and_archives_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_profile(root, UUID_A, schema1(root, [
                dict(ok_row(root, enabled=True, game_start='manual', timeout_seconds=2345), id='ww'),
                {'id': 'genshin', 'adapter': 'bettergi', 'installation_root': str(root / 'bg'),
                 'enabled': False}]), name='中文组合')
            (root / 'bg').mkdir()
            preview = preview_registration_migration(root)
            result = commit_registration_migration(root, preview, {})
            self.assertEqual(result['kind'], 'migration')
            store = ProfileStore(root)
            document = store.load_profile(UUID_A)
            self.assertEqual([r['id'] for r in document['assistants']], ['ww', 'genshin'])
            self.assertEqual(document['assistants'][0]['game_start'], 'manual')
            self.assertEqual(document['assistants'][0]['timeout_seconds'], 2345)
            self.assertTrue(document['assistants'][0]['enabled'])
            self.assertFalse(document['assistants'][1]['enabled'])
            self.assertEqual(set(document['assistants'][0]),
                             {'id', 'adapter', 'enabled', 'game_start', 'timeout_seconds'})
            self.assertEqual(store.list_profiles()[0]['name'], '中文组合')
            registry = RegistrationStore(root).read_required()
            self.assertEqual(registry['assistants']['ok-ww'], {'installation_root': str(root)})
            self.assertEqual(registry['assistants']['bettergi'], {'installation_root': str(root / 'bg')})
            self.assertIsNone(read_journal(root))
            self.assertTrue(list(backup_root(root).glob('*/complete.json')))
            self.assertTrue(list((backup_root(root)).glob('*/sources/config/profiles/*')))
            self.assertEqual(startup_state(root)['status'], 'ready')
            with self.assertRaises(ConfigError):
                commit_registration_migration(root, preview, {})  # repeated migration refused

    def test_conflicting_candidates_require_a_choice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            other = root / 'other'; other.mkdir()
            write_profile(root, UUID_A, schema1(root, [ok_row(root)]), name='A')
            write_profile(root, UUID_B, schema1(root, [ok_row(other)]), name='B')
            preview = preview_registration_migration(root)
            with self.assertRaises(ConfigError) as caught:
                commit_registration_migration(root, preview, {})
            self.assertIn('选择', str(caught.exception))
            commit_registration_migration(root, preview, {'ok-ww': 1})
            self.assertEqual(RegistrationStore(root).read_required()['assistants']['ok-ww'],
                             {'installation_root': str(other)})
            # 稍后重新接入: no entry, and the profile keeps its selection.
            write_registry(root, {'ok-ww': {'installation_root': 'C:/current'}})
            (root / f'config/profiles/{UUID_A}/config.json').write_text(
                json.dumps(schema1(root, [ok_row(root, enabled=True)])), encoding='utf-8')
            preview = preview_registration_migration(root)
            commit_registration_migration(root, preview, {'ok-ww': None})
            registry = RegistrationStore(root).read_required()
            self.assertNotIn('ok-ww', registry['assistants'])
            document = ProfileStore(root).load_profile(UUID_A)
            self.assertTrue(document['assistants'][0]['enabled'])

    def test_registry_entries_without_candidates_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_registry(root, {'ok-ww': {'installation_root': 'C:/kept'}})
            write_profile(root, UUID_A, schema1(root, [
                {'id': 'genshin', 'adapter': 'bettergi', 'installation_root': str(root), 'enabled': False}]),
                name='A')
            preview = preview_registration_migration(root)
            flagged = {entry['adapter']: entry['conflict'] for entry in preview['adapters']}
            self.assertEqual(flagged, {'ok-ww': False, 'bettergi': False})
            commit_registration_migration(root, preview, {})
            registry = RegistrationStore(root).read_required()
            self.assertEqual(registry['assistants']['ok-ww'], {'installation_root': 'C:/kept'})

    def test_source_changes_and_busy_execution_block_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_profile(root, UUID_A, schema1(root, [ok_row(root)]), name='A')
            preview = preview_registration_migration(root)
            (root / f'config/profiles/{UUID_A}/name.json').write_text(
                json.dumps({'name': '改过', 'created': '2026-01-02T00:00:00+00:00'}), encoding='utf-8')
            with self.assertRaises(SourceChanged):
                commit_registration_migration(root, preview, {})
            preview = preview_registration_migration(root)
            write_profile(root, UUID_B, schema1(root, [ok_row(root)]), name='新')
            with self.assertRaises(SourceChanged):
                commit_registration_migration(root, preview, {})
            preview = preview_registration_migration(root)
            with patch('gachaops.registration_migration.read_state', return_value={'phase': 'running'}):
                with self.assertRaises(ConfigError) as caught:
                    commit_registration_migration(root, preview, {})
                self.assertIn('清理未确认', str(caught.exception))


class MigrationContinueTests(unittest.TestCase):
    def prepare_interrupted(self, root):
        """Stage a transaction whose publishing stops after the registry target."""
        from gachaops import registration_migration
        write_profile(root, UUID_A, schema1(root, [ok_row(root, enabled=True)]), name='A')
        write_registry(root, {'bettergi': {'installation_root': 'C:/kept'}})
        preview = preview_registration_migration(root)
        real_publish = registration_migration._publish_targets

        def flaky(root_arg, targets):
            real_publish(root_arg, targets[:1])  # registry lands, profiles do not
            raise PermissionError('fixture interruption')
        with patch.object(registration_migration, '_publish_targets', flaky):
            with self.assertRaises(PermissionError):
                commit_registration_migration(root, preview, {})
        journal = read_journal(root)
        self.assertIsNotNone(journal)
        self.assertEqual(journal['state'], 'prepared')
        return preview

    def test_publish_failure_recovers_forward_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_interrupted(root)
            self.assertEqual(startup_state(root)['status'], 'prepared')
            result = continue_registration_transaction(root)
            self.assertEqual(result['kind'], 'migration')
            document = ProfileStore(root).load_profile(UUID_A)
            self.assertTrue(document['assistants'][0]['enabled'])
            registry = RegistrationStore(root).read_required()
            self.assertEqual(registry['assistants']['bettergi'], {'installation_root': 'C:/kept'})
            self.assertEqual(registry['assistants']['ok-ww'], {'installation_root': str(root)})
            self.assertIsNone(read_journal(root))
            self.assertEqual(startup_state(root)['status'], 'ready')
            with self.assertRaises(ConfigError):
                continue_registration_transaction(root)

    def test_external_edits_and_backup_damage_stop_continuation(self):
        from gachaops.profiles import default_profile, profile_document
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_interrupted(root)
            # Valid schema 2 but different bytes than the staged target.
            (root / f'config/profiles/{UUID_A}/config.json').write_text(
                json.dumps(profile_document(default_profile(), 1), ensure_ascii=False), encoding='utf-8')
            with self.assertRaises(ConfigError) as caught:
                continue_registration_transaction(root)
            self.assertIn('外部改动', str(caught.exception))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_interrupted(root)
            backup = next(backup_root(root).glob('*/sources/config/assistant-registry.json'))
            backup.write_bytes(b'corrupted')
            with self.assertRaises(ConfigError) as caught:
                continue_registration_transaction(root)
            self.assertIn('备份损坏', str(caught.exception))
            self.assertEqual(read_journal(root)['state'], 'prepared')


class ImportContinueTests(unittest.TestCase):
    def prepare_interrupted_import(self, root, publish_count):
        """Stage an import whose publishing stops after `publish_count` targets."""
        from gachaops import registration_migration
        initialize_fresh(root)
        write_registry(root, {'ok-ww': {'installation_root': 'C:/current'}})
        source = root.parent / 'external-import.json'
        source.write_text(json.dumps(schema1(root, [ok_row('C:/imported', enabled=True)])),
                          encoding='utf-8')
        preview = preview_registration_import(root, source, '导入组合')
        real_publish = registration_migration._publish_targets

        def flaky(root_arg, targets):
            real_publish(root_arg, targets[:publish_count])
            raise PermissionError('fixture interruption')
        with patch.object(registration_migration, '_publish_targets', flaky):
            with self.assertRaises(PermissionError):
                commit_registration_import(root, preview, {'ok-ww': 'keep'})
        journal = read_journal(root)
        self.assertIsNotNone(journal)
        self.assertEqual(journal['state'], 'prepared')
        return preview

    def test_partially_published_import_target_completes(self):
        """config.json landed but name.json did not: continuation must finish it
        instead of crashing on the half-written profile directory."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            preview = self.prepare_interrupted_import(root, 2)
            profile_dir = root / 'config/profiles' / preview['new_uuid']
            self.assertTrue((profile_dir / 'config.json').is_file())
            self.assertFalse((profile_dir / 'name.json').exists())
            result = continue_registration_transaction(root)
            self.assertEqual(result['kind'], 'import')
            document = ProfileStore(root).load_profile(preview['new_uuid'])
            self.assertTrue(document['assistants'][0]['enabled'])
            # The conflict choice ('keep') still applies to the merged registry.
            self.assertEqual(RegistrationStore(root).read_required()['assistants']['ok-ww'],
                             {'installation_root': 'C:/current'})
            self.assertIsNone(read_journal(root))
            self.assertEqual(startup_state(root)['status'], 'ready')

    def test_import_target_absent_altogether_completes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            preview = self.prepare_interrupted_import(root, 0)
            self.assertFalse(list((root / 'config/profiles').glob('*')))
            result = continue_registration_transaction(root)
            self.assertEqual(result['kind'], 'import')
            self.assertTrue(ProfileStore(root).load_profile(preview['new_uuid'])['assistants'])
            self.assertIsNone(read_journal(root))

    def test_unrelated_profile_dir_still_blocks_continuation(self):
        from gachaops.profiles import default_profile, profile_document
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            MigrationContinueTests.prepare_interrupted(self, root)
            write_profile(root, UUID_B, profile_document(default_profile(), 1), name='外来')
            with self.assertRaises(ConfigError) as caught:
                continue_registration_transaction(root)
            self.assertIn('未纳入清单', str(caught.exception))
            self.assertEqual(read_journal(root)['state'], 'prepared')


class ImportTests(unittest.TestCase):
    def external_schema2(self, root):
        return {'schema_version': 2, 'kind': 'assistant_profile', 'revision': 3,
                'repeat_limit': 1, 'assistants': [
                    {'id': 'wuthering-waves', 'adapter': 'ok-ww', 'enabled': True,
                     'game_start': 'manual', 'timeout_seconds': 999}]}

    def test_schema2_import_adds_references_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_fresh(root)
            source = root.parent / 'external-schema2.json'
            source.write_text(json.dumps(self.external_schema2(root)), encoding='utf-8')
            preview = preview_registration_import(root, source, '外来')
            self.assertEqual(preview['schema'], 2)
            self.assertEqual(preview['adapters'], [])
            result = commit_registration_import(root, preview, {})
            document = ProfileStore(root).load_profile(result['profile_id'])
            self.assertEqual(document['assistants'][0]['game_start'], 'manual')
            self.assertEqual(document['revision'], 1)  # fresh document, not the source revision
            self.assertEqual(RegistrationStore(root).read_required()['assistants'], {})
            self.assertIsNone(read_journal(root))

    def test_schema1_import_new_same_conflict_and_choices(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_fresh(root)
            write_registry(root, {'ok-ww': {'installation_root': 'C:/current'},
                                  'bettergi': {'installation_root': 'C:/bg'}})
            source = root.parent / 'external-schema1.json'
            source.write_text(json.dumps(schema1(root, [
                ok_row('C:/imported', enabled=True),
                {'id': 'genshin', 'adapter': 'bettergi', 'installation_root': 'C:/bg', 'enabled': False},
                {'id': 'nte', 'adapter': 'ok-nte', 'installation_root': 'C:/nte', 'enabled': False}])),
                encoding='utf-8')
            preview = preview_registration_import(root, source, '旧导出')
            self.assertEqual(preview['schema'], 1)
            actions = {entry['adapter']: entry['action'] for entry in preview['adapters']}
            self.assertEqual(actions, {'ok-ww': 'conflict', 'bettergi': 'same', 'ok-nte': 'new'})
            with self.assertRaises(ConfigError):
                commit_registration_import(root, preview, {})
            result = commit_registration_import(root, preview, {'ok-ww': 'keep'})
            assistants = RegistrationStore(root).read_required()['assistants']
            self.assertEqual(assistants['ok-ww'], {'installation_root': 'C:/current'})
            self.assertEqual(assistants['ok-nte'], {'installation_root': 'C:/nte'})
            document = ProfileStore(root).load_profile(result['profile_id'])
            self.assertTrue(document['assistants'][0]['enabled'])
            # A fresh preview draws a new target UUID; only recovery reuses one.
            again = preview_registration_import(root, source, '旧导出')
            self.assertNotEqual(again['new_uuid'], preview['new_uuid'])

    def test_import_source_boundaries_and_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_fresh(root)
            inside = root / 'config/inside.json'
            inside.write_text(json.dumps(self.external_schema2(root)), encoding='utf-8')
            with self.assertRaises(ConfigError):
                preview_registration_import(root, inside, '内部')
            source = root.parent / 'external.json'
            source.write_text(json.dumps(schema1(root, [ok_row(root)])), encoding='utf-8')
            preview = preview_registration_import(root, source, '旧导出')
            source.write_text('{}', encoding='utf-8')
            with self.assertRaises(SourceChanged):
                commit_registration_import(root, preview, {})
            self.assertFalse(list(Path(root).glob('config/profiles/*')))
            self.assertIsNone(read_journal(root))

    def test_registry_change_between_preview_and_commit_blocks(self):
        """Conflict choices were computed against the previewed registry; a
        registry change in between must force a fresh preview, not merge."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_fresh(root)
            write_registry(root, {'ok-ww': {'installation_root': 'C:/current'}})
            source = root.parent / 'external.json'
            source.write_text(json.dumps(schema1(root, [ok_row('C:/imported')])), encoding='utf-8')
            preview = preview_registration_import(root, source, '旧导出')
            self.assertIn('registry_sha256', preview)
            write_registry(root, {'ok-ww': {'installation_root': 'C:/current'},
                                  'bettergi': {'installation_root': 'C:/bg'}}, revision=2)
            with self.assertRaisesRegex(SourceChanged, '全局登记已变化'):
                commit_registration_import(root, preview, {'ok-ww': 'keep'})
            self.assertFalse(list(Path(root).glob('config/profiles/*')))
            self.assertIsNone(read_journal(root))

    def test_import_schema1_conflict_import_choice_updates_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_fresh(root)
            write_registry(root, {'ok-ww': {'installation_root': 'C:/current'}})
            source = root.parent / 'external.json'
            source.write_text(json.dumps(schema1(root, [ok_row('C:/imported')])), encoding='utf-8')
            preview = preview_registration_import(root, source, '旧导出')
            result = commit_registration_import(root, preview, {'ok-ww': 'import'})
            self.assertEqual(RegistrationStore(root).read_required()['assistants']['ok-ww'],
                             {'installation_root': 'C:/imported'})
            self.assertTrue(list(backup_root(root).glob('*/complete.json')))
            self.assertIsNone(read_journal(root))


if __name__ == '__main__':
    unittest.main()
