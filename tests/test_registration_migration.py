"""Offline migration/import regressions with fixture directories only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from gachaops.migration_journal import (JOURNAL_KIND, archive_journal, backup_directory,
                                        backup_root, journal_path, read_journal)
from gachaops.planner import ConfigError
from gachaops.profiles import ProfileStore
from gachaops.recovery import save
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


def absolute(*parts):
    """Portable absolute fixture path: a Windows drive letter is not absolute
    on POSIX, so valid fixtures build from a host-absolute base instead."""
    return str(Path(tempfile.gettempdir()).resolve().joinpath(*parts))


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
            write_registry(root, {'ok-ww': {'installation_root': absolute('current')}})
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
            write_registry(root, {'ok-ww': {'installation_root': absolute('kept')}})
            write_profile(root, UUID_A, schema1(root, [
                {'id': 'genshin', 'adapter': 'bettergi', 'installation_root': str(root), 'enabled': False}]),
                name='A')
            preview = preview_registration_migration(root)
            flagged = {entry['adapter']: entry['conflict'] for entry in preview['adapters']}
            self.assertEqual(flagged, {'ok-ww': False, 'bettergi': False})
            commit_registration_migration(root, preview, {})
            registry = RegistrationStore(root).read_required()
            self.assertEqual(registry['assistants']['ok-ww'], {'installation_root': absolute('kept')})

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
        write_registry(root, {'bettergi': {'installation_root': absolute('kept')}})
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
            self.assertEqual(registry['assistants']['bettergi'], {'installation_root': absolute('kept')})
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
        write_registry(root, {'ok-ww': {'installation_root': absolute('current')}})
        source = root.parent / 'external-import.json'
        source.write_text(json.dumps(schema1(root, [ok_row(absolute('imported'), enabled=True)])),
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
                             {'installation_root': absolute('current')})
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
            write_registry(root, {'ok-ww': {'installation_root': absolute('current')},
                                  'bettergi': {'installation_root': absolute('bg')}})
            source = root.parent / 'external-schema1.json'
            source.write_text(json.dumps(schema1(root, [
                ok_row(absolute('imported'), enabled=True),
                {'id': 'genshin', 'adapter': 'bettergi', 'installation_root': absolute('bg'), 'enabled': False},
                {'id': 'nte', 'adapter': 'ok-nte', 'installation_root': absolute('nte'), 'enabled': False}])),
                encoding='utf-8')
            preview = preview_registration_import(root, source, '旧导出')
            self.assertEqual(preview['schema'], 1)
            actions = {entry['adapter']: entry['action'] for entry in preview['adapters']}
            self.assertEqual(actions, {'ok-ww': 'conflict', 'bettergi': 'same', 'ok-nte': 'new'})
            with self.assertRaises(ConfigError):
                commit_registration_import(root, preview, {})
            result = commit_registration_import(root, preview, {'ok-ww': 'keep'})
            assistants = RegistrationStore(root).read_required()['assistants']
            self.assertEqual(assistants['ok-ww'], {'installation_root': absolute('current')})
            self.assertEqual(assistants['ok-nte'], {'installation_root': absolute('nte')})
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
            write_registry(root, {'ok-ww': {'installation_root': absolute('current')}})
            source = root.parent / 'external.json'
            source.write_text(json.dumps(schema1(root, [ok_row(absolute('imported'))])), encoding='utf-8')
            preview = preview_registration_import(root, source, '旧导出')
            self.assertIn('registry_sha256', preview)
            write_registry(root, {'ok-ww': {'installation_root': absolute('current')},
                                  'bettergi': {'installation_root': absolute('bg')}}, revision=2)
            with self.assertRaisesRegex(SourceChanged, '全局登记已变化'):
                commit_registration_import(root, preview, {'ok-ww': 'keep'})
            self.assertFalse(list(Path(root).glob('config/profiles/*')))
            self.assertIsNone(read_journal(root))

    def test_import_schema1_conflict_import_choice_updates_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_fresh(root)
            write_registry(root, {'ok-ww': {'installation_root': absolute('current')}})
            source = root.parent / 'external.json'
            source.write_text(json.dumps(schema1(root, [ok_row(absolute('imported'))])), encoding='utf-8')
            preview = preview_registration_import(root, source, '旧导出')
            result = commit_registration_import(root, preview, {'ok-ww': 'import'})
            self.assertEqual(RegistrationStore(root).read_required()['assistants']['ok-ww'],
                             {'installation_root': absolute('imported')})
            self.assertTrue(list(backup_root(root).glob('*/complete.json')))
            self.assertIsNone(read_journal(root))


class IncompleteCandidateTests(unittest.TestCase):
    """A partial registration must stay 待接入, never dead-end the commit."""

    def test_incomplete_zzz_single_candidate_is_not_auto_adopted_and_migration_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_profile(root, UUID_A, schema1(root, [
                {'id': 'zenless', 'adapter': 'zzz-onedragon', 'installation_root': str(root),
                 'enabled': False}]), name='旧z')
            preview = preview_registration_migration(root)
            self.assertEqual(preview['adapters'], [])
            commit_registration_migration(root, preview, {})
            registry = RegistrationStore(root).read_required()
            self.assertNotIn('zzz-onedragon', registry['assistants'])
            # Run fields and selection survive; the row is not auto-disabled.
            document = ProfileStore(root).load_profile(UUID_A)
            self.assertFalse(document['assistants'][0]['enabled'])
            self.assertEqual(document['assistants'][0]['timeout_seconds'], 1800)
            self.assertEqual(startup_state(root)['status'], 'ready')
            # A complete candidate for the same adapter still auto-adopts.
            write_profile(root, UUID_B, schema1(root, [
                {'id': 'zenless2', 'adapter': 'zzz-onedragon', 'installation_root': str(root),
                 'game_path': str(root / 'ZenlessZoneZero.exe'), 'enabled': True}]), name='新z')
            preview = preview_registration_migration(root)
            commit_registration_migration(root, preview, {})
            registry = RegistrationStore(root).read_required()
            self.assertEqual(registry['assistants']['zzz-onedragon'],
                             {'installation_root': str(root),
                              'game_path': str(root / 'ZenlessZoneZero.exe')})

    def test_incomplete_import_candidate_is_not_applied_and_import_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_fresh(root)
            source = root.parent / 'external-zzz.json'
            source.write_text(json.dumps(schema1(root, [
                {'id': 'zenless', 'adapter': 'zzz-onedragon',
                 'installation_root': str(root), 'enabled': False}])), encoding='utf-8')
            preview = preview_registration_import(root, source, '导入z')
            self.assertEqual(preview['adapters'], [])
            result = commit_registration_import(root, preview, {})
            self.assertEqual(RegistrationStore(root).read_required()['assistants'], {})
            document = ProfileStore(root).load_profile(result['profile_id'])
            self.assertFalse(document['assistants'][0]['enabled'])
            self.assertIsNone(read_journal(root))


class JournalArchiveRaceTests(unittest.TestCase):
    """startup/preview archive a completed journal; a concurrent window may
    replace it with a new prepared transaction between the read and the
    archive. The stale archive must never unlink the newer journal."""

    @staticmethod
    def journal(backup, state, transaction):
        return {'schema_version': 1, 'kind': JOURNAL_KIND, 'state': state,
                'transaction': transaction, 'backup': backup,
                'manifest_sha256': '0' * 64, 'created_at': '2026-01-01T00:00:00+00:00'}

    @staticmethod
    def interleaved_archive(root):
        """Window B archives the complete journal and its new import is
        interrupted right after publishing: a prepared journal sits in the
        journal file. Runs without taking the config lock so it can execute
        inside the function under test."""
        from gachaops import registration_migration
        real_archive = registration_migration.archive_journal
        def stale_archive(data_root, journal):
            real_archive(data_root, journal)
            save(journal_path(root), JournalArchiveRaceTests.journal(
                'd' * 32, 'prepared', 'import'))
        return stale_archive

    def test_startup_state_surfaces_a_newer_prepared_journal_instead_of_unlinking_it(self):
        from gachaops import registration_migration
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_fresh(root)
            save(journal_path(root), self.journal('a' * 32, 'complete', 'migration'))
            with patch.object(registration_migration, 'archive_journal',
                              self.interleaved_archive(root)):
                state = startup_state(root)
            self.assertEqual(state, {'status': 'prepared', 'transaction': 'import'})
            journal = read_journal(root)
            self.assertIsNotNone(journal)
            self.assertEqual(journal['backup'], 'd' * 32)

    def test_migration_preview_surfaces_a_newer_prepared_journal_instead_of_unlinking_it(self):
        from gachaops import registration_migration
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_fresh(root)
            save(journal_path(root), self.journal('b' * 32, 'complete', 'migration'))
            with patch.object(registration_migration, 'archive_journal',
                              self.interleaved_archive(root)):
                with self.assertRaises(ConfigError) as caught:
                    preview_registration_migration(root)
            self.assertIn('未完成的迁移/导入事务', str(caught.exception))
            journal = read_journal(root)
            self.assertIsNotNone(journal)
            self.assertEqual(journal['backup'], 'd' * 32)


CONTINUE_PROCESS_SCRIPT = '''\
"""Window A: observe the prepared journal, then pause before taking locks."""
import os
import sys
import time
from pathlib import Path

repo, root, sent_read, sent_go, sent_result = (Path(arg) for arg in sys.argv[1:6])
sys.path.insert(0, str(repo))
from gachaops import registration_migration
from gachaops.planner import ConfigError

def finish(message):
    # Atomic publish: the parent polls for the sentinel, so the result must
    # never be observable half-written.
    temporary = sent_result.with_name(sent_result.name + '.tmp')
    temporary.write_text(message, encoding='utf-8')
    os.replace(temporary, sent_result)

real_read = registration_migration.read_journal
paused = {'done': False}

def read_then_pause(data_root):
    journal = real_read(data_root)
    if not paused['done']:
        paused['done'] = True
        sent_read.write_text('read', encoding='utf-8')
        deadline = time.monotonic() + 120
        while not sent_go.exists():
            if time.monotonic() > deadline:
                finish('TIMEOUT')
                raise SystemExit(3)
            time.sleep(0.02)
    return journal  # every later call re-reads the real on-disk state

registration_migration.read_journal = read_then_pause
try:
    registration_migration.continue_registration_transaction(root)
    finish('CONTINUED')
except ConfigError as error:
    finish('REFUSED:' + str(error))
except BaseException as error:  # surfaced verbatim for diagnosis
    finish('ERROR:' + repr(error))
'''


class ContinueRegistrationRaceTests(unittest.TestCase):
    """Real cross-process interleaving on the continue entry point: window A
    reads the prepared journal before both locks; window B completes that
    transaction and an interrupted new import leaves a newer prepared journal.
    A must stop loudly and never archive its stale journal over the newer one
    (review thread PRRT_kwDOVetvVc67tj-G, continue residual gap)."""

    def _wait_for(self, sentinel, process, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sentinel.exists():
                content = sentinel.read_text(encoding='utf-8')
                if content:
                    return content
            if process.poll() is not None:
                _, err = process.communicate(timeout=10)
                raise AssertionError(f'helper process exited early: {err!r}')
            time.sleep(0.02)
        raise AssertionError('helper process did not reach its checkpoint in time')

    def test_continue_racing_a_newer_transaction_preserves_it_and_stays_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            ImportContinueTests.prepare_interrupted_import(self, root, 0)
            old_backup = read_journal(root)['backup']
            repo = Path(__file__).resolve().parents[1]
            sent_read = root / 'a-read.sentinel'
            sent_go = root / 'a-go.sentinel'
            sent_result = root / 'a-result.sentinel'
            script = root / 'continue_a.py'
            script.write_text(CONTINUE_PROCESS_SCRIPT, encoding='utf-8')
            process = subprocess.Popen(
                [sys.executable, '-X', 'utf8', str(script), str(repo), str(root),
                 str(sent_read), str(sent_go), str(sent_result)],
                cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self._wait_for(sent_read, process)
                # Window B (this process, real locks and files): finish the old
                # transaction, then leave a newer import prepared and unpublished.
                continue_registration_transaction(root)
                self.assertIsNone(read_journal(root))
                from gachaops import registration_migration
                source = root.parent / 'external-import.json'
                second = preview_registration_import(root, source, '导入B')
                real_publish = registration_migration._publish_targets

                def flaky(root_arg, targets):
                    real_publish(root_arg, targets[:0])
                    raise PermissionError('fixture interruption')
                with patch.object(registration_migration, '_publish_targets', flaky):
                    with self.assertRaises(PermissionError):
                        commit_registration_import(root, second, {'ok-ww': 'keep'})
                newer = read_journal(root)
                self.assertIsNotNone(newer)
                newer_backup = newer['backup']
                self.assertNotEqual(newer_backup, old_backup)
                self.assertTrue((backup_root(root) / newer_backup / 'manifest.json').is_file())
                # Window A resumes holding both locks with its stale journal.
                sent_go.write_text('go', encoding='ascii')
                result = self._wait_for(sent_result, process)
                self.assertTrue(result.startswith('REFUSED'), result)
                self.assertIn('事务已变化', result)
                self.assertEqual(process.wait(timeout=30), 0)
                # The newer transaction and its staged backup are untouched.
                intact = read_journal(root)
                self.assertIsNotNone(intact)
                self.assertEqual(intact['backup'], newer_backup)
                self.assertTrue((backup_root(root) / newer_backup / 'manifest.json').is_file())
                # The newest transaction still recovers forward; a repeated
                # continue reports nothing pending instead of degrading.
                recovered = continue_registration_transaction(root)
                self.assertEqual(recovered['kind'], 'import')
                self.assertEqual(recovered['backup'], newer_backup)
                self.assertIsNone(read_journal(root))
                self.assertTrue((backup_root(root) / newer_backup / 'complete.json').is_file())
                self.assertEqual(startup_state(root)['status'], 'ready')
                with self.assertRaises(ConfigError):
                    continue_registration_transaction(root)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=30)


class JournalBackupValidationTests(unittest.TestCase):
    """A forged or damaged journal's backup value must not point backup
    reads or writes outside this data root's backup directory."""

    @staticmethod
    def journal(backup, state='complete'):
        return {'schema_version': 1, 'kind': JOURNAL_KIND, 'state': state,
                'transaction': 'migration', 'backup': backup,
                'manifest_sha256': '0' * 64, 'created_at': '2026-01-01T00:00:00+00:00'}

    def test_read_journal_rejects_non_canonical_backup_values(self):
        # The only value the program writes is uuid4().hex; every other
        # spelling is a forged journal and is refused outright.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_fresh(root)
            for value in ('../../outside', 'C:/outside/x', str(Path(temp) / 'elsewhere'),
                          'nested/value', 'a' * 31, 'a' * 33, 'A' * 32, '0' * 32 + ' '):
                save(journal_path(root), self.journal(value))
                with self.subTest(value=value):
                    with self.assertRaises(ConfigError):
                        read_journal(root)
                (journal_path(root)).unlink()

    def test_relative_backup_escape_is_refused_for_archiving(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_fresh(root)
            escape = '../../outside-' + uuid.uuid4().hex
            save(journal_path(root), self.journal(escape))
            with self.assertRaises(ConfigError):
                read_journal(root)
            with self.assertRaises(ConfigError):
                archive_journal(root, self.journal(escape))
            self.assertFalse((root / escape.split('/')[-1] / 'complete.json').exists())

    def test_absolute_backup_value_is_refused_for_archiving(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(temp)
            initialize_fresh(root)
            save(journal_path(root), self.journal(str(Path(elsewhere) / 'abs')))
            with self.assertRaises(ConfigError):
                read_journal(root)
            with self.assertRaises(ConfigError):
                archive_journal(root, self.journal(str(Path(elsewhere) / 'abs')))
            self.assertFalse((Path(elsewhere) / 'abs' / 'complete.json').exists())

    def test_continue_read_stays_inside_the_backup_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_fresh(root)
            escape = '../../outside-' + uuid.uuid4().hex
            manifest = json.dumps({'kind': 'registration_migration', 'sources': [],
                                   'targets': [], 'profiles': []}).encode('utf-8')
            # The '..' spelling lands at the data root itself: outside the
            # backup root (the containment boundary) but inside the data root.
            escaped = (backup_root(root) / escape)
            escaped.mkdir(parents=True, exist_ok=True)
            (escaped / 'manifest.json').write_bytes(manifest)
            save(journal_path(root), self.journal(escape, state='prepared') | {
                'manifest_sha256': hashlib.sha256(manifest).hexdigest()})
            # Refused at the schema layer before any lock or backup access:
            # the unfixed product reads the planted manifest from outside the
            # backup root and fails later with a different message.
            with self.assertRaisesRegex(ConfigError, 'invalid migration journal schema'):
                continue_registration_transaction(root)
            # Nothing is deleted or rewritten: the forged journal file stays
            # exactly as it was found.
            self.assertTrue(journal_path(root).exists())
            self.assertEqual(json.loads(journal_path(root).read_text(
                encoding='utf-8-sig'))['backup'], escape)
            self.assertFalse((escaped / 'complete.json').exists())
            self.assertEqual((escaped / 'manifest.json').read_bytes(), manifest)

    def test_symlinked_backup_directory_is_refused_for_archiving(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(temp)
            initialize_fresh(root)
            (root / 'backups' / 'registration-migrations').mkdir(parents=True)
            target = Path(elsewhere) / 'real'
            target.mkdir()
            link = backup_root(root) / ('a' * 32)
            try:
                os.symlink(target, link, target_is_directory=True)
            except OSError:
                self.skipTest(f'symlink creation unavailable: {sys.exc_info()[1]}')
            save(journal_path(root), self.journal('a' * 32))
            with self.assertRaises(ConfigError):
                archive_journal(root, self.journal('a' * 32))
            self.assertFalse((target / 'complete.json').exists())

    def test_canonical_hex_backup_still_archives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_fresh(root)
            backup = 'c' * 32
            (backup_root(root) / backup).mkdir(parents=True)
            save(journal_path(root), self.journal(backup))
            archive_journal(root, self.journal(backup))
            self.assertTrue((backup_root(root) / backup / 'complete.json').is_file())
            self.assertFalse(journal_path(root).exists())

    def test_backup_directory_rejects_non_canonical_names(self):
        # backup_directory must enforce the hex name itself, not trust its
        # caller to have passed the journal's schema check first.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_fresh(root)
            with self.assertRaises(ConfigError):
                backup_directory(root, '.')
            with self.assertRaises(ConfigError):
                backup_directory(root, str(backup_root(root)))
            with self.assertRaises(ConfigError):
                backup_directory(root, 'A' * 32)
            self.assertEqual(backup_directory(root, 'c' * 32),
                             backup_root(root) / ('c' * 32))


class ManifestRowPathValidationTests(unittest.TestCase):
    """A manifest is a local, forgeable file: its digest proves only that it
    was not changed, never that it is trustworthy. Every row path must stay
    inside its own staged subdirectory before any backup file is read."""

    def prepare_interrupted(self, root):
        """A real staged transaction (manifest, copies, prepared journal)."""
        return MigrationContinueTests.prepare_interrupted(self, root)

    @staticmethod
    def _rewrite_manifest(root, mutate):
        """Mutate one row of the staged manifest and re-sign the journal, so
        only the row validation itself can refuse the document."""
        manifest_path = next(backup_root(root).glob('*/manifest.json'))
        manifest = json.loads(manifest_path.read_bytes().decode('utf-8'))
        mutate(manifest)
        payload = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
        manifest_path.write_bytes(payload)
        journal = json.loads(journal_path(root).read_text(encoding='utf-8-sig'))
        journal['manifest_sha256'] = hashlib.sha256(payload).hexdigest()
        save(journal_path(root), journal)

    @staticmethod
    def _recording_reads():
        """Patch Path.read_bytes to record every resolved path it reads."""
        real_read = Path.read_bytes
        reads = []

        def recording(path):
            reads.append(path.resolve())
            return real_read(path)
        return patch.object(Path, 'read_bytes', recording), reads

    def test_manifest_row_absolute_path_is_refused_before_any_read(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(temp)
            self.prepare_interrupted(root)
            outside = Path(elsewhere) / 'abs-source.json'
            outside.write_bytes(b'outside payload')
            row_digest = hashlib.sha256(b'outside payload').hexdigest()
            guard, reads = self._recording_reads()
            with guard:
                self._rewrite_manifest(
                    root, lambda m: m['sources'][-1].update(
                        {'path': str(outside), 'sha256': row_digest}))
                with self.assertRaisesRegex(ConfigError, '非法备份路径'):
                    continue_registration_transaction(root)
                # Across the whole call nothing outside the data root was read.
                self.assertNotIn(outside.resolve(), reads)
            self.assertEqual(outside.read_bytes(), b'outside payload')
            self.assertEqual(read_journal(root)['state'], 'prepared')
            self.assertFalse(list(backup_root(root).glob('*/complete.json')))

    def test_manifest_row_relative_escape_is_refused_before_any_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare_interrupted(root)
            # Four '..' from the targets directory land at the data root:
            # outside the staged subdirectory, the boundary that matters.
            escape = '../../../../outside-' + uuid.uuid4().hex
            escaped = root / escape.split('/')[-1]
            escaped.write_bytes(b'planted target')
            row_digest = hashlib.sha256(b'planted target').hexdigest()
            guard, reads = self._recording_reads()
            with guard:
                self._rewrite_manifest(
                    root, lambda m: m['targets'][0].update(
                        {'path': escape, 'sha256': row_digest}))
                with self.assertRaisesRegex(ConfigError, '非法备份路径'):
                    continue_registration_transaction(root)
                self.assertNotIn(escaped.resolve(), reads)
            self.assertEqual(escaped.read_bytes(), b'planted target')
            self.assertEqual(read_journal(root)['state'], 'prepared')

    def test_manifest_row_symlink_escape_is_refused_before_any_read(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(temp)
            self.prepare_interrupted(root)
            outside = Path(elsewhere) / 'linked-target.json'
            outside.write_bytes(b'linked payload')
            row_digest = hashlib.sha256(b'linked payload').hexdigest()
            targets_dir = next(backup_root(root).glob('*/manifest.json')).parent / 'targets'
            link = targets_dir / ('b' * 32 + '.json')
            try:
                os.symlink(outside, link, target_is_directory=False)
            except OSError:
                self.skipTest(f'symlink creation unavailable: {sys.exc_info()[1]}')
            guard, reads = self._recording_reads()
            with guard:
                self._rewrite_manifest(
                    root, lambda m: m['targets'][0].update(
                        {'path': 'b' * 32 + '.json', 'sha256': row_digest}))
                with self.assertRaisesRegex(ConfigError, '非法备份路径'):
                    continue_registration_transaction(root)
                self.assertNotIn(outside.resolve(), reads)
            self.assertEqual(outside.read_bytes(), b'linked payload')
            self.assertEqual(read_journal(root)['state'], 'prepared')


if __name__ == '__main__':
    unittest.main()
