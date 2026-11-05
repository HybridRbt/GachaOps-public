"""Optional Qt regression checks; only temporary configuration and reports are used."""
import importlib.util
import json
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'optional desktop dependencies unavailable')
class DesktopUiTests(unittest.TestCase):
    def _registration_case(self, exercise):
        from PySide6.QtWidgets import QApplication
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def invoke(window):
                window.timer.stop(); window.recheck_timer.stop()
                exercise(window, root)
                self.assertIsNone(window.process)
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', invoke, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_registration_save_cancel_buttons_are_chinese_and_keep_standard_roles(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        def exercise(window, root):
            before = Path(window.profiles.currentData()).read_bytes()
            def cancel(dialog):
                buttons = dialog.findChild(QDialogButtonBox)
                save = buttons.button(QDialogButtonBox.StandardButton.Save)
                cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
                self.assertEqual(save.text(), '保存')
                self.assertEqual(cancel.text(), '取消')
                self.assertEqual(buttons.buttonRole(save), QDialogButtonBox.ButtonRole.AcceptRole)
                self.assertEqual(buttons.buttonRole(cancel), QDialogButtonBox.ButtonRole.RejectRole)
                return QDialog.DialogCode.Rejected
            with patch.object(QDialog, 'exec', cancel):
                window.register_assistant(window.data['assistants'][0]['id'])
            self.assertEqual(Path(window.profiles.currentData()).read_bytes(), before)
        self._registration_case(exercise)

    def _registration_retry_case(self, action, chooser_cancel=False, failures=1, preview_accept=True):
        from PySide6.QtWidgets import QDialog, QLineEdit, QFileDialog, QMessageBox
        from gachaops import desktop
        from gachaops.registration import RegistrationInspection
        def exercise(window, root):
            row = next(r for r in window.data['assistants'] if r['adapter'] == 'zzz-onedragon')
            before = Path(window.profiles.currentData()).read_bytes()
            calls = []; errors = []
            def edit(dialog):
                install = dialog.findChild(QLineEdit, 'installation_root')
                game = dialog.findChild(QLineEdit, 'game_path')
                if not calls: game.setText(str(root / 'remember.exe'))
                self.assertEqual(game.text(), str(root / 'remember.exe'))
                calls.append(install.text())
                if chooser_cancel and len(calls) > 1:
                    self.assertEqual(install.text(), '')
                    return QDialog.DialogCode.Rejected
                return QDialog.DialogCode.Accepted
            def error_dialog(box):
                labels = {button.text(): button for button in box.buttons()}
                self.assertEqual(set(labels), {'重新选择', '取消'})
                errors.append(box.text())
                choice = '取消' if action == 'cancel' or len(errors) > failures else '重新选择'
                box._fixture_choice = labels[choice]
                return 0
            candidate = RegistrationInspection(dict(row, installation_root=str(root)), (), (), {})
            outcomes = [ValueError('请选择已安装助手的完整目录')] * failures
            if action != 'cancel' and not chooser_cancel: outcomes.append(candidate)
            with patch.object(QDialog, 'exec', edit), \
                 patch.object(QMessageBox, 'exec', error_dialog), \
                 patch.object(QMessageBox, 'clickedButton', lambda box: box._fixture_choice), \
                 patch.object(QMessageBox, 'warning', return_value=None), \
                 patch.object(QMessageBox, 'question', return_value=(QMessageBox.StandardButton.Yes if preview_accept else QMessageBox.StandardButton.No)) as preview, \
                 patch.object(QFileDialog, 'getExistingDirectory', return_value='' if chooser_cancel else str(root)) as chooser, \
                 patch.object(desktop, 'inspect_registration', side_effect=outcomes) as inspect:
                window.register_assistant(row['id'])
                self.assertEqual(len(errors), failures)
                if action == 'cancel' or chooser_cancel:
                    self.assertEqual(Path(window.profiles.currentData()).read_bytes(), before)
                    self.assertEqual(inspect.call_count, 1)
                    preview.assert_not_called()
                    self.assertEqual(chooser.call_count, int(chooser_cancel))
                else:
                    self.assertEqual(inspect.call_count, failures + 1)
                    self.assertEqual(chooser.call_count, failures)
                    self.assertEqual(inspect.call_args.args[1]['installation_root'], str(root))
                    self.assertEqual(len(calls), failures + 1)
                    preview.assert_called_once()
                    if preview_accept:
                        saved = next(r for r in window.data['assistants'] if r['id'] == row['id'])
                        self.assertEqual(saved['installation_root'], str(root))
                    else:
                        self.assertEqual(Path(window.profiles.currentData()).read_bytes(), before)
        self._registration_case(exercise)

    def test_registration_invalid_path_can_retry_and_revalidates_before_save(self):
        self._registration_retry_case('retry', failures=3)

    def test_registration_error_cancel_exits_without_saving(self):
        self._registration_retry_case('cancel')

    def test_registration_chooser_cancel_returns_to_preserved_inputs(self):
        self._registration_retry_case('retry', chooser_cancel=True)

    def test_registration_retry_preview_cancel_does_not_save(self):
        self._registration_retry_case('retry', preview_accept=False)

    def test_profile_rename_preserves_unsaved_order_selection_and_timeouts(self):
        from copy import deepcopy
        from PySide6.QtWidgets import QApplication
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                before_disk = Path(window.profiles.currentData()).read_bytes()
                window.data['assistants'].reverse()
                window.data['assistants'][0]['timeout_seconds'] = 2400
                window.data['assistants'][0]['installation_root'] = str(root)
                window.selected_ids = [window.data['assistants'][0]['id']]
                window.render()
                window.selection()
                expected_data = deepcopy(window.data)
                expected_selection = window.selected_ids[:]
                window.rename_profile_named('只改名称')
                self.assertEqual(window.data, expected_data)
                self.assertEqual(window.selected_ids, expected_selection)
                self.assertEqual(Path(window.profiles.currentData()).read_bytes(), before_disk)
                self.assertEqual(window.profile_names[window.profiles.currentData()], '只改名称')
                self.assertIn('自定义', window.profiles.currentText())
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_profile_deletion_requires_named_permanent_confirmation_and_cancel_is_noop(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                window.create_profile_named('待删除组合')
                selected = window.profiles.currentData()
                before = Path(selected).read_bytes()
                window.profiles.setItemText(window.profiles.currentIndex(), '自定义 · 已选 0 项')
                with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No) as ask:
                    window.prompt_delete_profile()
                    message = ask.call_args.args[2]
                    self.assertIn('待删除组合', message)
                    self.assertIn('不可恢复', message)
                    self.assertNotIn('移入备份', message)
                self.assertEqual(Path(selected).read_bytes(), before)
                with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                    window.prompt_delete_profile()
                self.assertFalse(Path(selected).parent.exists())
                self.assertEqual(window.profiles.count(), 1)
                self.assertIsNone(window.process)
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_partial_profile_deletion_refreshes_list_and_warns_about_residue(self):
        from PySide6.QtWidgets import QMessageBox
        def exercise(window, root):
            window.create_profile_named('待删除')
            real_delete = window.profile_store.delete_profile
            def incomplete(identity):
                real_delete(identity)
                return {'cleanup_complete': False, 'residual_path': str(root / 'config/profiles/.deleting-fixture')}
            with patch.object(window.profile_store, 'delete_profile', side_effect=incomplete), \
                 patch.object(QMessageBox, 'warning') as warning:
                window.delete_current_profile()
                self.assertEqual(window.profiles.count(), 1)
                warning.assert_called_once()
                self.assertIn('未完全删除', warning.call_args.args[1])
                self.assertIn('.deleting-fixture', warning.call_args.args[2])
        self._registration_case(exercise)

    def setUp(self):
        # Public tests use generated pixels, never redistribution-unknown game art.
        from PySide6.QtGui import QImage, QColor
        from gachaops import desktop
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for identity in desktop.NAMES:
            image = QImage(64, 64, QImage.Format.Format_ARGB32)
            image.fill(QColor('#2478bb'))
            self.assertTrue(image.save(str(root / (identity + '.png'))))
        icon_patch = patch.object(desktop, 'ICON_DIR', root)
        icon_patch.start(); self.addCleanup(icon_patch.stop)
        self.addCleanup(self._release_qt)

    def test_clean_first_start_profiles_are_editable_without_starting_worker(self):
        from PySide6.QtWidgets import QApplication
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                self.assertEqual(len(window.data['assistants']), 7)
                self.assertFalse(window.selected_ids)
                self.assertIsNone(window.process)
                self.assertFalse(window.start.isEnabled())
                window.create_profile_named('新组合')
                self.assertEqual(window.profiles.count(), 2)
                window.rename_profile_named('已改名')
                self.assertIn('已改名', window.profiles.currentText())
                window.delete_current_profile()
                self.assertEqual(window.profiles.count(), 1)
                self.assertIsNone(window.process)
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_each_catalog_adapter_has_registration_and_blocked_candidate_stays_disabled(self):
        from PySide6.QtWidgets import QApplication, QPushButton, QDialog, QLineEdit
        from gachaops import desktop
        from gachaops.registration import RegistrationInspection
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop(); window.set_view('all')
                for index, row in enumerate(window.data['assistants']):
                    button = window.table.cellWidget(index, 2).findChild(QPushButton)
                    self.assertIsNotNone(button, row['adapter'])
                    self.assertTrue(button.isEnabled())
                    def cancel(dialog):
                        from gachaops.adapter_catalog import get_adapter
                        for field in get_adapter(row['adapter']).registration_fields:
                            self.assertIsNotNone(dialog.findChild(QLineEdit, field))
                        return QDialog.DialogCode.Rejected
                    before = Path(window.profiles.currentData()).read_bytes()
                    with patch.object(QDialog, 'exec', cancel): window.register_assistant(row['id'])
                    self.assertEqual(Path(window.profiles.currentData()).read_bytes(), before)
                row = window.data['assistants'][0]
                inspection = RegistrationInspection(dict(row, installation_root=str(root)),
                    ('version_unsupported',), (), {})
                window.commit_registration_inspection(row['id'], inspection)
                self.assertFalse(window.data['assistants'][0]['enabled'])
                self.assertIsNone(window.process)
                self.assertEqual(window.profile_store.load_profile(window.profile_ids[window.profiles.currentData()]), window.data)
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), patch('sys.argv', ['desktop-test']):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_manual_mode_only_for_supported_adapters_and_ww_persists(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QComboBox
        from gachaops import desktop
        from gachaops.assistants import load_assistants
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'fixture.json'
            adapters = [('wuthering-waves', 'ok-ww'), ('endfield', 'maa-end'),
                        ('zzz', 'zzz-onedragon'), ('nte', 'ok-nte'), ('rail', 'march7'),
                        ('gf2', 'maa-gf2'), ('genshin', 'bettergi')]
            rows = [dict(id=identity, adapter=adapter, installation_root=str(root), enabled=True)
                    for identity, adapter in adapters]
            rows[2]['game_path'] = str(root / 'game.exe')
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                controls = {}
                for row in window.data['assistants']:
                    cell = window.game_start_cell(row, False, '')
                    combo = cell.findChild(QComboBox, 'gameStartInput')
                    if row['id'] in {'wuthering-waves', 'endfield'}:
                        self.assertIsNotNone(combo, row['id'])
                        controls[row['id']] = (cell, combo)
                    else:
                        self.assertIsNone(combo, row['id'])
                        cell.deleteLater()
                combo = controls['wuthering-waves'][1]
                self.assertIn('自动启动', combo.toolTip())
                self.assertNotIn('MaaEnd', combo.toolTip())
                combo.setCurrentIndex(combo.findData('manual'))
                self.assertEqual(window.selection()['assistants'][0]['game_start'], 'manual')
                window.save_selection()
                saved = load_assistants(Path(window.profiles.currentData()))
                self.assertEqual(saved['assistants'][0]['game_start'], 'manual')
                # Unsupported adapters cannot be changed through the handler either.
                window.commit_game_start('nte', combo)
                self.assertNotEqual(window.data['assistants'][3].get('game_start'), 'manual')
                with patch.object(window, 'running', return_value=True):
                    locked = window.game_start_cell(window.data['assistants'][0], False, '')
                    self.assertIsNone(locked.findChild(QComboBox))
                    locked.deleteLater()
                with patch.object(window, 'busy', return_value=True):
                    combo.setCurrentIndex(combo.findData('assistant'))
                    self.assertEqual(window.data['assistants'][0]['game_start'], 'manual')
                for cell, _ in controls.values(): cell.deleteLater()
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QProcess, 'start', return_value=None), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_endfield_start_mode_is_saved_and_locked_while_running(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QComboBox
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'fixture.json'
            path.write_text(json.dumps(dict(schema_version=1, assistants=[dict(
                id='endfield', adapter='maa-end', installation_root=str(root), enabled=True)])))
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                combo = window.findChild(QComboBox, 'gameStartInput')
                self.assertIsNotNone(combo)
                combo.setCurrentIndex(combo.findData('manual'))
                self.assertEqual(window.selection()['assistants'][0]['game_start'], 'manual')
                window.save_selection()
                saved = json.loads((Path(window.profiles.currentData())).read_text(encoding='utf-8'))
                self.assertEqual(saved['assistants'][0]['game_start'], 'manual')
                with patch.object(window, 'running', return_value=True):
                    cell = window.game_start_cell(window.data['assistants'][0], False, '')
                    self.assertIsNone(cell.findChild(QComboBox))
                    cell.deleteLater()
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QProcess, 'start', return_value=None), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_unknown_protocol_has_actionable_chinese_preflight_message(self):
        from gachaops.desktop import BLOCKER_TEXT
        message = BLOCKER_TEXT['native_command_protocol_unverified']
        self.assertIn('助手版本', message)
        self.assertIn('GachaOps', message)
    def test_recovery_entry_uses_worker_without_running_games_or_losing_report(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QMessageBox, QDialog, QPushButton
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'fixture.json'
            path.write_text(json.dumps(dict(schema_version=1, assistants=[dict(
                id='one', adapter='ok-ww', installation_root=str(root), enabled=False)])), encoding='utf-8')
            (root / 'state').mkdir()
            (root / 'state/execution.json').write_text(json.dumps(dict(schema_version=1, run_id='run',
                phase='quarantined', cleanup_verified=False)))
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                self.assertFalse(window.recovery_button.isHidden())
                window.results={'one': dict(id='one', status='ownership_or_cleanup_unconfirmed')}
                buttons=[]
                with patch.object(QDialog, 'exec', lambda dialog: buttons.extend(
                    b.text() for b in dialog.findChildren(QPushButton))):
                    window.open_result_reason('one')
                self.assertIn('检查并恢复运行资格', buttons)
                old=root/'old-session'; old.mkdir(); window.session=old
                old_summary=old/'summary.json'; window.summary_path=old_summary
                window.begin('recover-assistants')
                self.assertEqual(window.action, 'recover-assistants')
                self.assertIn('recover-assistants', window.process.arguments())
                (window.session/'recovery.json').write_text(json.dumps(dict(status='recovered', message='核对通过')))
                with patch.object(QMessageBox, 'information') as info:
                    window.finished(0, QProcess.ExitStatus.NormalExit)
                    self.assertIn('核对通过', info.call_args.args[2])
                self.assertEqual(window.session, old); self.assertEqual(window.summary_path, old_summary)
                window.data = None  # Recovery must not depend on a loadable current preset.
                window.begin_recovery()
                self.assertEqual(window.action, 'recover-assistants')
                self.assertNotEqual(window.session, old)
                self.assertIn('recover-assistants', window.process.arguments())
                with patch.object(window, 'busy', return_value=True):
                    window.refresh_controls(); self.assertFalse(window.recovery_button.isEnabled())
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QProcess, 'start', return_value=None), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_repeat_control_snapshot_lock_and_active_retry(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QLabel
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'fixture.json'
            path.write_text(json.dumps(dict(schema_version=1, assistants=[dict(
                id='one', adapter='ok-ww', installation_root=str(root), enabled=True)])), encoding='utf-8')
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                self.assertEqual(window.repeat_input.value(), 0)
                window.repeat_input.setValue(2)
                window.save_selection()
                saved = json.loads((Path(window.profiles.currentData())).read_text(encoding='utf-8'))
                self.assertEqual(saved['repeat_limit'], 2)
                window.begin('run-assistants')
                self.assertEqual(window.data['repeat_limit'], 2)
                snapshot = json.loads((window.session / 'assistants.json').read_text(encoding='utf-8'))
                self.assertEqual(snapshot['repeat_limit'], 2)
                chain = window.session / 'assistants-fixture'; chain.mkdir()
                node = chain / 'node-02'; node.mkdir()
                summary = dict(status='running', current_round=2, repeat_limit=2,
                    assistants=[dict(id='one', status='partial_failure', attempts=[dict(id='one',
                        status='partial_failure', round=1, completion={'errors': ['第一次失败']})])],
                    active_node_summary=str(node / 'summary.json'))
                (chain / 'summary.json').write_text(json.dumps(summary))
                (node / 'summary.json').write_text(json.dumps(dict(id='one', status='running')))
                with patch.object(window, 'running', return_value=True), patch.object(window, 'busy', return_value=True):
                    window.refresh_controls(); window.poll()
                    self.assertFalse(window.repeat_input.isEnabled())
                    self.assertEqual(window.results['one']['status'], 'running')
                    self.assertEqual(window.results['one']['attempts'][0]['round'], 1)
                    self.assertTrue(any(label.text() == '运行详情'
                        for label in window.table.cellWidget(0, 4).findChildren(QLabel)))
                    self.assertIn('第 2 / 3 轮', window.running_title.text())
                    window.request_stop()
                    self.assertTrue((window.session / 'stop.request').exists())
                summary.update(status='completed_with_failures', repeat_limit_reached=True)
                (chain / 'summary.json').write_text(json.dumps(summary))
                window.poll()
                self.assertIn('达到重复上限', window.running_title.text())
                self.assertFalse(window.repeat_summary.isHidden())
                self.assertIn('达到重复上限', window.repeat_summary.text())
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QProcess, 'start', return_value=None), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_startup_failure_dialog_contains_worker_error(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QDialog, QPlainTextEdit
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'fixture.json'
            path.write_text(json.dumps(dict(schema_version=1, assistants=[dict(
                id='fixture', adapter='ok-ww', installation_root=str(root), enabled=True)])), encoding='utf-8')
            session = root / 'session'; session.mkdir()
            raw = 'gachaops: previous execution has unverified cleanup; manual inspection required'
            (session / 'worker.log').write_text(raw + '\n', encoding='utf-8')
            from gachaops.recovery import save
            chain_path = root / 'runs/gui-old/assistants-old/summary.json'
            chain_path.parent.mkdir(parents=True)
            save(chain_path, dict(run_id='old-run', status='stopped_cleanup_unconfirmed', assistants=[
                dict(id='genshin', status='ownership_or_cleanup_unconfirmed', cleanup_verified=False)]))
            (root / 'state').mkdir()
            save(root / 'state/execution.json', dict(schema_version=1, run_id='old-run', phase='quarantined',
                cleanup_verified=False, chain_summary=str(chain_path)))
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                window.session = session; window.action = 'run-assistants'; window.summary_path = None
                with patch.object(window, 'set_problems') as problems:
                    window.finished(2, QProcess.ExitStatus.NormalExit)
                    problem = problems.call_args.args[0][0]
                texts = []
                with patch.object(QDialog, 'exec', lambda dialog: texts.append(
                        dialog.findChild(QPlainTextEdit).toPlainText())):
                    window.open_result_reason(None, problem)
                self.assertIn('上一轮清理未确认', texts[0])
                self.assertIn('当前轮未启动', texts[0])
                self.assertIn(raw, texts[0])
                for instruction in ('原神', 'old-run', '管理员', '退出', '运行情况', '检查并恢复运行资格', '导出诊断包'):
                    self.assertIn(instruction, texts[0])
                self.assertNotIn('可能原因，并非已确认的诊断', texts[0])
                self.assertNotIn('请查看下方错误', texts[0])
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QProcess, 'start', side_effect=AssertionError('no process launch')), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_timeout_input_clamps_saves_and_enters_worker_snapshot(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QLineEdit
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'fixture.json'
            rows = [dict(id=f'fixture-{i}', adapter='ok-ww', installation_root=str(root),
                         enabled=True, timeout_seconds=1800) for i in range(7)]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                for i in range(7):
                    value = window.table.cellWidget(i, 3).findChild(QLineEdit)
                    self.assertIsNotNone(value)
                    value.setText('180' if i == 6 else '5')
                    value.editingFinished.emit()
                    self.assertEqual(value.text(), '180' if i == 6 else '10')
                expected = [600] * 6 + [10800]
                self.assertEqual([r['timeout_seconds'] for r in window.selection()['assistants']], expected)
                window.render()  # Old cell widgets awaiting deletion cannot overwrite edits.
                value = window.table.cellWidget(0, 3).findChild(QLineEdit)
                value.setText('45')  # Save commits a still-active editor too.
                window.save_selection()
                saved = json.loads((Path(window.profiles.currentData())).read_text(encoding='utf-8'))
                self.assertEqual(saved['assistants'][0]['timeout_seconds'], 2700)
                window.load_profile()
                # Reload the saved combination rather than the original fixture.
                window.profiles.setCurrentIndex(window.profiles.findData(str(Path(window.profiles.currentData()))))
                window.recheck_timer.stop()
                self.assertEqual(window.table.cellWidget(0, 3).findChild(QLineEdit).text(), '45')
                window.set_view('all')
                value = window.table.cellWidget(1, 3).findChild(QLineEdit)
                value.setText('-1'); value.editingFinished.emit()
                self.assertEqual(value.text(), '10')
                value.setText(''); value.editingFinished.emit()
                self.assertEqual(value.text(), '10')
                window.show(); value.setFocus(); QApplication.processEvents()
                with patch.object(window, 'begin') as launch:
                    window.auto_recheck(); launch.assert_not_called()
                window.recheck_timer.stop()
                with patch.object(QProcess, 'start') as start:
                    window.begin('run-assistants')
                    start.assert_called_once()
                    snapshot = json.loads((window.session / 'assistants.json').read_text(encoding='utf-8'))
                    self.assertEqual(snapshot['assistants'][6]['timeout_seconds'], 10800)
                    self.assertEqual(snapshot['assistants'][0]['timeout_seconds'], 2700)
                class Running:
                    def state(self): return QProcess.Running
                window.process = Running()
                cell = window.timeout_cell(window.data['assistants'][0], None)
                self.assertIsNone(cell.findChild(QLineEdit))
                window.process = None; window.session = None; window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QProcess, 'start', side_effect=AssertionError('no process launch')), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_finished_poll_keeps_bottom_assistant_accessible(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QPushButton
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'fixture.json'
            rows = [dict(id=f'fixture-{i}', adapter='ok-ww', installation_root=str(root),
                         enabled=True, game_start='assistant') for i in range(7)]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')
            session = root / 'session'; summary = session / 'assistants-fixture' / 'summary.json'
            summary.parent.mkdir(parents=True)
            report = dict(status='completed_with_failures', assistants=[
                dict(id=row['id'], status='partial_failure', error='fixture failure') for row in rows])
            summary.write_text(json.dumps(report), encoding='utf-8')
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                window.session = session; window.action = 'run-assistants'
                window.table.setFixedHeight(160); window.show(); window.poll()
                QApplication.processEvents()
                bar = window.table.verticalScrollBar()
                self.assertGreater(bar.maximum(), 0)
                bar.setValue(bar.maximum()); bottom = bar.value()
                button = window.table.cellWidget(6, 5).findChild(QPushButton)
                for _ in range(3):
                    window.poll(); QApplication.processEvents()
                    self.assertEqual(bar.value(), bottom)
                    self.assertIs(window.table.cellWidget(6, 5).findChild(QPushButton), button)
                button.click(); QApplication.processEvents()
                self.assertNotIn(rows[-1]['id'], window.selected_ids)
                self.assertGreater(bar.value(), 0)
                window.poll(); QApplication.processEvents()
                self.assertNotIn(rows[-1]['id'], window.selected_ids)
                window.undo_remove(); QApplication.processEvents()
                self.assertIn(rows[-1]['id'], window.selected_ids)
                self.assertGreater(bar.value(), 0)
                report['assistants'][0]['error'] = 'late final detail'
                summary.write_text(json.dumps(report), encoding='utf-8')
                window.poll(); QApplication.processEvents()
                self.assertEqual(window.results[rows[0]['id']]['error'], 'late final detail')
                self.assertGreater(bar.value(), 0)
                window.recheck_timer.stop(); window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QProcess, 'start', side_effect=AssertionError('no process launch')), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_bettergi_registration_link_saves_paths_without_launch_or_selection_changes(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QApplication, QCheckBox, QFileDialog, QMessageBox, QPushButton
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'fixture.json'
            rows = [dict(id='wuthering-waves', adapter='ok-ww', installation_root=str(root), enabled=True),
                    dict(id='genshin', adapter='bettergi', installation_root=None, enabled=False)]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')
            registered = dict(schema_version=1, assistants=[rows[0], dict(rows[1], installation_root=str(root),
                              entry_executable=str(root / 'BetterGI.exe'), game_start='assistant')])
            prepared = {'runner_version': '0.66.0.0', 'instance_name': '默认配置',
                        'game_path': str(root / 'GenshinImpact.exe'), 'blockers': []}
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop(); window.set_view('all')
                cell = window.table.cellWidget(1, 0)
                self.assertFalse(cell.findChild(QCheckBox).isEnabled())
                link = window.table.cellWidget(1, 2).findChild(QPushButton)
                self.assertEqual(link.text(), '去接入 →')
                with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(root / 'BetterGI.exe'), '')), \
                     patch.object(desktop, 'register_bettergi', return_value=(registered, prepared)), \
                     patch.object(QMessageBox, 'information') as info, \
                     patch.object(QDesktopServices, 'openUrl') as opened:
                    link.click()
                    self.assertTrue(info.called); opened.assert_not_called()
                saved = json.loads((Path(window.profiles.currentData())).read_text(encoding='utf-8'))
                self.assertEqual(saved, registered)
                self.assertEqual(window.selected_ids, ['wuthering-waves'])
                self.assertEqual(Path(window.profiles.currentData()).name, 'config.json')
                window.set_view('all')
                self.assertTrue(window.table.cellWidget(1, 0).findChild(QCheckBox).isEnabled())
                self.assertEqual(window.table.cellWidget(1, 2).findChild(QPushButton).text(), '设置接入')
                with patch.object(QFileDialog, 'getOpenFileName', return_value=('', '')):
                    window.register_local_bettergi('genshin')
                self.assertEqual(json.loads((Path(window.profiles.currentData())).read_text(encoding='utf-8')), saved)
                window.recheck_timer.stop(); window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QProcess, 'start', side_effect=AssertionError('no process launch')), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_reason_links_are_scoped_and_dialog_contains_tasks(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPlainTextEdit
        from gachaops import desktop
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / 'fixture.json'
            rows = [dict(id='wuthering-waves', adapter='ok-ww', installation_root=str(root), enabled=True,
                         game_start='assistant'),
                    dict(id='zenless-zone-zero', adapter='zzz-onedragon', installation_root=str(root),
                         game_path=str(root / 'fixture-game.exe'), enabled=True, game_start='assistant')]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')
            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                window.results = {row['id']: {'id': row['id'], 'status': 'partial_failure',
                    'completion': {'failed_applications': ['task-' + row['id']]}} for row in rows}
                with patch.object(window, 'open_result_reason') as open_reason:
                    boxes = [window.status_cell_running(row, 'partial_failure', window.results[row['id']], False, None)
                             for row in rows]
                    for box in boxes:
                        next(label for label in box.findChildren(QLabel) if label.text() == '查看原因').mousePressEvent(None)
                    self.assertEqual([call.args[0] for call in open_reason.call_args_list], [r['id'] for r in rows])
                    window.set_problems(window.problem_entries(list(window.results.values())))
                    for label in window.problems_box.findChildren(QLabel):
                        if label.text() == '查看原因': label.mousePressEvent(None)
                    self.assertEqual([call.args[0] for call in open_reason.call_args_list][-2:], [r['id'] for r in rows])
                texts = []
                def inspect_dialog(dialog):
                    texts.append(dialog.findChild(QPlainTextEdit).toPlainText())
                    return 0
                with patch.object(QDialog, 'exec', inspect_dialog):
                    window.open_result_reason(rows[1]['id'])
                self.assertIn('task-zenless-zone-zero', texts[0])
                self.assertNotIn('task-wuthering-waves', texts[0])
                window.close()
            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QProcess, 'start', side_effect=AssertionError('no process launch')), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def _release_qt(self):
        # PySide6 pins the QApplication singleton (builtins.qApp); drop it so the
        # next Qt test in this file can create another instance.
        import builtins
        import gc
        import shiboken6
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            shiboken6.delete(app)
        if getattr(builtins, 'qApp', None) is not None:
            del builtins.qApp
        gc.collect()

    def test_selection_order_progress_stop_and_return_to_idle(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton
        from gachaops import desktop

        class Process:
            active = True
            def state(self):
                return QProcess.ProcessState.Running if self.active else QProcess.ProcessState.NotRunning

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'fixture.json'
            rows = [dict(id='wuthering-waves', adapter='ok-ww', installation_root=str(root), enabled=True,
                         game_start='assistant'),
                    dict(id='zenless-zone-zero', adapter='zzz-onedragon', installation_root=str(root),
                         game_path=str(root / 'fixture-game.exe'), enabled=False, game_start='assistant')]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')

            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                # Idle opens in the selected view mirroring the loaded profile.
                self.assertEqual(window.view, 'selected')
                self.assertEqual(window.table.rowCount(), 1)
                self.assertFalse(window.table.cellWidget(0, 0).findChildren(QCheckBox))

                # All view: checkbox-only first column, aligned for every row.
                window.set_view('all')
                self.assertEqual(window.table.rowCount(), 2)
                window.table.cellWidget(0, 0).findChild(QCheckBox).setChecked(False)
                window.table.cellWidget(1, 0).findChild(QCheckBox).setChecked(True)
                self.assertEqual(window.selected_ids, ['zenless-zone-zero'])
                self.assertEqual([r['enabled'] for r in window.selection()['assistants']], [False, True])

                # Selected view: drag order via visible positions.
                window.table.cellWidget(0, 0).findChild(QCheckBox).setChecked(True)
                window.set_view('selected')
                self.assertEqual(window.table.rowCount(), 2)
                window.move_row(1, 0)
                self.assertEqual(window.selection()['assistants'][0]['id'], 'zenless-zone-zero')
                self.assertEqual(window.selected_rows()[0]['id'], 'zenless-zone-zero')
                window.move_row(0, 1)
                self.assertEqual(window.selection()['assistants'][1]['id'], 'zenless-zone-zero')

                # Removal with undo; removing everything returns to the all view.
                window.table.cellWidget(1, 5).findChild(QPushButton).click()
                self.assertEqual(window.selected_ids, ['wuthering-waves'])
                self.assertTrue(window.undo_label.isVisibleTo(window.table_card) or window.undo_info is not None)
                window.undo_remove()
                self.assertEqual(len(window.selected_ids), 2)
                window.table.cellWidget(0, 5).findChild(QPushButton).click()
                window.table.cellWidget(0, 5).findChild(QPushButton).click()
                self.assertEqual(window.view, 'all')

                # Selected view with nothing selected shows the empty state.
                window.set_view('selected')
                self.assertEqual(window.table.rowCount(), 0)
                self.assertTrue(window.empty_state.isVisibleTo(window.table_card))
                self.assertFalse(window.table_header.isVisible())
                window.go_select.click()
                self.assertEqual(window.view, 'all')
                self.assertEqual(window.table.rowCount(), 2)
                window.table.cellWidget(1, 0).findChild(QCheckBox).setChecked(True)
                window.move_row(1, 0)

                # Collapsed passing summary with a single arrow; problems auto-expand.
                window.check_report = {'assistants': [{'id': 'zenless-zone-zero', 'status': 'ready'}]}
                window.check_collapsed = True
                window.refresh_checklist()
                self.assertEqual(window.checklist.count(), 1)
                summary = window.checklist.itemAt(0).widget()
                self.assertTrue(any('检查通过' in label.text() for label in summary.findChildren(QLabel)))
                window.check_report = {'assistants': [{'id': 'zenless-zone-zero', 'status': 'blocked',
                                                      'blockers': ['desktop_environment_interrupted']}]}
                window.refresh_checklist()
                self.assertFalse(window.check_collapsed)
                window.recheck_timer.stop()

                window.map_log_line('Running: 60s elapsed; game detected: True')
                self.assertGreaterEqual(window.node_elapsed(), 60)
                window.map_log_line('[2/2] wuthering-waves: checking saved assistant entry...')
                self.assertLess(window.node_elapsed(), 1)

                window.process = Process(); window.action = 'run-assistants'; window.session = root
                window.started = time.monotonic()
                window.refresh_controls()
                self.assertTrue(window.start.isHidden())
                self.assertFalse(window.progress_card.isHidden())
                self.assertFalse(window.result.isEnabled())
                self.assertFalse(window.seg_box.isVisibleTo(window.table_card))
                window.render_running_state({}, {'id': 'zenless-zone-zero', 'status': 'running'})
                self.assertTrue(window.table.isColumnHidden(5))
                window.request_stop()
                self.assertTrue((root / 'stop.request').exists())
                window.render_running_state({}, {'id': 'zenless-zone-zero', 'status': 'running'})
                self.assertIn('停止中', window.running_title.text())

                window.process.active = False
                result = dict(id='zenless-zone-zero', status='partial_failure', duration_seconds=75)
                window.results = {result['id']: result}
                window.render_finished_states(dict(status='completed_with_failures', assistants=[result]))
                self.assertFalse(window.start.isHidden())
                self.assertTrue(window.start.isEnabled())
                self.assertTrue(window.progress_card.isHidden())
                self.assertTrue(window.result.isEnabled())
                labels = window.table.cellWidget(0, 4).findChildren(QLabel)
                self.assertIn('部分失败', [label.text() for label in labels])
                self.assertTrue(window.problems_box.isVisibleTo(window.table_card))
                self.assertFalse(window.recheck_timer.isActive(), 'Rendering must not schedule another check')
                previous_session = window.session
                check_session = root / 'check'; check_session.mkdir()
                (check_session / 'worker.log').write_text(json.dumps({'assistants': [
                    {'id': 'zenless-zone-zero', 'status': 'ready'}]}), encoding='utf-8')
                window.check_origin = (previous_session, window.summary_path)
                window.session = check_session; window.action = 'check-assistants'
                window.process.active = True
                window.selection_changed(schedule_check=False)
                self.assertEqual(window.start.text(), '检查中…')
                window.process.active = False
                window.finished(0, QProcess.ExitStatus.NormalExit)
                self.assertEqual(window.session, previous_session)
                self.assertFalse(window.recheck_timer.isActive(), 'Check completion must not loop')
                self.assertTrue(window.problems_box.isVisibleTo(window.table_card), 'Precheck must preserve previous failures')
                window.recheck_timer.stop(); window.close()

            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)

        self._release_qt()

    def test_assistant_icons_bundled_with_letter_badge_fallback(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QLabel
        from gachaops import desktop

        for game_id in desktop.NAMES:
            self.assertTrue((desktop.ICON_DIR / f'{game_id}.png').is_file(), game_id)

        class Process:
            active = True
            def state(self):
                return QProcess.ProcessState.Running if self.active else QProcess.ProcessState.NotRunning

        def icon_labels(cell):
            return [label for label in cell.findChildren(QLabel)
                    if label.pixmap() is not None and not label.pixmap().isNull()]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'fixture.json'
            rows = [dict(id='wuthering-waves', adapter='ok-ww', installation_root=str(root), enabled=True,
                         game_start='assistant'),
                    dict(id='genshin', adapter='bettergi', installation_root=None, enabled=False,
                         game_start='assistant')]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')

            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                window.set_view('all')
                self.assertEqual(window.table.rowCount(), 2)
                # Enabled row: circular game icon instead of the letter badge.
                active = icon_labels(window.table.cellWidget(0, 1))
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0].pixmap().width(), 30)
                self.assertEqual(active[0].text(), '')
                # An unregistered row keeps its icon but uses the disabled palette.
                pending = icon_labels(window.table.cellWidget(1, 1))
                self.assertEqual(len(pending), 1)
                available = window.identity_cell(dict(id='genshin', adapter='bettergi'), False, None)
                washed = pending[0].pixmap().toImage()
                unwashed = [label for label in available.findChildren(QLabel)
                            if label.pixmap() is not None and not label.pixmap().isNull()][0].pixmap().toImage()
                self.assertNotEqual(washed.pixelColor(15, 15), unwashed.pixelColor(15, 15))
                # A missing icon resource degrades to the first-letter badge.
                with patch.object(desktop, 'ICON_DIR', root / 'no-icons'):
                    fallback = window.identity_cell(dict(id='endfield', adapter='maa-end'), False, None)
                badges = [label for label in fallback.findChildren(QLabel) if label.text() == '终']
                self.assertTrue(badges and badges[0].pixmap().isNull())
                window.recheck_timer.stop(); window.close()

            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_idle_views_share_columns_and_full_row_tint(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QApplication
        from gachaops import desktop

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'fixture.json'
            rows = [dict(id='wuthering-waves', adapter='ok-ww', installation_root=str(root), enabled=True,
                         game_start='assistant'),
                    dict(id='zenless-zone-zero', adapter='zzz-onedragon', installation_root=str(root),
                         game_path=str(root / 'fixture-game.exe'), enabled=True, game_start='assistant')]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')

            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                header = window.table.horizontalHeader()
                window.set_view('all')
                QApplication.processEvents()
                self.assertFalse(window.table.isColumnHidden(5))
                positions = [header.sectionViewportPosition(column) for column in (2, 3, 4)]
                window.set_view('selected')
                QApplication.processEvents()
                self.assertFalse(window.table.isColumnHidden(5))
                self.assertEqual([header.sectionViewportPosition(column) for column in (2, 3, 4)], positions)
                # The selected-view tint covers the remove column too (HYB-46).
                self.assertEqual(window.table.item(0, 5).background().color(), QColor('#f0f6ff'))
                window.recheck_timer.stop(); window.close()

            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_run_view_fits_rows_and_keeps_icons_colored(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QLabel
        from gachaops import desktop

        class Process:
            active = True
            def state(self):
                return QProcess.ProcessState.Running if self.active else QProcess.ProcessState.NotRunning

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'fixture.json'
            entries = [('wuthering-waves', 'ok-ww'), ('zenless-zone-zero', 'zzz-onedragon'),
                       ('honkai-star-rail', 'march7'), ('endfield', 'maa-end'),
                       ('neverness-to-everness', 'ok-nte'), ('girls-frontline-2', 'maa-gf2')]
            rows = []
            for game_id, adapter in entries:
                row = dict(id=game_id, adapter=adapter, installation_root=str(root), enabled=True,
                           game_start='assistant')
                if adapter == 'zzz-onedragon':
                    row['game_path'] = str(root / 'fixture-game.exe')
                rows.append(row)
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')

            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                window.set_view('all')
                QApplication.processEvents()
                self.assertGreaterEqual(window.table.viewport().height(), 320)
                window.process = Process(); window.action = 'run-assistants'; window.session = root
                window.started = time.monotonic()
                window.refresh_controls()
                window.render_running_state({}, {'id': 'wuthering-waves', 'status': 'running'})
                QApplication.processEvents()
                self.assertTrue(window.table.isColumnHidden(5))
                # The list keeps vertical priority at the 1080x894 reference size;
                # six 52px rows were the Windows fixture complaint (HYB-6/35).
                self.assertGreaterEqual(window.table.viewport().height(), 260)
                # The table stays usable in run state (reason entry, HYB-33); real
                # icons must keep their colors (HYB-18).
                self.assertTrue(window.table.isEnabled())
                avatar = [label for label in window.table.cellWidget(0, 1).findChildren(QLabel)
                          if label.pixmap() is not None and not label.pixmap().isNull()][0]
                rendered = avatar.grab().toImage()
                self.assertEqual(rendered.pixelColor(15, 15),
                                 avatar.pixmap().toImage().pixelColor(15, 15))
                window.recheck_timer.stop(); window.close()

            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_display_translations_and_duration_format(self):
        from gachaops import desktop

        self.assertEqual(desktop.fmt_dur(415), '6:55')
        self.assertEqual(desktop.fmt_dur(7200), '2:00:00')
        self.assertEqual(desktop.fmt_dur(0), '0:00')
        detail = desktop.sanitize_detail('[Errno 2] C:\\SyntheticFixture\\y.exe at 0x7ff gachaops.preflight.check')
        self.assertNotIn('C:\\', detail); self.assertNotIn('0x7ff', detail); self.assertNotIn('gachaops.', detail)
        self.assertIn('系统调用失败', detail); self.assertIn('程序文件路径', detail)
        self.assertEqual(desktop.sanitize_detail('Traceback (most recent call last): boom'),
                         '程序内部异常，详情见运行日志')
        self.assertEqual(desktop.SEG_COLORS['cancelled'], '#64748b')
        for status in ('timeout', 'completion_unconfirmed', 'desktop_environment_interrupted',
                       'ownership_or_cleanup_unconfirmed', 'stopped_cleanup_unconfirmed', 'interrupted'):
            self.assertEqual(desktop.SEG_COLORS[status], '#c77d00', status)
        self.assertEqual(desktop.SEG_COLORS['execution_error'], '#c0392b')
        self.assertEqual(desktop.RUNNING_PILL['stopped_cleanup_unconfirmed'][0], '清理未确认')
        self.assertEqual(desktop.RUNNING_PILL['execution_error'][0], '运行异常')
        # Elapsed floors and remaining ceils add up to the budget with no lost second (HYB-47).
        elapsed, budget = 415.4, 1200
        self.assertEqual(int(elapsed) + math.ceil(budget - elapsed), budget)

    def test_review_round_three_ui_fixes(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QLabel
        from gachaops import desktop

        class Process:
            active = True
            def state(self):
                return QProcess.ProcessState.Running if self.active else QProcess.ProcessState.NotRunning

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'fixture.json'
            rows = [dict(id='wuthering-waves', adapter='ok-ww', installation_root=str(root), enabled=True,
                         game_start='assistant'),
                    dict(id='zenless-zone-zero', adapter='zzz-onedragon', installation_root=str(root),
                         game_path=str(root / 'fixture-game.exe'), enabled=False, game_start='assistant')]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')

            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                # Retained problems hide the idle placeholder (HYB-41).
                window.set_problems([dict(name='鸣潮', summary='任务完成，含失败项', detail='情报板')])
                self.assertTrue(window.problems_box.isVisibleTo(window.run_card))
                self.assertFalse(window.run_placeholder.isVisibleTo(window.run_card))
                window.set_problems([])
                self.assertTrue(window.run_placeholder.isVisibleTo(window.run_card))
                # Removal holds the auto recheck until the undo window is over (HYB-43).
                window.set_view('selected')
                window.remove_assistant('wuthering-waves')
                self.assertEqual(window.recheck_timer.interval(), 6000)
                self.assertTrue(window.recheck_timer.isActive())
                self.assertTrue(window.undo_label.isVisibleTo(window.table_card))
                window.undo_remove()
                self.assertEqual(window.selected_ids, ['wuthering-waves'])
                # Fixed primary-button width (HYB-52).
                self.assertGreaterEqual(window.start.minimumWidth(), 160)
                # Suggestions match the blocker; extra blockers are labelled as such (HYB-54).
                window.check_report = {'assistants': [dict(id='wuthering-waves', status='blocked',
                                                           blockers=['official_launcher_path_unverified',
                                                                     'existing_runner_not_exclusive'])]}
                window.check_collapsed = False
                window.refresh_checklist()
                texts = []
                for index in range(window.checklist.count()):
                    texts.extend(label.text() for label in
                                 window.checklist.itemAt(index).widget().findChildren(QLabel))
                self.assertTrue(any('官方启动器路径需要检查' in text for text in texts))
                self.assertTrue(any('核实官方启动器路径' in text for text in texts))
                self.assertTrue(any('另外：请退出已有助手' in text for text in texts))
                # Partial-failure rows expose an in-row reason entry (HYB-33).
                window.process = Process(); window.action = 'run-assistants'; window.session = root
                window.started = time.monotonic()
                window.node_started = time.monotonic() - 415.4
                window.results = {'wuthering-waves': dict(status='partial_failure', duration_seconds=113,
                                                          completion={'failed_applications': ['情报板']})}
                window.refresh_controls()
                window.render_running_state({}, {'id': 'wuthering-waves'})
                QApplication.processEvents()
                self.assertEqual(window.stat_labels['node'].text(), '6:55')
                self.assertEqual(window.stat_labels['left'].text(), '23:05')
                cell = window.table.cellWidget(0, 4)
                self.assertTrue([label for label in cell.findChildren(QLabel) if label.text() == '查看原因'])
                window.recheck_timer.stop(); window.close()

            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_review_round_four_fixes(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication, QCheckBox, QLabel
        from gachaops import desktop

        class Process:
            active = True
            def state(self):
                return QProcess.ProcessState.Running if self.active else QProcess.ProcessState.NotRunning

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'fixture.json'
            rows = [dict(id='wuthering-waves', adapter='ok-ww', installation_root=str(root), enabled=True,
                         game_start='assistant'),
                    dict(id='zenless-zone-zero', adapter='zzz-onedragon', installation_root=str(root),
                         game_path=str(root / 'fixture-game.exe'), enabled=True, game_start='assistant'),
                    dict(id='honkai-star-rail', adapter='march7', installation_root=str(root), enabled=True,
                         game_start='assistant')]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')

            def pill_text(cell):
                pills = [label for label in cell.findChildren(QLabel) if label.text() in
                         ('排队中', '需处理', '部分失败', '已完成', '超时', '失败', '结果未确认',
                          '运行中', '已停止', '清理未确认', '运行异常', '已中断', '桌面中断',
                          '手动游戏仍在运行')]
                return pills[0] if pills else None

            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                # Undo after removing the last selected item re-checks the box in
                # the all view (HYB-43 review case).
                window.set_view('selected')
                window.remove_assistant('wuthering-waves')
                window.remove_assistant('zenless-zone-zero')
                window.remove_assistant('honkai-star-rail')
                self.assertEqual(window.view, 'all')
                checkbox = window.table.cellWidget(2, 0).findChild(QCheckBox)
                self.assertFalse(checkbox.isChecked())
                window.undo_remove()
                checkbox = window.table.cellWidget(2, 0).findChild(QCheckBox)
                self.assertTrue(checkbox.isChecked())
                self.assertIn('honkai-star-rail', window.selected_ids)
                window.set_selected('wuthering-waves', True)
                window.set_selected('zenless-zone-zero', True)
                # Not-started rows stay queued; only unknown terminals read as
                # 需处理 (HYB-37 review case).
                window.process = Process(); window.action = 'run-assistants'; window.session = root
                window.started = time.monotonic()
                window.results = {'wuthering-waves': dict(status='partial_failure', duration_seconds=113,
                                                          completion={'failed_applications': ['情报板']}),
                                  'honkai-star-rail': dict(status='mystery_terminal')}
                window.refresh_controls()
                window.render_running_state({}, {'id': 'zenless-zone-zero', 'status': 'running'})
                QApplication.processEvents()
                # Run rows keep the reason entry clickable (HYB-33 review case 1).
                self.assertTrue(window.table.isEnabled())
                cell = window.table.cellWidget(0, 4)
                link = [label for label in cell.findChildren(QLabel) if label.text() == '查看原因'][0]
                self.assertTrue(link.isEnabled())
                # Pill and detail line share the 52px row without clipping (case 2).
                pill = pill_text(cell)
                self.assertIsNotNone(pill)
                self.assertGreaterEqual(pill.height(), pill.minimumSizeHint().height())
                self.assertEqual(pill_text(window.table.cellWidget(1, 4)).text(), '排队中')
                self.assertEqual(pill_text(window.table.cellWidget(2, 4)).text(), '需处理')
                window.recheck_timer.stop(); window.close()

            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_review_round_five_fixes(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QProcess, Qt
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QApplication
        from gachaops import desktop

        class Process:
            active = True
            def state(self):
                return QProcess.ProcessState.Running if self.active else QProcess.ProcessState.NotRunning

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'fixture.json'
            entries = [('wuthering-waves', 'ok-ww'), ('zenless-zone-zero', 'zzz-onedragon'),
                       ('honkai-star-rail', 'march7'), ('endfield', 'maa-end'),
                       ('neverness-to-everness', 'ok-nte'), ('girls-frontline-2', 'maa-gf2')]
            rows = []
            for game_id, adapter in entries:
                row = dict(id=game_id, adapter=adapter, installation_root=str(root), enabled=True,
                           game_start='assistant')
                if adapter == 'zzz-onedragon':
                    row['game_path'] = str(root / 'fixture-game.exe')
                rows.append(row)
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')

            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                window.table.set_hover_row(None)
                window.set_view('all')
                window.set_selected('zenless-zone-zero', False)
                QApplication.processEvents()
                window.table._hover_timer.stop()
                window.table.set_hover_row(None)
                # Row hover covers the whole row; blue still means selected only (HYB-17).
                window.table.selectRow(1)
                self.assertEqual(window.table.item(1, 4).background().style(), Qt.BrushStyle.NoBrush)
                self.assertEqual(window.table.item(0, 4).background().color().name(), '#f0f6ff')
                window.table.set_hover_row(1)
                self.assertEqual(window.table.item(1, 4).background().color().name(), '#f5f8fc')
                self.assertEqual(window.table.item(0, 4).background().color().name(), '#f0f6ff')
                window.table.set_hover_row(0)
                self.assertEqual(window.table.item(0, 4).background().color().name(), '#e2ecfd')
                window.table.set_hover_row(None)
                self.assertEqual(window.table.item(0, 4).background().color().name(), '#f0f6ff')
                self.assertEqual(window.table.item(1, 4).background().style(), Qt.BrushStyle.NoBrush)
                # Six rows stay fully visible with three problems and log content (HYB-6).
                window.set_selected('zenless-zone-zero', True)
                window.process = Process(); window.action = 'run-assistants'; window.session = root
                window.started = time.monotonic()
                window.refresh_controls()
                window.render_running_state({}, {'id': 'endfield', 'status': 'running'})
                window.set_problems([dict(name=f'游戏{n}', summary='部分任务失败', detail='用时 59:37')
                                     for n in range(3)])
                for line in range(4):
                    window.log.appendPlainText(f'{line:02d}:00:00  [鸣潮] 任务完成，用时 10:20')
                window.resize(1080, 960)
                QApplication.processEvents()
                self.assertGreaterEqual(window.table.viewport().height(), 312)
                window.recheck_timer.stop(); window.close()

            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()

    def test_review_round_six_hover_boundaries(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtCore import QPoint, QProcess, Qt
        from PySide6.QtWidgets import QApplication
        from gachaops import desktop

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / 'fixture.json'
            rows = [dict(id='wuthering-waves', adapter='ok-ww', installation_root=str(root), enabled=True,
                         game_start='assistant'),
                    dict(id='zenless-zone-zero', adapter='zzz-onedragon', installation_root=str(root),
                         game_path=str(root / 'fixture-game.exe'), enabled=True, game_start='assistant')]
            path.write_text(json.dumps(dict(schema_version=1, assistants=rows)), encoding='utf-8')

            def exercise(window):
                window.timer.stop(); window.recheck_timer.stop()
                window.table._hover_timer.stop()
                window.table.set_hover_row(None)
                window.set_view('all')
                QApplication.processEvents()
                viewport = window.table.viewport()
                # Polling path: cursor beside the table at the same y must not highlight.
                outside = viewport.mapToGlobal(QPoint(-100, 26))
                window.table._cursor_global_pos = lambda: outside
                window.table.refresh_hover()
                self.assertIsNone(window.table._hover_row)
                self.assertEqual(window.table.item(0, 4).background().color().name(), '#f0f6ff')
                # Cursor inside row 0 highlights it through refresh_hover.
                window.table._cursor_global_pos = lambda: viewport.mapToGlobal(QPoint(10, 26))
                window.table.refresh_hover()
                self.assertEqual(window.table._hover_row, 0)
                self.assertEqual(window.table.item(0, 4).background().color().name(), '#e2ecfd')
                # Re-render rebuilds base colors; the hover paint must come back.
                window.render()
                self.assertEqual(window.table.item(0, 4).background().color().name(), '#e2ecfd')
                # Moving the cursor away clears the highlight again.
                window.table._cursor_global_pos = lambda: outside
                window.table.refresh_hover()
                self.assertIsNone(window.table._hover_row)
                self.assertEqual(window.table.item(0, 4).background().color().name(), '#f0f6ff')
                window.recheck_timer.stop(); window.close()

            with patch.object(desktop, 'workspace_root', return_value=root), \
                 patch.object(desktop, 'elevation_report', return_value={'elevated': True}), \
                 patch.object(desktop, 'PREVIEW_FACTORY', exercise, create=True), \
                 patch.object(QApplication, 'exec', return_value=0), \
                 patch('sys.argv', ['desktop-test', '--config', str(path)]):
                self.assertEqual(desktop.main(), 0)
        self._release_qt()
