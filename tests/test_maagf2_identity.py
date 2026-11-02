"""Framework selection must follow the entry executable, never stale MFA files."""
import hashlib
import ctypes
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

from gachaops import maagf2
from gachaops.assistants import check_assistants, prepare_assistant


class VersionResourceTests(unittest.TestCase):
    def query_product(self, names, translations_size=None):
        translations = (ctypes.c_ushort * (2 * len(names)))(
            *(v for i in range(len(names)) for v in (i, 1200)))
        strings = [ctypes.create_unicode_buffer(n) if n is not None else None for n in names]
        def query(buffer, key, ptr, length):
            if key == '\\VarFileInfo\\Translation':
                storage = translations
                count = ctypes.sizeof(translations) if translations_size is None else translations_size
            else:
                index = int(key.split('\\')[2][:4], 16)
                storage = strings[index]
                if storage is None:
                    return 0
                count = len(storage)
            ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(storage)
            ctypes.cast(length, ctypes.POINTER(ctypes.c_uint))[0] = count
            return 1
        api = SimpleNamespace(GetFileVersionInfoSizeW=Mock(return_value=4096),
                              GetFileVersionInfoW=Mock(return_value=1), VerQueryValueW=Mock(side_effect=query))
        with patch.object(maagf2, 'os', SimpleNamespace(name='nt')), \
             patch.object(ctypes, 'WinDLL', return_value=api, create=True):
            return maagf2.executable_product_name(Path('fixture.exe'))

    def test_real_mfa_style_extra_trailing_nul_is_accepted(self):
        # Actual MFA VersionInfo reports a second terminating NUL.
        self.assertEqual(self.query_product(['MFAAvalonia\0']), 'MFAAvalonia')

    def test_conflicting_missing_or_malformed_translations_are_rejected(self):
        for names, size in ((['MFAAvalonia', 'mxu'], None), ([None], None),
                            ([''], None), (['MFA\0Avalonia'], None), (['mxu'], 3)):
            with self.subTest(names=names, size=size), self.assertRaisesRegex(ValueError, '身份'):
                self.query_product(names, size)

    def test_consistent_translations_and_mxu_name_are_read(self):
        self.assertEqual(self.query_product(['MFAAvalonia', 'MFAAvalonia']), 'MFAAvalonia')
        self.assertEqual(self.query_product(['mxu']), 'mxu')


class FrameworkIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in ('MaaGF2Exilium.exe', 'libs/MFAAvalonia.Core.dll',
                     'runtimes/win-x64/native/MaaFramework.dll', 'game.exe'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes('autostart quit-after-run instance'.encode('utf-16le'))
        (self.root / 'interface.json').write_text(json.dumps({'version': 'v2.7.3', 'controller': [
            {'name': '桌面端', 'type': 'Win32', 'win32': {'class_regex': '.*', 'window_regex': '^EXILIUM$'}}]}))
        self.instance = self.root / 'config/instances/default.json'
        self.instance.parent.mkdir(parents=True)
        self.instance.write_text(json.dumps({'CurrentController': 1, 'BeforeTask': 'StartupSoftware',
                                            'SoftwarePath': str(self.root / 'game.exe')}))
        (self.root / 'config/config.json').write_text('{}')
        self.row = {'id': 'gf2', 'adapter': 'maa-gf2', 'installation_root': str(self.root), 'enabled': True}
        for target, value in (
                ('SUPPORTED_CORE_SHA256', hashlib.sha256((self.root / 'libs/MFAAvalonia.Core.dll').read_bytes()).hexdigest()),
                ('elevation_report', {'elevated': True}), ('desktop_report', {'status': 'observed_available'}),
                ('find_matching_processes', {'status': 'observed', 'matches': []})):
            context = patch.object(maagf2, target, value) if isinstance(value, str) else patch.object(maagf2, target, return_value=value)
            context.start()
            self.addCleanup(context.stop)

    def test_mxu_with_verified_legacy_files_is_blocked_before_instance_read(self):
        self.instance.write_text('invalid old preset', encoding='utf-8')
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(maagf2, 'executable_product_name', return_value='mxu', create=True):
            checked = check_assistants({'assistants': [self.row]})['assistants'][0]
        self.assertEqual(checked['status'], 'blocked')
        self.assertIn('MXU', checked['error'])
        self.assertIn('取消勾选', checked['error'])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_pure_mxu_does_not_require_legacy_core_or_config_to_explain_error(self):
        (self.root / 'libs/MFAAvalonia.Core.dll').unlink()
        (self.root / 'config/config.json').unlink()
        with patch.object(maagf2, 'executable_product_name', return_value='MXU', create=True):
            with self.assertRaisesRegex(ValueError, 'MXU'):
                prepare_assistant(self.row)

    def test_unknown_or_unreadable_identity_does_not_fall_back_to_known_core(self):
        for product in ('', 'Other Assistant', 'MFAAvalonia MXU'):
            with self.subTest(product=product), patch.object(maagf2, 'executable_product_name', return_value=product, create=True):
                with self.assertRaisesRegex(ValueError, '身份'):
                    prepare_assistant(self.row)
        with patch.object(maagf2, 'executable_product_name', side_effect=ValueError('无法核验入口身份'), create=True):
            with self.assertRaisesRegex(ValueError, '身份'):
                prepare_assistant(self.row)

    def test_verified_mfa_preserves_existing_configuration_failure(self):
        with patch.object(maagf2, 'executable_product_name', return_value='MFAAvalonia', create=True):
            self.assertEqual(prepare_assistant(self.row)['blockers'], [])
            self.instance.write_text(json.dumps({'CurrentController': 0}))
            with self.assertRaisesRegex(ValueError, 'desktop controller'):
                prepare_assistant(self.row)

    def test_entry_replacement_after_preflight_blocks_execution_before_process_creation(self):
        from types import SimpleNamespace
        from gachaops.ok_trial import run_prepared
        with patch.object(maagf2, 'executable_product_name', return_value='MFAAvalonia', create=True):
            current = prepare_assistant(self.row)
        (self.root / 'MaaGF2Exilium.exe').write_bytes(b'mxu replacement')
        (self.root / 'run').mkdir()
        session = SimpleNamespace(record={'run_id': 'fixture'}, finish=lambda *args, **kwargs: None)
        with patch('gachaops.ok_trial.WindowsProcess', side_effect=AssertionError('must not launch')):
            report, _ = run_prepared(current, session, self.root / 'run', SimpleNamespace(offset=0), 1)
        self.assertEqual(report['status'], 'blocked')
        self.assertFalse(report['game_execution'])
        self.assertTrue(report['repeat_blocked'])

    def test_replacement_during_identity_check_cannot_be_stamped_as_verified(self):
        def old_resource_result(exe):
            exe.write_bytes(b'mxu replacement during version query')
            return 'MFAAvalonia'
        with patch.object(maagf2, 'executable_product_name', side_effect=old_resource_result):
            with self.assertRaisesRegex(ValueError, '变化'):
                prepare_assistant(self.row)

    def test_core_and_launch_setting_drift_block_before_process_creation(self):
        from gachaops.compatibility import verify_installation_stamp
        for changed in ('core', 'SoftwarePath', 'BeforeTask', 'CurrentControllerName',
                        'IncludeInGlobalStart', 'AutoDetectOnConnectionFailed', 'extra_instance'):
            with self.subTest(changed=changed):
                before = self.instance.read_bytes()
                core = self.root / 'libs/MFAAvalonia.Core.dll'
                original_core = core.read_bytes()
                with patch.object(maagf2, 'executable_product_name', return_value='MFAAvalonia'):
                    current = prepare_assistant(self.row)
                if changed == 'core':
                    core.write_bytes(b'unknown protocol')
                elif changed == 'extra_instance':
                    (self.instance.parent / 'extra.json').write_text('{}')
                else:
                    data = json.loads(before)
                    data[changed] = 'changed'
                    self.instance.write_text(json.dumps(data))
                try:
                    with self.assertRaisesRegex(ValueError, '变化'):
                        verify_installation_stamp(current)
                finally:
                    core.write_bytes(original_core)
                    self.instance.write_bytes(before)
                    (self.instance.parent / 'extra.json').unlink(missing_ok=True)

    def test_task_items_remain_opaque_to_launch_stamp(self):
        from gachaops.compatibility import verify_installation_stamp
        with patch.object(maagf2, 'executable_product_name', return_value='MFAAvalonia'):
            current = prepare_assistant(self.row)
        data = json.loads(self.instance.read_text())
        data['TaskItems'] = {'opaque': 'new saved daily task selection'}
        self.instance.write_text(json.dumps(data))
        verify_installation_stamp(current)

    def test_missing_startup_option_changed_to_null_is_drift(self):
        from gachaops.compatibility import verify_installation_stamp
        with patch.object(maagf2, 'executable_product_name', return_value='MFAAvalonia'):
            current = prepare_assistant(self.row)
        data = json.loads(self.instance.read_text())
        data['AutoDetectOnConnectionFailed'] = None
        self.instance.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, '变化'):
            verify_installation_stamp(current)

    def test_launch_metadata_changed_during_preparation_cannot_become_baseline(self):
        def update_during_preparation(targets):
            data = json.loads(self.instance.read_text())
            data['SoftwarePath'] = str(self.root / 'another-game.exe')
            self.instance.write_text(json.dumps(data))
            return {'status': 'observed', 'matches': []}
        with patch.object(maagf2, 'executable_product_name', return_value='MFAAvalonia'), \
             patch.object(maagf2, 'find_matching_processes', side_effect=update_during_preparation):
            with self.assertRaisesRegex(ValueError, '变化'):
                prepare_assistant(self.row)
