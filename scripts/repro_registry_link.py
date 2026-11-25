"""Reproducible synthetic investigation for GitHub issue #4.

Builds temporary, clearly synthetic data roots and drives the real desktop
entry headlessly (Qt offscreen, QProcess refused) to compare how the old
seven-registration configurations connect with the global registry model:

R1  legacy schema 1 profile with all seven registrations -> startup migration
R2  issue state: default profile with one registration + old seven-registration
    file next to it -> startup migration, then import preview/confirm/cancel
R3  explicit --config draft of a legacy file -> run fields only, zero writes
R4  migration prompt cancelled -> zero writes
R5  conflicting candidates and an incomplete zzz registration during migration
R6  after migration: shared registry across profiles and across reopen

Every scenario records a root label, the selected combination, the global
registration count and per-assistant selection/order/timeout/repeat_limit.
Temporary and sample paths are masked to <data-root>/<sample> in the results;
no real installation paths, accounts or games are involved. Never starts an
assistant or a game: QProcess.start is refused and recorded.

Usage: python scripts/repro_registry_link.py --all --out issue4-results.json
       python scripts/repro_registry_link.py --scenario R1 --out R1.json
"""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
ADAPTERS = ('ok-ww', 'bettergi', 'ok-nte', 'maa-end', 'march7', 'maa-gf2', 'zzz-onedragon')


def sample_root(base):
    """Sample installation directories; existence is never required."""
    return base / 'SampleAssistants'


def legacy_rows(base, enabled=False, registered=ADAPTERS, game_for_zzz=True):
    rows = []
    for index, adapter in enumerate(ADAPTERS):
        row = {'id': f'assistant-{index}', 'adapter': adapter,
               'enabled': enabled if not isinstance(enabled, dict) else enabled.get(adapter, False),
               'game_start': 'assistant', 'timeout_seconds': 1800}
        if adapter in registered:
            row['installation_root'] = str(sample_root(base) / adapter)
        if adapter == 'zzz-onedragon' and game_for_zzz and adapter in registered:
            row['game_path'] = str(sample_root(base) / adapter / 'ZenlessZoneZero.exe')
        rows.append(row)
    return rows


def write_legacy_config(path, base, **kwargs):
    document = {'schema_version': 1, 'repeat_limit': 0,
                'assistants': legacy_rows(base, **kwargs)}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    return document


def write_legacy_profile(root, identity, name, base, **kwargs):
    directory = root / 'config/profiles' / identity
    directory.mkdir(parents=True)
    document = {'schema_version': 1, 'repeat_limit': 0,
                'assistants': legacy_rows(base, **kwargs)}
    (directory / 'config.json').write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    (directory / 'name.json').write_text(json.dumps(
        {'name': name, 'created': '2026-01-01T00:00:00+00:00'}), encoding='utf-8')


def mask(path, root, base):
    text = str(path)
    for prefix, label in ((str(root), '<data-root>'), (str(sample_root(base)), '<sample>')):
        if prefix and text.startswith(prefix):
            return label + text[len(prefix):]
    return text


def registry_snapshot(root):
    path = root / 'config/assistant-registry.json'
    if not path.exists():
        return {'exists': False, 'revision': None, 'count': 0, 'adapters': []}
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    return {'exists': True, 'revision': document['revision'],
            'count': len(document['assistants']), 'adapters': sorted(document['assistants'])}


def profile_snapshot(window, root, base):
    rows = []
    for profile in window.profile_store.list_profiles():
        document = window.profile_store.load_profile(profile['id'])
        rows.append({'name': profile['name'], 'id': mask(profile['id'], root, base),
                     'revision': document['revision'],
                     'repeat_limit': document['repeat_limit'],
                     'registered_rows': sum(1 for r in document['assistants']
                                            if r.get('installation_root') or r.get('game_path')),
                     'assistants': [
                         {'adapter': r['adapter'], 'enabled': r.get('enabled', True),
                          'order': index, 'game_start': r.get('game_start', 'assistant'),
                          'timeout_seconds': r.get('timeout_seconds')}
                         for index, r in enumerate(document['assistants'])]})
    return rows


def window_snapshot(window, root, base):
    return {'selected_profile': window.profile_names.get(window.profiles.currentData()),
            'registry': registry_snapshot(root),
            'profiles': profile_snapshot(window, root, base)}


