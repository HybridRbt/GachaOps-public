from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch


class CatalogTests(unittest.TestCase):
    def test_builtin_identity_capabilities_and_unknown_rejection(self):
        from gachaops.adapter_catalog import list_adapters, get_adapter
        specs = list_adapters()
        self.assertIsInstance(specs, tuple)
        self.assertEqual({s.adapter_id for s in specs}, {'ok-ww', 'ok-nte', 'maa-end',
            'maa-gf2', 'march7', 'zzz-onedragon', 'bettergi'})
        self.assertEqual({s.adapter_id for s in specs if 'manual' in s.start_modes}, {'ok-ww', 'maa-end'})
        self.assertEqual({s.adapter_id for s in specs if 'game_path' in s.registration_fields}, {'zzz-onedragon'})
        with self.assertRaises(ValueError): get_adapter('unknown')
        with self.assertRaises(FrozenInstanceError): specs[0].display_name = 'changed'

    def test_all_seven_prepare_routes_preserve_outputs(self):
        from gachaops.adapter_catalog import get_adapter
        from gachaops import assistants
        routes = [('zzz-onedragon', 'onedragon.prepare'), ('maa-end', 'maaend.prepare'),
            ('maa-gf2', 'maagf2.prepare'), ('march7', 'march7.prepare'), ('bettergi', 'bettergi.prepare'),
            ('ok-ww', '_prepare_ok_assistant'), ('ok-nte', '_prepare_ok_assistant')]
        for identity, target in routes:
            with self.subTest(adapter=identity), patch('gachaops.assistants.' + target, return_value={'fixture': identity}):
                self.assertEqual(assistants.prepare_assistant({'adapter': identity}), {'fixture': identity})

    def test_synthetic_declaration_is_not_added_to_production_and_duplicates_fail(self):
        from gachaops.adapter_catalog import AdapterSpec, validate_catalog, list_adapters
        fixture = AdapterSpec('fixture', 'fixture-game', 'Fixture', 'https://example.invalid',
            ('installation_root',), ('assistant',), lambda row: {'ok': True})
        catalog = validate_catalog((*list_adapters(), fixture))
        self.assertEqual(catalog[-1].prepare({}), {'ok': True})
        self.assertFalse(any(s.adapter_id == 'fixture' for s in list_adapters()))
        with self.assertRaises(ValueError): validate_catalog((fixture, fixture))
