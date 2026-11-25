"""Synthetic MXU installations and owned-process logs; never run native apps."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from gachaops import maagf2
from gachaops.assistants import prepare_assistant
from gachaops.compatibility import verify_installation_stamp


class MxuPreparationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('gachaops.maagf2_mxu'),
                             'MXU needs a separate exact-contract adapter')
        self.mxu = importlib.import_module('gachaops.maagf2_mxu')
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.components = {
            'MaaGF2Exilium.exe': b'--autostart --instance --quit-after-run',
            'maafw/MaaFramework.dll': b'fixture-framework',
            'python/python.exe': b'fixture-python',
            'agent/main.py': b'fixture-agent',
        }
        for name, content in self.components.items():
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for directory in ('resource/base', 'resource/resource_pc', 'tasks', 'config'):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.write(self.root / 'tasks/daily.json', {'task': [{'name': 'fixture-daily', 'entry': 'Start'}]})
        self.write(self.root / 'resource/base/pipeline.json', {'Start': {}})
        self.game = self.root / 'GF2_Exilium.exe'; self.game.write_bytes(b'fixture-game')
        self.controller = '桌面端-前台'
        self.interface = {
            'interface_version': 2, 'name': 'FixtureGF2', 'version': 'v2.8.3',
            'controller': [{'name': self.controller, 'type': 'Win32', 'win32': {
                'class_regex': '.*', 'window_regex': '^fixture-game$',
                'screencap': 'Foreground', 'mouse': 'Seize', 'keyboard': 'Seize'}}],
            'resource': [{'name': '中文资源(PC端)', 'path': ['resource/base', 'resource/resource_pc']}],
            'agent': {'child_exec': 'python/python.exe', 'child_args': ['-u', './agent/main.py']},
            'import': ['tasks/daily.json'],
        }
        self.instance = {
            'id': 'fixture-id', 'name': 'fixture-default', 'controllerName': self.controller,
            'resourceName': '中文资源(PC端)',
            'savedDevice': {'windowName': 'fixture-game', 'connectedProgramPath': str(self.game)},
            'preActions': [{'id': 'launch', 'enabled': True, 'program': str(self.game),
                'args': '', 'useCmd': False, 'waitForExit': False, 'skipIfRunning': True}],
            'tasks': [{'id': 'task1', 'taskName': 'fixture-daily', 'enabled': True,
                'enabledByController': {self.controller: True},
                'optionValues': {'opaque': {'type': 'input', 'values': {'fixture': 'preserve'}}}}],
            'schedulePolicies': [],
        }
        self.config = {'version': '1.0', 'lastActiveInstanceId': 'fixture-id', 'instances': [self.instance],
            'settings': {'webServerEnabled': False, 'hotkeys': {'globalEnabled': False},
                         'autoRunOnLaunch': False}}
        self.config_path = self.root / 'config/mxu-FixtureGF2.json'
        self.flush()
        self.row = {'id': 'girls-frontline-2', 'adapter': 'maa-gf2',
                    'installation_root': str(self.root), 'enabled': True}
        expected = {name: hashlib.sha256(content).hexdigest() for name, content in self.components.items()}
        for context in (
            patch.object(self.mxu, 'EXPECTED_COMPONENTS', expected),
            patch.object(maagf2, 'executable_product_name', return_value='mxu'),
            patch.object(self.mxu, 'elevation_report', return_value={'elevated': True}),
            patch.object(self.mxu, 'desktop_report', return_value={'status': 'observed_available'}),
            patch.object(self.mxu, 'find_matching_processes', return_value={'status': 'observed', 'matches': []}),
            patch('subprocess.Popen', side_effect=AssertionError('inspection cannot launch anything')),
        ):
            context.start(); self.addCleanup(context.stop)

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def flush(self):
        self.write(self.root / 'interface.json', self.interface)
        self.write(self.config_path, self.config)

    def test_exact_mxu_uses_saved_instance_and_leaves_native_tasks_and_legacy_residue_untouched(self):
        (self.root / 'config/instances').mkdir()
        (self.root / 'config/instances/default.json').write_text('broken legacy configuration')
        self.instance['tasks'].append(dict(self.instance['tasks'][0], id='task2'))
        self.flush(); before = self.config_path.read_bytes()
        prepared = prepare_assistant(self.row)
        self.assertFalse(prepared['blockers'])
        self.assertEqual(prepared['completion_kind'], 'maa-gf2-mxu')
        self.assertEqual(prepared['argv_candidate'], [str(self.root / 'MaaGF2Exilium.exe'),
            '--autostart', '--instance', 'fixture-default', '--quit-after-run'])
        self.assertEqual(prepared['runner_version'], 'v2.8.3')
        self.assertEqual(prepared['mxu_framework_version'], '2.8.1')
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(prepared['task_configuration'], 'assistant_current_saved_preset')
        verify_installation_stamp(prepared)

    def test_unknown_component_or_resource_version_never_uses_legacy_core(self):
        for name in self.components:
            with self.subTest(name=name):
                path = self.root / name; path.write_bytes(b'unverified')
                with self.assertRaisesRegex(ValueError, 'MXU'): prepare_assistant(self.row)
                path.write_bytes(self.components[name])
        self.interface['version'] = 'v2.8.4'; self.flush()
        with self.assertRaisesRegex(ValueError, 'MXU'): prepare_assistant(self.row)

    def test_missing_duplicate_or_ambiguous_instance_is_rejected(self):
        original = deepcopy(self.config)
        for kind in ('missing', 'duplicate-id', 'duplicate-name'):
            with self.subTest(kind=kind):
                self.config = deepcopy(original)
                if kind == 'missing': self.config['lastActiveInstanceId'] = 'unknown'
                else:
                    second = deepcopy(self.config['instances'][0])
                    if kind == 'duplicate-name': second['id'] = 'other-id'
                    self.config['instances'].append(second)
                self.flush()
                with self.assertRaises(ValueError): prepare_assistant(self.row)

    def test_controller_resource_or_saved_game_mismatch_is_rejected(self):
        original = deepcopy(self.instance)
        for field, value in (('controllerName', '安卓端'), ('resourceName', '模拟器'),
                             ('savedDevice', {'windowName': 'other', 'connectedProgramPath': str(self.game)})):
            with self.subTest(field=field):
                self.instance.clear(); self.instance.update(deepcopy(original)); self.instance[field] = value
                self.flush()
                with self.assertRaises(ValueError): prepare_assistant(self.row)

    def test_direct_launch_is_unique_not_shell_and_does_not_borrow_running_game(self):
        original = deepcopy(self.instance['preActions'])
        for field, value in (('useCmd', True), ('args', '--unsafe'), ('waitForExit', True)):
            with self.subTest(field=field):
                self.instance['preActions'] = deepcopy(original)
                self.instance['preActions'][0][field] = value; self.flush()
                with self.assertRaises(ValueError): prepare_assistant(self.row)
        self.instance['preActions'] = original * 2; self.flush()
        with self.assertRaises(ValueError): prepare_assistant(self.row)
        self.instance['preActions'] = original; self.flush()
        with patch.object(self.mxu, 'find_matching_processes', return_value={'status': 'observed', 'matches': [{'pid': 123}]}):
            self.assertIn('existing_runner_or_game_not_exclusive', prepare_assistant(self.row)['blockers'])

    def test_native_host_actions_are_blocked_but_disabled_special_task_is_preserved(self):
        for task_name in ('__MXU_KILLPROC__', '__MXU_LAUNCH__', '__MXU_POWER__', '__MXU_UNKNOWN__', '__MXU_PRETASK__Other'):
            with self.subTest(task=task_name):
                special = {'id': 'special', 'taskName': task_name, 'enabled': True, 'optionValues': {}}
                self.instance['tasks'] = [deepcopy(self.instance['tasks'][0]), special]; self.flush()
                before = self.config_path.read_bytes()
                self.assertIn('maagf2_mxu_unsafe_native_action', prepare_assistant(self.row)['blockers'])
                self.assertEqual(self.config_path.read_bytes(), before)
                special['enabled'] = False; self.flush()
                self.assertNotIn('maagf2_mxu_unsafe_native_action', prepare_assistant(self.row)['blockers'])

    def test_schedules_and_external_task_triggers_block_automatic_run(self):
        other = deepcopy(self.instance); other.update(id='other', name='other')
        other['schedulePolicies'] = [{'id': 'schedule', 'enabled': True}]
        self.config['instances'].append(other); self.flush()
        self.assertIn('maagf2_mxu_disable_external_triggers', prepare_assistant(self.row)['blockers'])
        self.config['instances'].pop()
        for field, value in (('webServerEnabled', True), ('hotkeys', {'globalEnabled': True}), ('autoRunOnLaunch', True)):
            settings = deepcopy(self.config['settings'])
            with self.subTest(field=field):
                self.config['settings'][field] = value; self.flush()
                self.assertIn('maagf2_mxu_disable_external_triggers', prepare_assistant(self.row)['blockers'])
                self.config['settings'] = settings

    def test_missing_global_hotkey_setting_uses_the_native_disabled_default(self):
        self.config['settings'].pop('hotkeys'); self.flush()
        self.assertNotIn('maagf2_mxu_disable_external_triggers', prepare_assistant(self.row)['blockers'])

    def test_redirected_installation_root_is_rejected_before_metadata_reads(self):
        from types import SimpleNamespace
        real_lstat = Path.lstat
        def redirected(path):
            value = real_lstat(path)
            return (SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
                    if path == self.root else value)
        with patch.object(Path, 'lstat', redirected):
            with self.assertRaisesRegex(ValueError, 'MXU'): prepare_assistant(self.row)

    def test_import_cycles_external_paths_and_duplicate_selectors_are_rejected(self):
        for imported in ({'import': ['tasks/daily.json']}, {'controller': self.interface['controller']}):
            with self.subTest(imported=list(imported)):
                self.write(self.root / 'tasks/daily.json', imported)
                with self.assertRaises(ValueError): prepare_assistant(self.row)

    def test_alternate_import_spellings_normalize_to_the_same_drift_key(self):
        for spelling in ('tasks/daily.json', 'tasks//daily.json', 'tasks/./daily.json'):
            with self.subTest(spelling=spelling):
                self.interface['import'] = [spelling]; self.flush()
                self.assertFalse(prepare_assistant(self.row)['blockers'])

    def test_official_launcher_is_observed_and_reported_when_present(self):
        from types import SimpleNamespace
        directory = self.root / 'game'; directory.mkdir()
        self.game = directory / 'GF2_Exilium.exe'; self.game.write_bytes(b'fixture-game')
        self.instance['savedDevice']['connectedProgramPath'] = str(self.game)
        self.instance['preActions'][0]['program'] = str(self.game)
        self.flush()
        self.assertIsNone(prepare_assistant(self.row)['official_launcher'])
        launcher = self.root / 'PCLauncher.exe'; launcher.write_bytes(b'fixture-launcher')
        scanned = {}
        def scan(targets):
            scanned['targets'] = list(targets)
            return {'status': 'observed', 'matches': []}
        with patch.object(self.mxu, 'find_matching_processes', side_effect=scan):
            prepared = prepare_assistant(self.row)
        self.assertEqual(prepared['official_launcher'], str(launcher))
        self.assertIn(str(launcher), prepared['observation_targets'])
        self.assertIn(str(launcher), scanned['targets'])
        real_lstat = Path.lstat
        def redirected(path):
            value = real_lstat(path)
            return (SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
                    if path == launcher else value)
        with patch.object(Path, 'lstat', redirected):
            with self.assertRaisesRegex(ValueError, 'MXU'): prepare_assistant(self.row)

    def test_redirected_game_parent_is_rejected_at_prepare_and_recheck(self):
        from types import SimpleNamespace
        directory = self.root / 'game'; directory.mkdir()
        self.game = directory / 'GF2_Exilium.exe'; self.game.write_bytes(b'fixture-game')
        self.instance['savedDevice']['connectedProgramPath'] = str(self.game)
        self.instance['preActions'][0]['program'] = str(self.game)
        self.flush()
        prepared = prepare_assistant(self.row)
        real_lstat = Path.lstat
        def redirected(path):
            value = real_lstat(path)
            return (SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
                    if path == directory else value)
        with patch.object(Path, 'lstat', redirected):
            with self.assertRaisesRegex(ValueError, 'MXU'): prepare_assistant(self.row)
            with self.assertRaises(ValueError): verify_installation_stamp(prepared)

    def test_native_installation_and_game_drift_is_rejected_before_launch(self):
        for name in ('config/mxu-FixtureGF2.json', 'agent/main.py', 'resource/base/pipeline.json', 'tasks/daily.json', 'GF2_Exilium.exe'):
            with self.subTest(name=name):
                path = self.root / name; before = path.read_bytes()
                prepared = prepare_assistant(self.row)
                path.write_bytes(before + b' ')
                with self.assertRaisesRegex(ValueError, '重新检查'): verify_installation_stamp(prepared)
                path.write_bytes(before)
        prepared = prepare_assistant(self.row)
        (self.root / 'agent/added.py').write_text('changed')
        with self.assertRaisesRegex(ValueError, '重新检查'): verify_installation_stamp(prepared)

    def test_change_between_prepare_and_stamp_is_rejected(self):
        original = self.mxu.installation_metadata
        calls = []
        def drift(*args):
            value = original(*args); calls.append(1)
            if len(calls) == 1: self.config_path.write_bytes(self.config_path.read_bytes() + b' ')
            return value
        with patch.object(self.mxu, 'installation_metadata', side_effect=drift):
            with self.assertRaisesRegex(ValueError, '重新检查'): prepare_assistant(self.row)

    def test_scheduler_dispatches_mxu_cursor_and_observer_without_legacy_pipe(self):
        from gachaops.assistants import run_assistants
        with patch('gachaops.assistants.windows_available', return_value=True), \
             patch('gachaops.assistants.run_prepared', return_value=({'status': 'success', 'cleanup_verified': True}, self.root)) as launch:
            run_assistants({'assistants': [self.row]}, self.root / 'runs', self.root / 'state', progress=lambda _: None)
        self.assertIs(launch.call_args.kwargs['completion_observer'], self.mxu.observe)
        self.assertEqual(launch.call_args.args[3].instance, 'fixture-default')

    def test_cancellation_during_full_stamp_still_prevents_process_creation(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from gachaops.ok_trial import run_prepared
        current = prepare_assistant(self.row)
        stopped = []
        original = self.mxu.installation_metadata
        def checked_then_cancelled(*args):
            value = original(*args); stopped.append(True)
            return value
        node = self.root / 'node'; node.mkdir()
        session = SimpleNamespace(record={'run_id': 'fixture'}, finish=Mock())
        with patch.object(self.mxu, 'installation_metadata', side_effect=checked_then_cancelled), \
             patch('gachaops.ok_trial.WindowsProcess', side_effect=AssertionError('cancelled during hashing')):
            result, _ = run_prepared(current, session, node, SimpleNamespace(offset=0), 1,
                                     stop_requested=lambda: bool(stopped))
        self.assertEqual(result['status'], 'cancelled')
        self.assertFalse(result['game_execution'])
        self.assertTrue(result['cleanup_verified'])




    def _digest_of(self, name):
        return hashlib.sha256((self.root / name).read_bytes()).hexdigest()

    def test_each_component_drift_reports_not_adapted_without_leaks(self):
        for name in self.components:
            with self.subTest(component=name):
                component = self.root / name
                component.write_bytes(component.read_bytes() + b'-drifted')
                with self.assertRaisesRegex(ValueError, '尚未适配') as caught:
                    prepare_assistant(self.row)
                message = str(caught.exception)
                self.assertIn('MXU', message)
                # User layer answers "what is wrong / what to do": no hashes,
                # no version numbers, no presumptive cause, no real paths.
                self.assertNotIn('残留 MFA', message)
                self.assertNotIn('请更新', message)
                self.assertNotIn(str(self.root), message)
                self.assertNotIn(self.mxu.EXPECTED_COMPONENTS[name][:12], message)
                self.assertNotIn(self._digest_of(name)[:12], message)
                component.write_bytes(component.read_bytes().replace(b'-drifted', b''))

    def test_multiple_component_drifts_are_all_listed_in_diagnostics(self):
        for name in ('MaaGF2Exilium.exe', 'maafw/MaaFramework.dll'):
            component = self.root / name
            component.write_bytes(component.read_bytes() + b'-drifted')
        with self.assertRaisesRegex(ValueError, '尚未适配') as caught:
            prepare_assistant(self.row)
        details = getattr(caught.exception, 'build_diagnostics', None)
        self.assertIsInstance(details, dict)
        listed = {row['component']: row for row in details['components']}
        self.assertEqual(set(listed), {'MaaGF2Exilium.exe', 'maafw/MaaFramework.dll'})
        for name, row in listed.items():
            self.assertEqual(row['actual_sha256'], self._digest_of(name)[:12])
            self.assertEqual(row['expected_sha256'], self.mxu.EXPECTED_COMPONENTS[name][:12])

    def test_resource_version_only_drift_reports_not_adapted_with_versions(self):
        self.interface['version'] = 'v2.8.4'
        self.flush()
        with self.assertRaisesRegex(ValueError, '尚未适配') as caught:
            prepare_assistant(self.row)
        details = caught.exception.build_diagnostics
        self.assertEqual(details['components'], [])
        self.assertEqual(details['versions'].get('resource_interface'), 'v2.8.4')

    def test_config_schema_drift_reports_not_adapted(self):
        self.config['version'] = '2.0'
        self.flush()
        with self.assertRaisesRegex(ValueError, '尚未适配'):
            prepare_assistant(self.row)

    def test_path_redirection_takes_priority_over_component_drift(self):
        (self.root / 'python/python.exe').write_bytes(b'drifted')
        with patch.object(self.mxu, '_redirected', return_value=True):
            with self.assertRaisesRegex(ValueError, '安装路径异常'):
                prepare_assistant(self.row)

    def test_missing_component_reports_not_adapted_and_missing_interface_reports_reinstall(self):
        # A missing expected component is drift (an old or foreign layout);
        # missing core metadata (interface.json) is an incomplete install.
        (self.root / 'agent/main.py').unlink()
        with self.assertRaisesRegex(ValueError, '尚未适配'):
            prepare_assistant(self.row)
        # Both missing at once: the incomplete install wins, because
        # reinstalling fixes both while waiting for an adaptation does not.
        (self.root / 'interface.json').unlink()
        with self.assertRaisesRegex(ValueError, '重新安装'):
            prepare_assistant(self.row)

    def test_missing_interface_alone_reports_reinstall_before_any_content_is_read(self):
        (self.root / 'interface.json').unlink()
        with patch.object(self.mxu, '_json', side_effect=AssertionError('content read before identity')):
            with self.assertRaisesRegex(ValueError, '重新安装'):
                prepare_assistant(self.row)

    def test_drifted_build_is_rejected_before_its_configuration_is_parsed(self):
        # Order matters: a build outside the reviewed baseline must be refused
        # on its hashes, without parsing the instance/controller data it ships.
        (self.root / 'maafw/MaaFramework.dll').write_bytes(b'drifted-framework')
        (self.root / 'interface.json').write_text('{"version": "v9.9.9", "name": "x",'
                                                  ' "interface_version": 2}', encoding='utf-8')
        with patch.object(self.mxu, '_imports', side_effect=AssertionError('imports read before identity')), \
             patch.object(self.mxu, '_instance', side_effect=AssertionError('instance read before identity')):
            with self.assertRaisesRegex(ValueError, '尚未适配'):
                prepare_assistant(self.row)

    def test_missing_entry_in_installation_tree_fails_instead_of_passing_as_unredirected(self):
        # _redirected stays strict: a missing path must raise, never read as
        # "not a link", because it also guards the installation tree walk.
        with self.assertRaises(OSError):
            self.mxu._redirected(self.root / 'no-such-entry')

    def test_check_panel_row_carries_user_text_and_build_diagnostics(self):
        (self.root / 'MaaGF2Exilium.exe').write_bytes(b'drifted-exe')
        (self.root / 'agent/main.py').write_bytes(b'drifted-agent')
        from gachaops.assistants import check_assistants
        report = check_assistants({'repeat_limit': 0, 'assistants': [dict(self.row)]})
        row = report['assistants'][0]
        self.assertEqual(row['status'], 'blocked')
        self.assertIn('尚未适配', row['error'])
        self.assertNotIn('残留 MFA', row['error'])
        self.assertNotIn(str(self.root), row['error'])
        details = row['build_diagnostics']
        self.assertEqual({c['component'] for c in details['components']},
                         {'MaaGF2Exilium.exe', 'agent/main.py'})
        self.assertTrue(all(len(c['actual_sha256']) == 12 for c in details['components']))

    def test_diagnostics_export_keeps_prefixes_and_drops_unknown_fields(self):
        from gachaops.diagnostics import build_diagnostic_preview
        (self.root / 'MaaGF2Exilium.exe').write_bytes(b'drifted-exe')
        from gachaops.assistants import check_assistants
        row = check_assistants({'repeat_limit': 0, 'assistants': [dict(self.row)]})['assistants'][0]
        summary = {'schema_version': 1, 'status': 'blocked', 'assistants': [row]}
        run = self.root / 'runs' / 'gui-fixture'
        run.mkdir(parents=True)
        (run / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
        preview = build_diagnostic_preview(run)
        payload = json.loads(preview.payload)
        entry = payload['records'][0]['summary']['assistants'][0]
        details = entry['build_diagnostics']
        self.assertEqual({c['component'] for c in details['components']}, {'MaaGF2Exilium.exe'})
        self.assertEqual(details['components'][0]['expected_sha256'],
                         self.mxu.EXPECTED_COMPONENTS['MaaGF2Exilium.exe'][:12])
        exported = json.dumps(payload)
        self.assertNotIn(self.mxu.EXPECTED_COMPONENTS['MaaGF2Exilium.exe'], exported)
        self.assertNotIn(self._digest_of('MaaGF2Exilium.exe'), exported)

    def test_diagnostics_export_refuses_path_like_or_free_form_fields(self):
        # Component names, versions and the reason all originate in data read
        # from the installation, which is unreviewed. The export whitelist
        # must let through only the package-relative name, 12 hex characters
        # and a version-like shape - never a drive, a parent reference, an
        # absolute prefix, a path separator or free-form text.
        from gachaops.diagnostics import _allowed
        hostile = {
            'build_diagnostics': {
                'reason': 'C:\\Users\\someone\\secret',
                'components': [
                    {'component': 'C:\\Users\\someone\\AppData\\x.dll', 'actual_sha256': 'a' * 12},
                    {'component': '../../etc/passwd', 'actual_sha256': 'b' * 12},
                    {'component': '/usr/local/bin/x', 'actual_sha256': 'c' * 12},
                    {'component': '.hidden', 'actual_sha256': 'd' * 12},
                    {'component': 'maafw\\MaaFramework.dll', 'actual_sha256': 'e' * 12},
                    {'component': 'maafw/MaaFramework.dll', 'actual_sha256': 'f' * 64},
                    {'component': 'python/python.exe', 'actual_sha256': '0123456789ab'},
                ],
                'versions': {'resource_interface': 'v2.8.8 C:\\Users\\someone',
                             'entry_file_version': '2.11.2',
                             'C:\\Users\\someone': 'x',
                             'extra': '1.0'},
            }}
        allowed = _allowed(hostile)['build_diagnostics']
        self.assertIsNone(allowed['reason'])
        # A legitimate relative name survives on its own when its hash field
        # is not a 12-hex prefix; anything path-like is dropped entirely.
        self.assertEqual(allowed['components'],
                         [{'component': 'maafw/MaaFramework.dll'},
                          {'component': 'python/python.exe', 'actual_sha256': '0123456789ab'}])
        self.assertEqual(allowed['versions'], {'entry_file_version': '2.11.2'})
        exported = json.dumps(allowed)
        for needle in ('Users', 'someone', 'passwd', '..', 'f' * 64):
            self.assertNotIn(needle, exported)

    def test_entry_file_version_is_read_from_the_real_exe(self):
        # The buffer size is the return value of GetFileVersionInfoSizeW; its
        # second argument is an obsolete handle output that stays zero. Reading
        # the handle made this helper return None for every executable, so the
        # advertised version never reached the diagnostics.
        if os.name != 'nt':
            self.skipTest('PE version resources are Windows only')
        # The fixture EXE carries no version resource: honestly reported as
        # absent rather than guessed.
        self.assertIsNone(self.mxu._pe_file_version(self.root / 'MaaGF2Exilium.exe'))
        self.assertIsNone(self.mxu._pe_file_version(self.root / 'agent' / 'main.py'))
        # Against a real binary that ships a version resource, the helper must
        # return the fixed-info version - the same reading Windows reports.
        system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'notepad.exe'
        if not system.is_file():
            self.skipTest('no versioned system binary available')
        version = self.mxu._pe_file_version(system)
        self.assertRegex(version, r'^\d+\.\d+\.\d+\.\d+$')

    def test_dangling_component_link_is_a_path_anomaly_not_drift(self):
        # A dangling link reports is_file() == False. The missing-file branch
        # must not classify it as drift ("wait for an adaptation"); the link
        # itself is the problem and the user has to reconnect.
        if not hasattr(os, 'symlink'):
            self.skipTest('symlinks unavailable')
        target = self.root / 'agent' / 'main.py'
        target.unlink()
        try:
            os.symlink(self.root / 'no-such-target', target)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f'symlink creation unavailable: {error}')
        with self.assertRaisesRegex(ValueError, '安装路径异常'):
            prepare_assistant(self.row)

    def test_component_under_redirected_directory_is_a_path_anomaly(self):
        # The installation root itself is clean; the directory holding one
        # component is a redirect whose target is gone, so the component's
        # is_file() is False. The missing-component branch must still walk the
        # path and report the anomaly instead of classifying it as drift.
        # Creating a real link needs a privilege this test may not have, so the
        # entry is kept on disk and only the link test answers "yes" for it.
        (self.root / 'python' / 'python.exe').unlink()
        (self.root / 'python' / 'gone.txt').write_bytes(b'keeps the directory present')
        real_redirected = self.mxu._redirected
        with patch.object(self.mxu, '_redirected',
                          side_effect=lambda p: p.name == 'python' or real_redirected(p)):
            with self.assertRaisesRegex(ValueError, '安装路径异常'):
                prepare_assistant(self.row)
        (self.root / 'python' / 'gone.txt').unlink()
        # With nothing redirected, the absent component is ordinary drift.
        with self.assertRaisesRegex(ValueError, '尚未适配'):
            prepare_assistant(self.row)

    def test_blocked_run_persists_build_diagnostics_into_the_exported_summary(self):
        # Drive the real flow: run_assistants must save the diagnostics into
        # the per-node summary.json, because that is the only file the
        # diagnostic export reads. A hand-built summary would hide a break
        # between the exception handler and the export.
        from gachaops.assistants import run_assistants
        from gachaops.diagnostics import build_diagnostic_preview
        (self.root / 'maafw/MaaFramework.dll').write_bytes(b'drifted-framework')
        runs = self.root / 'runs'
        with patch('gachaops.assistants.windows_available', return_value=True):
            run_assistants({'assistants': [self.row]}, runs, self.root / 'state', progress=lambda _: None)
        summaries = sorted(runs.glob('assistants-*/node-*/summary.json'))
        self.assertTrue(summaries, 'run_assistants wrote no node summary')
        saved = json.loads(summaries[0].read_text(encoding='utf-8-sig'))
        self.assertEqual(saved['status'], 'blocked')
        details = saved['build_diagnostics']
        self.assertEqual([c['component'] for c in details['components']], ['maafw/MaaFramework.dll'])
        # The desktop exports from the chain directory (summary_path.parent);
        # the export reads that directory's summary.json and node-*/summary.json.
        chain = summaries[0].parents[1]
        exported = json.loads(build_diagnostic_preview(chain).payload)
        entry = next(r['summary'] for r in exported['records'] if r['evidence'].endswith('node-01/summary.json'))
        self.assertIn('build_diagnostics', entry, 'diagnostics did not reach the export')
        self.assertEqual(entry['build_diagnostics']['components'],
                         [{'component': 'maafw/MaaFramework.dll',
                           'actual_sha256': hashlib.sha256(b'drifted-framework').hexdigest()[:12],
                           'expected_sha256': self.mxu.EXPECTED_COMPONENTS['maafw/MaaFramework.dll'][:12]}])
        # The fixture EXE has no version resource, so no version may be
        # invented for it; the end-to-end path is what matters here.
        self.assertEqual(entry['build_diagnostics']['versions'], {})

    def test_entry_version_reaches_the_export_when_the_exe_has_one(self):
        # Same real flow, with a versioned binary standing in for the entry
        # EXE: the version read at rejection time must survive into the export.
        from gachaops.assistants import run_assistants
        from gachaops.diagnostics import build_diagnostic_preview
        if os.name != 'nt':
            self.skipTest('PE version resources are Windows only')
        system = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'notepad.exe'
        if not system.is_file():
            self.skipTest('no versioned system binary available')
        expected_version = self.mxu._pe_file_version(system)
        self.assertRegex(expected_version, r'^\d+\.\d+\.\d+\.\d+$')
        (self.root / 'maafw/MaaFramework.dll').write_bytes(b'drifted-framework')
        runs = self.root / 'runs'
        with patch('gachaops.assistants.windows_available', return_value=True), \
             patch.object(self.mxu, '_pe_file_version', return_value=expected_version):
            run_assistants({'assistants': [self.row]}, runs, self.root / 'state', progress=lambda _: None)
        summaries = sorted(runs.glob('assistants-*/node-*/summary.json'))
        exported = json.loads(build_diagnostic_preview(summaries[0].parents[1]).payload)
        entry = next(r['summary'] for r in exported['records'] if r['evidence'].endswith('node-01/summary.json'))
        self.assertEqual(entry['build_diagnostics']['versions'], {'entry_file_version': expected_version})


class MxuCompletionTests(unittest.TestCase):
    def setUp(self):
        self.mxu = importlib.import_module('gachaops.maagf2_mxu')
        self.assertTrue(hasattr(self.mxu, 'bind_execution'), 'MXU needs a run-specific owned-writer cursor')
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / 'debug').mkdir(); (self.root / 'python').mkdir(); (self.root / 'node').mkdir()
        self.exe = self.root / 'MaaGF2Exilium.exe'; self.exe.write_bytes(b'fixture')
        self.agent = self.root / 'python/python.exe'; self.agent.write_bytes(b'fixture')
        self.current = {'cwd': str(self.root), 'instance_name': 'fixture-default', 'launcher': str(self.exe),
                        'mxu_agent_image': str(self.agent)}
        _, self.cursor = self.mxu.bind_execution(self.current, self.root / 'node')
        created = int((self.cursor.captured_at_ns / 1e9 + 11644473600) * 1e7)
        self.owned = [{'pid': 123, 'image': str(self.exe), 'creation_ticks': created, 'in_owned_job': True},
                      {'pid': 456, 'image': str(self.agent), 'creation_ticks': created, 'in_owned_job': True}]

    def cycle(self, failed=False):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        def frame(message, pid=123): return f'[{timestamp}][INF][Px{pid}][Tx1] {message}'
        def event(status, task_id, pid=123):
            return frame(f'[msg=Tasker.Task.{status}] [details=' + json.dumps({'task_id': task_id, 'entry': 'fixture-task-' + str(task_id)}) + ']', pid)
        frames = [frame('[Logger] MAA Process Start')]
        for task_id in (1, 2):
            frames.extend([event('Starting', task_id), event('Failed' if failed and task_id == 2 else 'Succeeded', task_id)])
        gui_stamp = timestamp[:19]
        logs = [f'{gui_stamp} INFO [Task] 实例 fixture-default: 任务已提交, task_ids: [1, 2]',
                f'{gui_stamp} INFO [App] 自动执行任务完成，关闭自身']
        return frames, logs, frame, event

    def write_cycle(self, frames, gui):
        (self.root / 'debug/maafw.log').write_text('\n'.join(frames) + '\n', encoding='utf-8')
        (self.root / 'debug/2026-11-04-1.log').write_text('\n'.join(gui) + '\n', encoding='utf-8')

    def observe(self, code=0, owned=None):
        return self.mxu.observe(self.cursor, code, 123, self.owned if owned is None else owned)

    def fresh_cursor(self):
        # Each malformed-input case represents a separate run. Log corruption
        # stays latched within a run and must not make later cases pass vacuously.
        self.cursor = replace(self.cursor, root_identity=None, log_error=None)

    def test_complete_root_batch_and_owned_python_writer_preserve_partial_failure(self):
        for failed in (False, True):
            frames, gui, frame, event = self.cycle(failed)
            self.write_cycle(frames + [frame('[Logger] MAA Process Start', 456), event('Succeeded', 99, 456)], gui)
            result = self.observe()
            self.assertEqual(result['status'], 'partial_failure' if failed else 'success')
            self.assertEqual(result['failed_applications'], ['fixture-task-2'] if failed else [])
            self.assertEqual([r['pid'] for r in result['auxiliary_log_writers']], [456])

    def test_extra_started_task_without_terminal_callback_stays_unconfirmed(self):
        # An unmatched Starting beyond the submitted batch is unfinished work;
        # its missing terminal callback must not be ignored for success.
        frames, gui, _, event = self.cycle()
        self.write_cycle(frames + [event('Starting', 3)], gui)
        self.assertEqual(self.observe()['status'], 'completion_unconfirmed')

    def test_missing_start_terminal_quit_wrong_instance_and_duplicate_batch_are_unknown(self):
        frames, gui, _, _ = self.cycle()
        for f, g in ((frames[1:], gui), (frames[:-1], gui), (frames, gui[:-1]),
                     (frames, [s.replace('fixture-default', 'other') for s in gui]),
                     (frames, gui + gui[:1]), (frames[0:1] + frames[2:], gui)):
            with self.subTest(frames=len(f), gui=len(g)):
                self.fresh_cursor()
                self.write_cycle(f, g)
                self.assertEqual(self.observe()['status'], 'completion_unconfirmed')
        self.fresh_cursor(); self.write_cycle(frames, gui)
        self.assertEqual(self.observe(code=None)['status'], 'completion_unconfirmed')
        self.assertEqual(self.observe(code=2)['status'], 'failed')

    def test_root_image_creation_time_and_job_membership_are_required(self):
        frames, gui, _, _ = self.cycle(); self.write_cycle(frames, gui)
        original = deepcopy(self.owned)
        for key, value in (('image', str(self.agent)), ('creation_ticks', 1), ('in_owned_job', False)):
            with self.subTest(key=key):
                owned = deepcopy(original); owned[0][key] = value
                self.assertEqual(self.observe(owned=owned)['status'], 'completion_unconfirmed')
        self.assertEqual(self.observe()['status'], 'success')
        replaced = deepcopy(original); replaced[0]['creation_ticks'] += 1
        self.assertEqual(self.observe(owned=replaced)['status'], 'completion_unconfirmed')

    def test_unknown_or_stale_auxiliary_writer_cannot_be_ignored(self):
        frames, gui, frame, _ = self.cycle()
        self.write_cycle(frames + [frame('auxiliary callback', 999)], gui)
        self.assertEqual(self.observe()['status'], 'completion_unconfirmed')
        self.fresh_cursor()
        self.write_cycle(frames + [frame('auxiliary callback', 456)], gui)
        owned = deepcopy(self.owned); owned[1]['creation_ticks'] += 60_000_000
        self.assertEqual(self.observe(owned=owned)['status'], 'completion_unconfirmed')

    def test_rotation_skips_only_authenticated_prefix_and_rejects_old_replacement(self):
        native = self.root / 'debug/maafw.log'
        native.write_text('old session\n', encoding='utf-8')
        _, self.cursor = self.mxu.bind_execution(self.current, self.root / 'node')
        native.rename(self.root / 'debug/maafw.bak.fixture.log')
        frames, gui, _, _ = self.cycle(); self.write_cycle(frames, gui)
        self.assertEqual(self.observe()['status'], 'success')
        native.write_text('\n'.join(s.replace(s[1:24], '2000-01-01 00:00:00.000') for s in frames) + '\n', encoding='utf-8')
        self.assertEqual(self.observe()['status'], 'completion_unconfirmed')

    def test_bad_utf8_incomplete_terminal_and_duplicate_terminal_never_succeed(self):
        frames, gui, _, _ = self.cycle()
        self.write_cycle(frames, gui)
        native = self.root / 'debug/maafw.log'
        with native.open('ab') as stream: stream.write(b'\xff critical\n')
        self.assertEqual(self.observe()['status'], 'completion_unconfirmed')
        self.write_cycle(frames, gui)
        self.assertEqual(self.observe()['status'], 'completion_unconfirmed')
        self.fresh_cursor()
        native.write_text('\n'.join(frames), encoding='utf-8')
        self.assertEqual(self.observe()['status'], 'completion_unconfirmed')
        self.fresh_cursor()
        self.write_cycle(frames + frames[-1:], gui)
        self.assertEqual(self.observe()['status'], 'completion_unconfirmed')

    def test_complete_batch_cannot_hide_malformed_unknown_or_unfinished_callback(self):
        frames, gui, frame, _ = self.cycle()
        for message, newline in (
            ('[msg=Tasker.Task.Failed] [details={"task_id":3', True),
            ('[msg=Tasker.Task.Unknown] [details={"task_id":3}]', True),
            ('[msg=Tasker.Task.Failed] [details=[]]', True),
            ('[msg=Tasker.Task.Failed] [details={"task_id":3', False),
            ('[msg=Tasker.Ta', False),
        ):
            with self.subTest(message=message, newline=newline):
                self.fresh_cursor(); self.write_cycle(frames, gui)
                with (self.root / 'debug/maafw.log').open('a', encoding='utf-8') as stream:
                    stream.write(frame(message) + ('\n' if newline else ''))
                self.assertEqual(self.observe()['status'], 'completion_unconfirmed')
                self.write_cycle(frames, gui)
                self.assertEqual(self.observe()['status'], 'completion_unconfirmed')
