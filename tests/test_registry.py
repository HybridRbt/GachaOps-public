import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gachaops.registry import (RegistrationStore, RegistryMissing, RevisionConflict, empty_registry,
                               validate_registry)
from gachaops.profiles import default_profile, run_content
from gachaops.configuration import compose_configuration, registration_of
from gachaops.registration import inspect_registration
from gachaops.planner import ConfigError


def inspection_for(adapter_id, root, **fields):
    with patch('gachaops.registration.prepare_assistant', return_value={'blockers': []}):
        return inspect_registration(adapter_id, dict({'installation_root': str(root)}, **fields))


class RegistryTests(unittest.TestCase):
    def test_validate_rejects_unknown_schema_kind_fields_and_bad_paths(self):
        base = {'schema_version': 1, 'kind': 'assistant_registry', 'revision': 1,
                'assistants': {'ok-ww': {'installation_root': 'C:/Assistants/ok'}}}
        for mutation in ({'schema_version': 2}, {'kind': 'other'}, {'revision': 0}, {'extra': 1},
                         {'assistants': []}, {'assistants': {'no-such-adapter': {'installation_root': 'C:/x'}}},
                         {'assistants': {'ok-ww': {}}},
                         {'assistants': {'ok-ww': {'installation_root': 'relative/path'}}},
                         {'assistants': {'zzz-onedragon': {'installation_root': 'C:/zzz'}}},
                         {'assistants': {'ok-ww': {'installation_root': 'C:/ok', 'game_path': 'C:/game.exe'}}}):
            with self.subTest(mutation=sorted(mutation)), self.assertRaises(ConfigError):
                validate_registry(dict(base, **mutation))
        validate_registry(dict(base, assistants={'zzz-onedragon': {'installation_root': 'C:/zzz',
                                                                  'game_path': 'C:/zzz/game.exe'}}))

    def test_missing_registry_is_a_requirement_and_create_only_fills_a_fresh_root(self):
        with tempfile.TemporaryDirectory() as temp:
            store = RegistrationStore(temp)
            self.assertIsNone(store.read())
            with self.assertRaises(RegistryMissing): store.read_required()
            created = store.create()
            self.assertEqual(created, empty_registry())
            with self.assertRaises(ConfigError): store.create()

    def test_commit_publishes_one_adapter_backs_up_and_conflicts_on_stale_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RegistrationStore(temp)
            store.create()
            inspection = inspection_for('bettergi', root)
            updated = store.commit('bettergi', inspection, 1)
            self.assertEqual(updated['revision'], 2)
            self.assertEqual(updated['assistants']['bettergi']['installation_root'], str(root.resolve()))
            self.assertNotIn('game_start', updated['assistants']['bettergi'])
            with self.assertRaises(RevisionConflict): store.commit('bettergi', inspection, 1)
            mismatched = inspection_for('ok-ww', root)
            with self.assertRaises(ConfigError): store.commit('bettergi', mismatched, 2)
            other = store.commit('ok-ww', mismatched, 2)
            self.assertEqual(other['revision'], 3)
            self.assertEqual(set(other['assistants']), {'bettergi', 'ok-ww'})
            backups = list((root / 'backups/registry').glob('*.json'))
            self.assertEqual(len(backups), 2)
            with self.assertRaises(ConfigError): store.commit('bettergi', 'not-an-inspection', 3)

    def test_commit_failure_leaves_previous_file_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RegistrationStore(temp); store.create()
            first = store.commit('bettergi', inspection_for('bettergi', root), 1)
            with patch('gachaops.registry.save', side_effect=PermissionError('fixture')):
                with self.assertRaises(PermissionError):
                    store.commit('ok-ww', inspection_for('ok-ww', root), 2)
            self.assertEqual(store.read(), first)

    def test_corrupt_registry_is_an_error_never_rebuilt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RegistrationStore(temp); store.create()
            store.path.write_text('{ broken', encoding='utf-8')
            with self.assertRaises(ConfigError): store.read()
            with self.assertRaises(ConfigError):
                store.commit('bettergi', inspection_for('bettergi', root), 1)


class ComposeTests(unittest.TestCase):
    def test_compose_merges_registry_and_blocks_only_selected_unregistered(self):
        registry = {'schema_version': 1, 'kind': 'assistant_registry', 'revision': 3, 'assistants': {
            'ok-ww': {'installation_root': 'C:/Assistants/ok'},
            'zzz-onedragon': {'installation_root': 'C:/zzz', 'game_path': 'C:/zzz/game.exe'}}}
        data = default_profile()
        order = {row['adapter']: row for row in data['assistants']}
        order['ok-ww']['enabled'] = True
        order['zzz-onedragon']['enabled'] = True
        order['bettergi']['enabled'] = False  # unselected and unregistered: must not block
        composed = compose_configuration(data, registry)
        self.assertEqual(composed['schema_version'], 1)
        self.assertEqual(composed['repeat_limit'], 0)
        merged = {row['adapter']: row for row in composed['assistants']}
        self.assertEqual(merged['ok-ww']['installation_root'], 'C:/Assistants/ok')
        self.assertEqual(merged['zzz-onedragon']['game_path'], 'C:/zzz/game.exe')
        self.assertNotIn('installation_root', merged['bettergi'])
        # Inputs stay untouched.
        self.assertNotIn('installation_root', order['ok-ww'])
        self.assertEqual(registry['revision'], 3)
        order['bettergi']['enabled'] = True
        with self.assertRaises(ConfigError) as caught:
            compose_configuration(data, registry)
        self.assertIn('genshin', str(caught.exception))

    def test_compose_rejects_invalid_inputs(self):
        registry = {'schema_version': 1, 'kind': 'assistant_registry', 'revision': 1, 'assistants': {}}
        data = default_profile()
        data['assistants'][0]['installation_root'] = 'C:/smuggled'
        with self.assertRaises(ConfigError):
            compose_configuration(data, registry)
        with self.assertRaises(ConfigError):
            compose_configuration(default_profile(), dict(registry, revision=0))
        self.assertEqual(registration_of(registry, 'ok-ww'), {})
        self.assertEqual(registration_of(registry, 'ok-ww'), registration_of(registry, 'nope'))

    def test_registered_rows_survive_profile_round_trip_and_compose(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = RegistrationStore(temp); registry = store.create()
            registry = store.commit('bettergi', inspection_for('bettergi', root), 1)
            profile = default_profile()
            row = next(r for r in profile['assistants'] if r['adapter'] == 'bettergi')
            row['enabled'] = True
            composed = compose_configuration(profile, registry)
            merged = next(r for r in composed['assistants'] if r['adapter'] == 'bettergi')
            self.assertTrue(merged['enabled'])
            self.assertEqual(merged['installation_root'], str(root.resolve()))
            # The stored document keeps run fields only; nothing leaks back.
            document = {'schema_version': 2, 'kind': 'assistant_profile', 'revision': 1,
                        'repeat_limit': profile['repeat_limit'], 'assistants': profile['assistants']}
            self.assertEqual(run_content(document)['assistants'], profile['assistants'])