def drive(root, scenario, exercise, argv=(), question='Yes', dialog='Accept'):
    """Run the real desktop entry headlessly; never launches anything.

    Modal dialogs are answered mechanically (question Yes/No, dialog
    Accept/Reject) so the gate can run without a user; scenarios that need
    finer control patch the specific dialogs inside their exercise hook.
    """
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from unittest.mock import patch
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    from gachaops import desktop
    launched = []
    messages = []
    answers = {'Yes': QMessageBox.StandardButton.Yes, 'No': QMessageBox.StandardButton.No}
    codes = {'Accept': QDialog.DialogCode.Accepted, 'Reject': QDialog.DialogCode.Rejected}

    def record(kind):
        def handler(*args, **kwargs):
            messages.append({'kind': kind, 'title': args[1] if len(args) > 1 else '',
                             'text': args[2] if len(args) > 2 else ''})
            return answers[question] if kind == 'question' else None
        return handler

    def answer_dialog(dialog_instance):
        # Resolve unresolved conflict combinations like a user choosing the
        # first candidate, then accept/reject; otherwise the migration dialog
        # would recurse on an unresolved conflict.
        from PySide6.QtWidgets import QComboBox
        for box in dialog_instance.findChildren(QComboBox):
            if box.currentData() is None and box.count() > 1:
                box.setCurrentIndex(1)
        return codes[dialog]

    if os.environ.get('REPRO_DEBUG'):
        import faulthandler
        faulthandler.dump_traceback_later(30, exit=True)

    def wrapped(window):
        exercise(window)
        # Closing the window synchronously (before app.exec starts) consumes
        # quitOnLastWindowClosed too early; schedule a real quit inside the
        # loop. 200 ms stays below the 900 ms auto-recheck so nothing launches.
        from PySide6.QtCore import QTimer
        live = QApplication.instance()
        if live is not None:
            QTimer.singleShot(200, live.quit)

    with patch.object(QProcess, 'start',
                      side_effect=lambda *a, **k: launched.append(a) or None):
        with patch.object(QMessageBox, 'question', record('question')), \
             patch.object(QMessageBox, 'critical', record('critical')), \
             patch.object(QMessageBox, 'warning', record('warning')), \
             patch.object(QMessageBox, 'information', record('information')), \
             patch.object(QDialog, 'exec', answer_dialog):
            with patch.object(sys, 'argv',
                              ['desktop'] + list(argv) + ['--data-root', str(root)]):
                with patch.object(desktop, 'PREVIEW_FACTORY', wrapped, create=True):
                    try:
                        code = desktop.main()
                    except SystemExit as exit_code:  # the gate's decline path
                        code = exit_code.code
    release_qt()
    return {'exit_code': code, 'launches': launched, 'dialogs': messages}


def release_qt():
    """Drop the QApplication singleton so the next scenario can build one."""
    import builtins
    import gc
    import shiboken6
    from PySide6.QtWidgets import QApplication
    application = QApplication.instance()
    if application is not None:
        shiboken6.delete(application)
    if getattr(builtins, 'qApp', None) is not None:
        del builtins.qApp
    gc.collect()


SCENARIOS = {}


def scenario(name):
    def register(function):
        SCENARIOS[name] = function
        return function
    return register


@scenario('R1')
def run_r1(out):
    """Old profile with all seven registrations: normal startup migration."""
    from PySide6.QtWidgets import QDialog
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'; root.mkdir()
        write_legacy_profile(root, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '旧七登记组合', base)
        before = registry_snapshot(root)

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            snapshot = window_snapshot(window, root, base)
            snapshot['notes'] = '启动闸门迁移确认后：七项全部进入全局登记；组合转为运行字段'
            window.close()
            out['snapshot'] = snapshot

        result = drive(root, 'R1', exercise)
        out.update(result)
        out['registry_before'] = before
        out['registry_after'] = registry_snapshot(root)


@scenario('R2')
def run_r2(out):
    """Issue #4 state: default profile with one registration plus the old
    seven-registration file; startup migration then import preview/cancel/confirm."""
    from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'; root.mkdir()
        # Default combination with only ok-ww registered (the observed state).
        write_legacy_profile(root, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '默认组合', base, registered=('ok-ww',))
        # The old complete configuration survives next to the data root.
        old_file = Path(temp) / '旧七登记配置.json'
        old_document = write_legacy_config(old_file, base)
        events = []

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            out['after_startup'] = window_snapshot(window, root, base)
            # 导入 -> 预览 -> 取消: zero writes.
            digests = {p: p.read_bytes() for p in [
                root / 'config/assistant-registry.json', root / 'config/profiles' / 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa' / 'config.json']}
            with contextlib.ExitStack() as stack:
                for manager in patch_dialogs(events, file_path=str(old_file),
                                             question=QMessageBox.StandardButton.No,
                                             import_dialog=False):
                    stack.enter_context(manager)
                window.browse()
            out['after_cancel'] = {'zero_write': all(
                p.read_bytes() == d for p, d in digests.items()),
                'events': list(events)}
            # 导入 -> 预览 -> 冲突选择"使用导入路径" -> 确认.
            events.clear()
            with contextlib.ExitStack() as stack:
                for manager in patch_dialogs(events, file_path=str(old_file),
                                             question=QMessageBox.StandardButton.Yes,
                                             import_dialog=True, conflict_choice='import'):
                    stack.enter_context(manager)
                window.browse()
            out['after_confirm'] = window_snapshot(window, root, base)
            out['after_confirm']['events'] = list(events)
            window.close()

        result = drive(root, 'R2', exercise)
        out.update(result)
        out['old_file_rows'] = len(old_document['assistants'])
        out['registry_after_all'] = registry_snapshot(root)


def patch_dialogs(events, file_path, question, import_dialog, conflict_choice=None):
    from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QMessageBox
    from unittest.mock import patch

    def open_file(*args, **kwargs):
        events.append('文件选择')
        return (file_path, '')

    def ask(*args, **kwargs):
        events.append(f'确认对话框: {"Yes" if question == QMessageBox.StandardButton.Yes else "No"}')
        return question

    def exec_dialog(dialog):
        for box in dialog.findChildren(QComboBox):
            if conflict_choice is not None:
                index = next((i for i in range(box.count())
                              if box.itemData(i) == conflict_choice), None)
                if index is not None and box.currentIndex() != index:
                    box.setCurrentIndex(index)
                    events.append(f'冲突选择: {conflict_choice}')
        events.append('迁移/导入对话框: 确认')
        return QDialog.DialogCode.Accepted

    return patch.object(QFileDialog, 'getOpenFileName', open_file), \
        patch.object(QMessageBox, 'question', ask), \
        patch.object(QDialog, 'exec', exec_dialog)


@scenario('R3')
def run_r3(out):
    """Explicit --config draft of a legacy file: run fields only, zero writes."""
    from PySide6.QtWidgets import QApplication
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'; root.mkdir()
        from gachaops.registry import RegistrationStore
        RegistrationStore(root).create()
        from gachaops.profiles import ProfileStore
        ProfileStore(root).create_profile('默认组合')
        legacy = Path(temp) / '旧七登记配置.json'
        write_legacy_config(legacy, base)
        before = registry_snapshot(root)

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            snapshot = window_snapshot(window, root, base)
            snapshot['draft_active'] = window._draft_active()
            # Saving the draft creates a new profile without touching the registry.
            window.create_profile_named('草稿保存组合')
            snapshot['after_save'] = window_snapshot(window, root, base)
            window.close()
            out['snapshot'] = snapshot

        result = drive(root, 'R3', exercise, argv=['--config', str(legacy)])
        out.update(result)
        out['registry_before'] = before
        out['registry_after'] = registry_snapshot(root)


@scenario('R4')
def run_r4(out):
    """Declining the migration prompt: zero writes, program exits."""
    from PySide6.QtWidgets import QDialog, QMessageBox
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'; root.mkdir()
        write_legacy_profile(root, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '旧组合', base)
        tracked = sorted(root.rglob('*.json'))
        digests = {p: p.read_bytes() for p in tracked}

        def exercise(window):  # never reached: the gate exits on decline
            out['unreachable'] = True

        result = drive(root, 'R4', exercise, dialog='Reject')
        out.update(result)
        out['zero_write'] = all(p.read_bytes() == d for p, d in digests.items())
        out['files_tracked'] = len(tracked)


@scenario('R5')
def run_r5(out):
    """Conflicting candidates need an explicit choice; an incomplete zzz
    registration stays 待接入 without blocking."""
    from PySide6.QtWidgets import QDialog, QMessageBox
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'; root.mkdir()
        write_legacy_profile(root, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '组合甲', base,
                             enabled=True, registered=ADAPTERS)
        write_legacy_profile(root, 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', '组合乙', base,
                             registered=('ok-ww',), game_for_zzz=False)
        # 组合乙's ok-ww row carries a different path than 组合甲's.
        document = json.loads((root / 'config/profiles' / 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' / 'config.json').read_text(encoding='utf-8'))
        document['assistants'][0]['installation_root'] = str(sample_root(base) / 'ok-ww-other')
        (root / 'config/profiles' / 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' / 'config.json').write_text(
            json.dumps(document, ensure_ascii=False), encoding='utf-8')
        events = []

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            snapshot = window_snapshot(window, root, base)
            zzz = next(row for profile in snapshot['profiles'] for row in profile['assistants']
                       if row['adapter'] == 'zzz-onedragon')
            snapshot['zzz_after'] = zzz
            snapshot['events'] = list(events)
            window.close()
            out['snapshot'] = snapshot

        def exec_dialog(dialog):
            from PySide6.QtWidgets import QComboBox
            for box in dialog.findChildren(QComboBox):
                if box.count() > 1 and box.currentData() is None:
                    box.setCurrentIndex(1)  # first explicit candidate
                    events.append(f'冲突选择: 候选1 {box.itemText(1)[:20]}')
            return QDialog.DialogCode.Accepted

        from unittest.mock import patch
        with patch.object(QDialog, 'exec', exec_dialog), \
             patch.object(QMessageBox, 'question',
                          return_value=QMessageBox.StandardButton.Yes):
            result = drive(root, 'R5', exercise)
        out.update(result)
        out['registry_after'] = registry_snapshot(root)


@scenario('R6')
def run_r6(out):
    """After migration the registry is shared across combinations and reopen."""
    from PySide6.QtWidgets import QDialog
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'; root.mkdir()
        write_legacy_profile(root, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', '组合甲', base, enabled=True)
        write_legacy_profile(root, 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', '组合乙', base)

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            out['after_migration'] = window_snapshot(window, root, base)
            # A second combination composes from the same registry.
            window.create_profile_named('新组合')
            out['after_new_profile'] = window_snapshot(window, root, base)
            window.close()

        out.update(drive(root, 'R6', exercise))
        # Reopen: a fresh desktop process sees the same registrations.
        second = {}
        from PySide6.QtWidgets import QApplication
        QApplication.quit()

        def exercise2(window):
            window.timer.stop(); window.recheck_timer.stop()
            second['snapshot'] = window_snapshot(window, root, base)
            window.close()

        result = drive(root, 'R6', exercise2)
        out['reopen'] = second.get('snapshot')
        out['reopen_exit'] = result['exit_code']
        out['launches'] = result['launches']


def run_scenario_in_process(name, out_path):
    out = {'scenario': name, 'pid': os.getpid()}
    SCENARIOS[name](out)
    Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--scenario', choices=sorted(SCENARIOS))
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--out', default='issue4-results.json')
    arguments = parser.parse_args()
    if arguments.scenario:
        out = run_scenario_in_process(arguments.scenario, arguments.out)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if not arguments.all:
        parser.error('choose --scenario NAME or --all')
    merged = {}
    for name in sorted(SCENARIOS):
        out_path = Path(arguments.out).with_name(f'{Path(arguments.out).stem}-{name}.json')
        completed = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                                    '--scenario', name, '--out', str(out_path)],
                                   cwd=str(REPO), capture_output=True, text=True)
        merged[name] = {'returncode': completed.returncode,
                        'stderr': completed.stderr[-2000:] if completed.returncode else ''}
        try:
            merged[name]['result'] = json.loads(out_path.read_text(encoding='utf-8'))
        except ValueError:
            pass
    Path(arguments.out).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(merged, ensure_ascii=False, indent=2))
    return 0 if all(entry['returncode'] == 0 for entry in merged.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
