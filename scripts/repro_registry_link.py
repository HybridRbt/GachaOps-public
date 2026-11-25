"""Reproducible synthetic investigation for GitHub issue #4.

Builds temporary, clearly synthetic data roots and drives the real desktop
entry headlessly (Qt offscreen) to compare how the old seven-registration
configurations connect with the global registry model. Every scenario
asserts its core invariants directly and exits non-zero on any violated
expectation; a return code alone is never treated as passing.

R1  legacy schema 1 profile with all seven registrations -> startup migration
R2  issue state: default profile with one registration plus external legacy
    files -> startup migration, then import same/confirm, import
    conflict/cancel/confirm, with exact file-set deltas
R3  explicit --config draft saved through the real save-selection path
R4  migration prompt cancelled -> exact zero-write over the whole inventory
R5  conflicting ok-ww candidates: without a choice the commit is refused,
    with an explicit choice it succeeds; an incomplete-only zzz defers while
    a complete zzz elsewhere is adopted
R6  after migration: switching through every profile composes each one, and
    a reopen in a separate OS process (different PID) finds identical state

Set REPRO_NEGATIVE to inject a regression that must FAIL loudly: '1'/'count'
runs R1 against a deliberately wrong expected registry count, 'compose'
blocks every composition, 'timeout' resets every imported run timeout,
'timeout2' resets only the conflict source's distinctive 3000s row, and
'inventory' deletes a profile file and plants a fake backup receipt.
Interactive surfaces are answered through one state-driven patch per
surface (no nested shadowing);
QProcess.start raises immediately and records any launch attempt. Results
are masked recursively before anything is written: temporary and sample
path prefixes become <tmp>/<sample> in every textual form and letter case,
a case-insensitive leak check refuses to publish any surviving prefix, and
a failure of the masking, leak-check or serialization exit itself publishes
only fixed text. The parent masks stderr with the system temp prefix and
decodes child output as UTF-8 regardless of the ANSI code page. Real paths
may appear only in private local logs.

Usage: python scripts/repro_registry_link.py --all --out issue4-results.json
       python scripts/repro_registry_link.py --scenario R1 --out R1.json
"""
import argparse
from contextlib import nullcontext
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

ADAPTERS = ('ok-ww', 'bettergi', 'ok-nte', 'maa-end', 'march7', 'maa-gf2', 'zzz-onedragon')
UUID_A = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
UUID_B = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
# Distinct, non-default run values so preservation checks pin real behavior.
TIMEOUTS = (1800, 2400, 3600, 1500, 2700, 3300, 2100)
MANUAL_ADAPTERS = ('ok-ww', 'maa-end')


class ScenarioFailure(AssertionError):
    """A scenario invariant did not hold."""


def check(condition, message):
    if not condition:
        raise ScenarioFailure(message)


def legacy_rows(base, plan):
    """Rows for one legacy configuration. `plan` maps adapter -> row options:
    registered (bool), enabled (bool), timeout (int), game (bool, zzz only),
    game_start (str). Defaults: registered, disabled, TIMEOUTS[index], no
    game EXE, 'assistant'."""
    rows = []
    for index, adapter in enumerate(ADAPTERS):
        options = plan.get(adapter, {})
        row = {'id': f'assistant-{index}', 'adapter': adapter,
               'enabled': options.get('enabled', False),
               'game_start': options.get('game_start', 'assistant'),
               'timeout_seconds': options.get('timeout', TIMEOUTS[index])}
        if options.get('registered', True):
            row['installation_root'] = str(base / adapter)
        if adapter == 'zzz-onedragon' and options.get('game'):
            row['game_path'] = str(base / adapter / 'ZenlessZoneZero.exe')
        rows.append(row)
    return rows


def write_legacy_config(path, base, plan, repeat_limit, reverse=False):
    rows = legacy_rows(base, plan)
    document = {'schema_version': 1, 'repeat_limit': repeat_limit,
                'assistants': list(reversed(rows)) if reverse else rows}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    return document


def write_legacy_profile(root, identity, name, base, plan, repeat_limit, reverse=False):
    directory = root / 'config/profiles' / identity
    directory.mkdir(parents=True)
    rows = legacy_rows(base, plan)
    document = {'schema_version': 1, 'repeat_limit': repeat_limit,
                'assistants': list(reversed(rows)) if reverse else rows}
    (directory / 'config.json').write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    (directory / 'name.json').write_text(json.dumps(
        {'name': name, 'created': '2026-01-01T00:00:00+00:00'}), encoding='utf-8')
    return document


def seal(out, temp, sample_bases):
    """Register the prefixes that must never appear in published output."""
    out['_private'] = {'tmp': str(Path(temp)), 'samples': [str(base) for base in sample_bases]}


def path_variants(needle):
    """Every textual form a Windows path can take in output, including the
    doubled-backslash form that list repr() produces."""
    backslash = chr(92)
    forward = needle.replace(backslash, '/')
    doubled = needle.replace(backslash, backslash * 2)
    return [needle, forward, doubled, doubled.replace('/', backslash * 2)]


def run_projection(source_rows):
    """The run fields one legacy row must contribute, projected once here."""
    return [{'id': row['id'], 'adapter': row['adapter'],
             'enabled': row.get('enabled', True),
             'game_start': row.get('game_start', 'assistant'),
             'timeout_seconds': row.get('timeout_seconds', 1800)}
            for row in source_rows]


def assert_rows_match_source(rows, source_rows, label):
    """Single owner of every per-row run-field comparison."""
    projection = run_projection(source_rows)
    check([row['id'] for row in rows] == [row['id'] for row in projection],
          f'{label}: row order or ids changed')
    for row, expected in zip(rows, projection):
        for field in ('adapter', 'enabled', 'game_start', 'timeout_seconds'):
            check(row[field] == expected[field],
                  f'{label}/{row["id"]}: {field} {row[field]!r} != {expected[field]!r}')
        for field in ('installation_root', 'game_path', 'entry_executable'):
            check(field not in row, f'{label}/{row["id"]}: registration path {field} leaked into run rows')


def composed_rows(document, registry_entries, label, expect_blocked=False):
    """What a run would actually use. A blocked composition is only evidence
    where the scenario expects it; anywhere else it fails the scenario."""
    from gachaops.configuration import compose_configuration
    registry = {'schema_version': 1, 'kind': 'assistant_registry',
                'revision': 1, 'assistants': registry_entries}
    try:
        composed = compose_configuration(document, registry)
    except Exception as error:
        check(expect_blocked, f'{label}: composition unexpectedly blocked: {error}')
        return {'blocked': str(error)}
    check(not expect_blocked, f'{label}: composition was expected to be blocked but succeeded')
    return {row['adapter']: {'enabled': row.get('enabled', True),
                             'installation_root': row.get('installation_root'),
                             'game_path': row.get('game_path')}
            for row in composed['assistants']}


def assert_composed(composed, registry_entries, source_rows, label):
    """The composition must succeed and use exactly the registry's paths."""
    check(isinstance(composed, dict) and 'blocked' not in composed,
          f'{label}: compose did not succeed: {composed!r}')
    for row in run_projection(source_rows):
        adapter = row['adapter']
        entry = composed.get(adapter)
        check(isinstance(entry, dict), f'{label}/{adapter}: missing from composition')
        expected = registry_entries.get(adapter, {})
        check(entry['installation_root'] == expected.get('installation_root'),
              f'{label}/{adapter}: composed path != registry path')
        if expected.get('game_path'):
            check(entry['game_path'] == expected['game_path'],
                  f'{label}/{adapter}: composed game path != registry game path')
        check(entry['enabled'] == row['enabled'], f'{label}/{adapter}: composed enabled != source')


def compare_inventory(before, after, phase):
    """Invariant C, deletions first: nothing existing may disappear; the
    caller's declared allowance governs new files and modifications."""
    removed = set(before) - set(after)
    check(not removed, f'{phase}: files disappeared: {sorted(removed)}')
    new = set(after) - set(before)
    changed = {name for name in set(before) & set(after) if before[name] != after[name]}
    return new, changed


def assert_import_delta(root, before, after, phase):
    """Only one new profile directory (config.json+name.json under one NEW
    uuid) and one transaction backup receipt (manifest.json+complete.json
    under one new backup uuid) may appear; only the registry may change; no
    dangling journal may survive."""
    new, changed = compare_inventory(before, after, phase)
    check(changed == {'config/assistant-registry.json'},
          f'{phase}: unexpected modified files: {sorted(changed)}')
    profile_new = {name for name in new if name.startswith('config/profiles/')}
    backup_new = {name for name in new if name.startswith('backups/registration-migrations/')}
    check(new == profile_new | backup_new and profile_new and backup_new,
          f'{phase}: unexpected new files: {sorted(new)}')
    uuids = {name.split('/')[2] for name in profile_new}
    check(len(uuids) == 1 and {name.rsplit('/', 1)[1] for name in profile_new}
          == {'config.json', 'name.json'},
          f'{phase}: new profile files must be one uuid with config.json+name.json: {sorted(profile_new)}')
    existing_uuids = {name.split('/')[2] for name in before if name.startswith('config/profiles/')}
    check(not uuids & existing_uuids, f'{phase}: import reused an existing profile uuid')
    backup_uuids = {name.split('/')[2] for name in backup_new}
    check(len(backup_uuids) == 1 and not (backup_uuids & existing_uuids),
          f'{phase}: backup receipts span multiple or existing transactions: {sorted(backup_new)}')
    receipt_names = {name.rsplit('/', 1)[1] for name in backup_new}
    check({'manifest.json', 'complete.json'} <= receipt_names,
          f'{phase}: transaction receipt incomplete: {sorted(receipt_names)}')
    check(not (root / 'config' / 'registration-migration.json').exists(),
          f'{phase}: a dangling migration journal survived')
    return new


def registry_snapshot(root):
    path = root / 'config/assistant-registry.json'
    if not path.exists():
        return {'exists': False, 'revision': None, 'count': 0, 'adapters': [], 'entries': {}}
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    return {'exists': True, 'revision': document['revision'],
            'count': len(document['assistants']), 'adapters': sorted(document['assistants']),
            'entries': document['assistants']}


def profile_snapshot(window, root, label, blocked_compose=()):
    """Snapshot every combination; a blocked composition is recorded only for
    the combinations named in `blocked_compose` and fails the scenario
    anywhere else."""
    from gachaops.profiles import run_content
    entries = registry_snapshot(root)['entries']
    rows = []
    for profile in window.profile_store.list_profiles():
        document = window.profile_store.load_profile(profile['id'])
        rows.append({'name': profile['name'], 'uuid': profile['id'],
                     'revision': document['revision'],
                     'repeat_limit': document['repeat_limit'],
                     'registered_rows': sum(1 for r in document['assistants']
                                            if r.get('installation_root') or r.get('game_path')),
                     'rows': [{'id': r['id'], 'adapter': r['adapter'], 'order': index,
                               'enabled': r.get('enabled', True),
                               'game_start': r.get('game_start', 'assistant'),
                               'timeout_seconds': r.get('timeout_seconds')}
                              for index, r in enumerate(document['assistants'])],
                     'composed': composed_rows(run_content(document), entries,
                                               f"{label}/{profile['name']}",
                                               expect_blocked=profile['name'] in blocked_compose)})
    return rows


def window_snapshot(window, label, blocked_compose=()):
    return {'root_label': label,
            'selected': {'uuid': window.profile_ids.get(window.profiles.currentData()),
                         'name': window.profile_names.get(window.profiles.currentData())},
            'draft': {'active': window._draft_active(), 'source': window.draft_source,
                      'has_data': window.draft_data is not None,
                      'data': window.draft_data if window._draft_active() else None},
            'registry': registry_snapshot(window.workspace),
            'profiles': profile_snapshot(window, window.workspace, label, blocked_compose)}


def preview_snapshot(root):
    """Candidate fields and sources exactly as the migration dialog sees them."""
    from gachaops.registration_migration import preview_registration_migration
    preview = preview_registration_migration(root)
    return [{'adapter': entry['adapter'], 'conflict': entry['conflict'],
             'candidates': entry['candidates']} for entry in preview['adapters']]


def file_inventory(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob('*')) if path.is_file()}


def store_rows(root):
    from gachaops.profiles import ProfileStore
    return len(ProfileStore(root).list_profiles())


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


def drive(root, exercise, argv=(), state=None):
    """Run the real desktop entry headlessly.

    All interactive surfaces are answered through `state`; there is exactly
    one patch per surface and scenarios never re-patch over it:
      state['question']       'Yes'/'No' answer for QMessageBox.question
      state['dialog_result']  'Accept'/'Reject' fallback for QDialog.exec
      state['dialog_handler'] callable(dialog) -> DialogCode, overriding the
                              fallback (combination boxes are the handler's job)
      state['open_file_path'] file returned by QFileDialog.getOpenFileName
    drive records every interaction in state['dialog_log'] and makes any
    process launch attempt raise immediately.
    """
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from unittest.mock import patch
    from PySide6.QtCore import QProcess, QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
    from gachaops import desktop
    state = state if state is not None else {}
    launches = []
    messages = []
    state.setdefault('dialog_log', [])
    answers = {'Yes': QMessageBox.StandardButton.Yes, 'No': QMessageBox.StandardButton.No}
    codes = {'Accept': QDialog.DialogCode.Accepted, 'Reject': QDialog.DialogCode.Rejected}

    def record_launch(*args, **kwargs):
        launches.append([str(argument) for argument in args])
        raise AssertionError('unexpected process launch in an offline scenario')

    def answer_question(*args, **kwargs):
        messages.append({'kind': 'question', 'title': args[1] if len(args) > 1 else '',
                         'text': args[2] if len(args) > 2 else ''})
        return answers[state.get('question', 'Yes')]

    def record_message(kind):
        def handler(*args, **kwargs):
            messages.append({'kind': kind, 'title': args[1] if len(args) > 1 else '',
                             'text': args[2] if len(args) > 2 else ''})
            return None
        return handler

    def default_dialog(dialog_instance):
        from PySide6.QtWidgets import QComboBox
        for box in dialog_instance.findChildren(QComboBox):
            if box.currentData() is None and box.count() > 1:
                box.setCurrentIndex(1)
        return codes[state.get('dialog_result', 'Accept')]

    def answer_dialog(dialog_instance):
        state['dialog_log'].append(type(dialog_instance).__name__)
        handler = state.get('dialog_handler')
        if handler is not None:
            return handler(dialog_instance)
        return default_dialog(dialog_instance)

    def pick_file(*args, **kwargs):
        return (state['open_file_path'], '') if 'open_file_path' in state else ('', '')

    def wrapped(window):
        exercise(window)
        # Closing the window synchronously (before app.exec starts) consumes
        # quitOnLastWindowClosed too early; schedule a real quit inside the
        # loop. 200 ms stays below the 900 ms auto-recheck so nothing launches.
        live = QApplication.instance()
        if live is not None:
            QTimer.singleShot(200, live.quit)

    if os.environ.get('REPRO_DEBUG'):
        import faulthandler
        faulthandler.dump_traceback_later(60, exit=True)
    with patch.object(QProcess, 'start', side_effect=record_launch), \
         patch.object(QMessageBox, 'question', answer_question), \
         patch.object(QMessageBox, 'critical', record_message('critical')), \
         patch.object(QMessageBox, 'warning', record_message('warning')), \
         patch.object(QMessageBox, 'information', record_message('information')), \
         patch.object(QDialog, 'exec', answer_dialog), \
         patch.object(QFileDialog, 'getOpenFileName', pick_file):
        with patch.object(sys, 'argv',
                          ['desktop'] + list(argv) + ['--data-root', str(root)]):
            with patch.object(desktop, 'PREVIEW_FACTORY', wrapped, create=True):
                try:
                    code = desktop.main()
                except SystemExit as exit_code:  # the gate's decline path
                    code = exit_code.code
    release_qt()
    return {'exit_code': code, 'launches': launches, 'dialogs': messages}


def select_combo(dialog, data):
    """Select the entry with `data` in every combination box that has it."""
    from PySide6.QtWidgets import QComboBox
    chosen = []
    for box in dialog.findChildren(QComboBox):
        index = next((i for i in range(box.count()) if box.itemData(i) == data), None)
        if index is not None and box.currentIndex() != index:
            box.setCurrentIndex(index)
            chosen.append(box.itemText(index))
    return chosen


REOPEN_RUNNER = '''\
"""Reopen the data root in a separate OS process and record what is shown."""
import json
import os
from pathlib import Path
import sys

repo, root, out_path = (Path(arg) for arg in sys.argv[1:4])
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / 'scripts'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from repro_registry_link import drive, window_snapshot

recorded = {}

def exercise(window):
    window.timer.stop(); window.recheck_timer.stop()
    recorded['pid'] = os.getpid()
    recorded['snapshot'] = window_snapshot(window, 'R6-reopen')
    window.close()

result = drive(root, exercise)
recorded.update(result)
out_path.write_text(json.dumps(recorded, ensure_ascii=False), encoding='utf-8')
'''


SCENARIOS = {}


def scenario(name):
    def register(function):
        SCENARIOS[name] = function
        return function
    return register


@scenario('R1')
def run_r1(out, state):
    """Old profile with all seven registrations: startup migration adopts
    every entry with the exact input paths and preserves run fields."""
    plan = {'ok-ww': {'enabled': True, 'game_start': 'manual', 'timeout': 2400},
            'maa-end': {'enabled': True, 'timeout': 2700},
            'zzz-onedragon': {'game': True, 'timeout': 2000}}

    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'
        root.mkdir()
        seal(out, temp, [base])
        source = write_legacy_profile(root, UUID_A, '旧七登记组合', base, plan, repeat_limit=3)
        out['before'] = {'registry': registry_snapshot(root)}

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            snapshot = window_snapshot(window, 'R1/data-root')
            check(snapshot['selected'] == {'uuid': UUID_A, 'name': '旧七登记组合'},
                  f"selected changed: {snapshot['selected']}")
            registry = snapshot['registry']
            expected_count = 6 if os.environ.get('REPRO_NEGATIVE') in ('1', 'count') else 7
            check(registry['count'] == expected_count,
                  f"registry count {registry['count']} != {expected_count}: {registry['adapters']}")
            check(set(registry['adapters']) == set(ADAPTERS),
                  f"registry adapters wrong: {registry['adapters']}")
            for row in source['assistants']:
                entry = registry['entries'][row['adapter']]
                check(entry.get('installation_root') == row.get('installation_root')
                      and entry.get('game_path') == row.get('game_path'),
                      f"{row['adapter']} adopted {entry} instead of the input paths")
            profiles = {profile['name']: profile for profile in snapshot['profiles']}
            check(set(profiles) == {'旧七登记组合'}, f'unexpected profiles: {set(profiles)}')
            converted = profiles['旧七登记组合']
            check(converted['uuid'] == UUID_A, 'converted profile uuid changed')
            check(converted['registered_rows'] == 0,
                  'profile still carries registration paths after migration')
            check(converted['repeat_limit'] == 3,
                  f"repeat_limit {converted['repeat_limit']} != 3")
            assert_rows_match_source(converted['rows'], source['assistants'], 'R1/旧七登记组合')
            assert_composed(converted['composed'], registry['entries'],
                            source['assistants'], 'R1/旧七登记组合')
            out['snapshot'] = snapshot
            window.close()

        result = drive(root, exercise, state=state)
        out.update(result)
        check(result['exit_code'] == 0, f"desktop exit {result['exit_code']} != 0")
        check(not state['dialog_log'] or 'QMessageBox' not in str(state['dialog_log']),
              f"unexpected message dialogs: {state['dialog_log']}")
        out['after'] = {'registry': registry_snapshot(root)}
        check(out['after']['registry']['count'] == 7, 'final registry count != 7')
        check(not out['launches'], f"process launches recorded: {out['launches']}")


@scenario('R2')
def run_r2(out, state):
    """Issue #4 state: default profile with one registration plus external
    legacy files. Startup migrates one entry. Import one: same ok-ww path
    plus six new rows (counts asserted). Import two: a genuinely different
    ok-ww path conflicts; cancelling writes nothing over the whole inventory;
    confirming with keep keeps the current path authoritative. The external
    sources and the existing profile never change."""
    from gachaops.registration_migration import (commit_registration_import,
                                                 preview_registration_import)
    from gachaops.planner import ConfigError
    from PySide6.QtWidgets import QDialog, QMessageBox

    with tempfile.TemporaryDirectory() as temp:
        base_default = Path(temp) / 'default-install'
        base_default.mkdir()
        base_file1 = Path(temp) / 'file1-install'
        base_file1.mkdir()
        base_file2 = Path(temp) / 'file2-install'
        base_file2.mkdir()
        root = Path(temp) / 'data-root'
        root.mkdir()
        seal(out, temp, [base_default, base_file1, base_file2])
        startup_source = write_legacy_profile(root, UUID_A, '默认组合', base_default,
                                              {'ok-ww': {'timeout': 1800},
                                               'bettergi': {'registered': False},
                                               'ok-nte': {'registered': False},
                                               'maa-end': {'registered': False},
                                               'march7': {'registered': False},
                                               'maa-gf2': {'registered': False},
                                               'zzz-onedragon': {'registered': False}},
                                              repeat_limit=1)
        path_a = str(base_default / 'ok-ww')
        file1 = Path(temp) / '旧七登记配置.json'
        source1 = write_legacy_config(file1, base_file1,
                                      {'ok-ww': {'timeout': 2400},
                                       'zzz-onedragon': {'game': True, 'timeout': 2100}},
                                      repeat_limit=2)
        # file1's ok-ww carries the SAME installation path as the default
        # profile's registration, so the preview must classify it as 'same'.
        document1 = json.loads(file1.read_text(encoding='utf-8'))
        next(row for row in document1['assistants']
             if row['adapter'] == 'ok-ww')['installation_root'] = path_a
        file1.write_text(json.dumps(document1, ensure_ascii=False, indent=2), encoding='utf-8')
        source1 = document1
        file1_bytes = file1.read_bytes()
        file2 = Path(temp) / '冲突接入配置.json'
        source2 = write_legacy_config(
            file2, base_file2,
            {'bettergi': {'registered': False}, 'ok-nte': {'registered': False},
             'maa-end': {'registered': False}, 'march7': {'registered': False},
             'maa-gf2': {'registered': False}, 'zzz-onedragon': {'registered': False},
             'ok-ww': {'enabled': True, 'timeout': 3000}}, repeat_limit=0)
        path_c = str(base_file2 / 'ok-ww')
        check(len({path_a, str(base_file1 / 'ok-ww'), path_c}) == 3,
              'fixture failed to build three distinct ok-ww paths')

        mode = {'phase': 'idle'}
        events = []

        def on_dialog(dialog_instance):
            # Structured evidence only: raw strings here would go through the
            # single masking exit, never through f-string repr().
            entry = {'phase': mode['phase'], 'dialog': type(dialog_instance).__name__}
            if mode['phase'] in {'cancel-conflict', 'confirm-keep'}:
                chosen = select_combo(dialog_instance, 'keep' if mode['phase'] == 'confirm-keep'
                                      else None)
                if chosen:
                    entry['chosen'] = chosen
            events.append(entry)
            return QDialog.DialogCode.Accepted

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            if os.environ.get('REPRO_NEGATIVE') == 'timeout':
                # Injected regression: the import silently resets every run
                # timeout. The row-by-row assertion must fail the scenario.
                from gachaops import registration_migration
                real_split = registration_migration._split_schema1

                def reset_timeouts(config):
                    profile_part, candidates = real_split(config)
                    for row in profile_part['assistants']:
                        row['timeout_seconds'] = 1800
                    return profile_part, candidates
                registration_migration._split_schema1 = reset_timeouts
            if os.environ.get('REPRO_NEGATIVE') == 'timeout2':
                # Injected regression aimed ONLY at the conflict source: its
                # distinctive 3000s row exists in no other document, so only
                # the source2 line comparison can catch it - an earlier
                # assertion must not fail first.
                from gachaops import registration_migration
                real_split = registration_migration._split_schema1

                def reset_conflict_timeouts(config):
                    profile_part, candidates = real_split(config)
                    for row in profile_part['assistants']:
                        if row.get('timeout_seconds') == 3000:
                            row['timeout_seconds'] = 1800
                    return profile_part, candidates
                registration_migration._split_schema1 = reset_conflict_timeouts
            startup = window_snapshot(window, 'R2/data-root')
            check(startup['selected'] == {'uuid': UUID_A, 'name': '默认组合'},
                  f"selected {startup['selected']} != 默认组合")
            check(startup['registry']['count'] == 1
                  and startup['registry']['entries'].get('ok-ww') == {'installation_root': path_a},
                  f"startup registry {startup['registry']}")
            default_profile = next(profile for profile in startup['profiles']
                                   if profile['name'] == '默认组合')
            check(default_profile['repeat_limit'] == 1, 'default repeat_limit not preserved')
            assert_rows_match_source(default_profile['rows'],
                                     startup_source['assistants'], 'R2/默认组合')
            out['after_startup'] = startup

            # Import one: same ok-ww path, six new adapters.
            preview = preview_registration_import(root, file1, Path(file1).stem)
            actions1 = {entry['adapter']: entry['action'] for entry in preview['adapters']}
            check(actions1.get('ok-ww') == 'same', f"file1 ok-ww action {actions1.get('ok-ww')} != same")
            check(sum(1 for action in actions1.values() if action == 'new') == 6,
                  f"file1 new-count {actions1} != 6 new")
            before_import1 = file_inventory(root)
            mode['phase'] = 'import-same'
            state['open_file_path'] = str(file1)
            state['question'] = 'Yes'
            state['dialog_handler'] = on_dialog
            window.browse()
            if os.environ.get('REPRO_NEGATIVE') == 'inventory':
                # Injected anomaly: an old profile file disappears and a fake
                # backup file appears. The inventory comparison must refuse.
                (root / 'config/profiles' / UUID_A / 'name.json').unlink()
                fake = root / 'backups/registration-migrations' / 'deadbeef' / 'anything.txt'
                fake.parent.mkdir(parents=True, exist_ok=True)
                fake.write_text('fake', encoding='ascii')
            confirmed = window_snapshot(window, 'R2/data-root')
            registry = confirmed['registry']
            check(registry['count'] == 7, f"after import-one count {registry['count']} != 7")
            check(registry['entries']['ok-ww'] == {'installation_root': path_a},
                  f"same path changed: {registry['entries'].get('ok-ww')}")
            for adapter in ('bettergi', 'ok-nte', 'maa-end', 'march7', 'maa-gf2'):
                check(registry['entries'][adapter] == {'installation_root': str(base_file1 / adapter)},
                      f"{adapter} not adopted from file1")
            check(registry['entries']['zzz-onedragon'].get('installation_root') == str(base_file1 / 'zzz-onedragon')
                  and registry['entries']['zzz-onedragon'].get('game_path') == str(base_file1 / 'zzz-onedragon' / 'ZenlessZoneZero.exe'),
                  f"zzz not adopted completely: {registry['entries'].get('zzz-onedragon')}")
            check(confirmed['selected']['name'] == '旧七登记配置',
                  f"selected {confirmed['selected']['name']} != 旧七登记配置")
            imported = next(profile for profile in confirmed['profiles']
                            if profile['name'] == '旧七登记配置')
            check(imported['repeat_limit'] == 2, 'imported repeat_limit != 2')
            assert_rows_match_source(imported['rows'], source1['assistants'],
                                     'R2/旧七登记配置')
            assert_composed(imported['composed'], registry['entries'],
                            source1['assistants'], 'R2/旧七登记配置')
            assert_import_delta(root, before_import1, file_inventory(root), 'import-one')
            check(file1.read_bytes() == file1_bytes,
                  'import-one changed the external source file')
            out['after_import_same'] = confirmed
            out['after_import_same']['events'] = list(events)
            events.clear()

            # Import two: genuinely different ok-ww path -> conflict.
            preview2 = preview_registration_import(root, file2, Path(file2).stem)
            actions2 = {entry['adapter']: entry['action'] for entry in preview2['adapters']}
            check(actions2 == {'ok-ww': 'conflict'}, f"file2 preview actions {actions2} != 1 conflict")
            out['preview_actions_conflict'] = actions2
            # Without a choice the commit is refused outright.
            try:
                commit_registration_import(root, preview2, {})
                raise ScenarioFailure('conflict commit without a choice unexpectedly succeeded')
            except ConfigError as error:
                check('选择' in str(error), f"unexpected no-choice error: {error}")
                out['no_choice_refused'] = str(error)

            # 取消: the whole inventory plus the external source stay identical.
            inventory_before_cancel = file_inventory(root)
            source2_before = file2.read_bytes()
            mode['phase'] = 'cancel-conflict'
            state['open_file_path'] = str(file2)
            state['question'] = 'No'
            window.browse()
            state['question'] = 'Yes'
            check(file_inventory(root) == inventory_before_cancel,
                  'cancelled import changed the data root inventory')
            check(file2.read_bytes() == source2_before, 'cancelled import changed the source file')
            out['after_cancel'] = {'inventory_zero_delta': True, 'events': list(events)}
            events.clear()

            # 确认（保留当前值）: registry keeps pathA; source and 默认组合 unchanged.
            inventory_before_keep = file_inventory(root)
            mode['phase'] = 'confirm-keep'
            state['open_file_path'] = str(file2)
            window.browse()
            mode['phase'] = 'idle'
            state.pop('open_file_path')
            confirmed2 = window_snapshot(window, 'R2/data-root')
            check(confirmed2['registry']['entries']['ok-ww'] == {'installation_root': path_a},
                  f"keep lost the current path: {confirmed2['registry']['entries'].get('ok-ww')}")
            check(file2.read_bytes() == source2_before, 'confirmed import changed the source file')
            default_after = next(profile for profile in confirmed2['profiles']
                                 if profile['name'] == '默认组合')
            check(default_after['repeat_limit'] == 1, 'default profile changed by import')
            assert_rows_match_source(default_after['rows'],
                                     startup_source['assistants'], 'R2/默认组合-after')
            assert_import_delta(root, inventory_before_keep, file_inventory(root), 'keep')
            # The conflict import creates its own combination; its stored run
            # fields must still line up with the external source document.
            conflict_import = next(profile for profile in confirmed2['profiles']
                                   if profile['name'] == '冲突接入配置')
            check(conflict_import['repeat_limit'] == 0, 'conflict import repeat_limit != 0')
            assert_rows_match_source(conflict_import['rows'], source2['assistants'],
                                     'R2/冲突接入配置')
            out['after_confirm_keep'] = confirmed2
            out['after_confirm_keep']['events'] = list(events)
            window.close()

        result = drive(root, exercise, state=state)
        out.update(result)
        check(result['exit_code'] == 0, f"desktop exit {result['exit_code']} != 0")
        out['registry_after_all'] = registry_snapshot(root)
        check(not out['launches'], f"process launches recorded: {out['launches']}")


@scenario('R3')
def run_r3(out, state):
    """Explicit --config draft saved through the real save-selection path:
    run fields only, order/enabled/game_start/timeouts/repeat preserved,
    registry untouched, paths never carried."""
    from gachaops.profiles import ProfileStore
    from gachaops.registry import RegistrationStore

    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'
        root.mkdir()
        seal(out, temp, [base])
        RegistrationStore(root).create()
        ProfileStore(root).create_profile('默认组合')
        legacy = Path(temp) / '旧七登记配置.json'
        plan = {'ok-ww': {'enabled': True, 'game_start': 'manual', 'timeout': 2400},
                'maa-end': {'enabled': True, 'timeout': 2700}}
        source = write_legacy_config(legacy, base, plan, repeat_limit=3, reverse=True)
        before = registry_snapshot(root)

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            check(window._draft_active(), '--config draft is not active')
            draft = window.draft_data
            assert_rows_match_source(draft['assistants'], source['assistants'], 'R3/draft')
            check(draft['repeat_limit'] == 3, f"draft repeat_limit {draft['repeat_limit']} != 3")
            # The freshly created 默认组合 is all-disabled, so its composition
            # succeeds trivially; the enabled legacy DRAFT is what the empty
            # registry must refuse.
            out['draft'] = window_snapshot(window, 'R3/data-root')['draft']
            draft_blocked = composed_rows(window.draft_data, {}, 'R3/legacy-draft',
                                          expect_blocked=True)
            check('尚未登记接入' in draft_blocked.get('blocked', ''),
                  f'draft compose blocked for the wrong reason: {draft_blocked!r}')
            window.save_selection()
            snapshot = window_snapshot(window, 'R3/data-root', blocked_compose={'我的组合'})
            saved = next(profile for profile in snapshot['profiles']
                         if profile['name'] == '我的组合')
            check(saved['registered_rows'] == 0, 'saved profile carries registration paths')
            check(saved['repeat_limit'] == 3, f"saved repeat_limit {saved['repeat_limit']} != 3")
            assert_rows_match_source(saved['rows'], source['assistants'], 'R3/我的组合')
            # Saved rows are enabled but the registry is empty: composing this
            # combination for a run is expected to be blocked with the
            # not-registered refusal, and only here.
            blocked = saved['composed']
            check(isinstance(blocked, dict) and 'blocked' in blocked
                  and '尚未登记接入' in blocked['blocked'],
                  f"saved profile compose not blocked as expected: {blocked!r}")
            default_profile = next(profile for profile in snapshot['profiles']
                                   if profile['name'] == '默认组合')
            assert_composed(default_profile['composed'], {}, [], 'R3/默认组合')
            check(snapshot['selected'] == {'uuid': saved['uuid'], 'name': '我的组合'},
                  f"selected {snapshot['selected']} after saving draft")
            check(snapshot['draft']['active'] is False, 'draft still active after save')
            out['snapshot'] = snapshot
            window.close()

        result = drive(root, exercise, argv=['--config', str(legacy)], state=state)
        out.update(result)
        check(result['exit_code'] == 0, f"desktop exit {result['exit_code']} != 0")
        after = registry_snapshot(root)
        check(after == before, f"registry changed by draft save: {before} -> {after}")
        check(store_rows(root) == 2, f"profile count {store_rows(root)} != 2")
        check(not out['launches'], f"process launches recorded: {out['launches']}")


@scenario('R4')
def run_r4(out, state):
    """Declining the migration prompt: program exits 0 and the whole file
    inventory (names and bytes) is identical afterwards."""
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'
        root.mkdir()
        seal(out, temp, [base])
        write_legacy_profile(root, UUID_A, '旧组合', base,
                             {'ok-ww': {'enabled': True, 'timeout': 2400},
                              'zzz-onedragon': {'game': True}}, repeat_limit=3)
        inventory = file_inventory(root)
        check(len(inventory) >= 2, f"fixture inventory too small: {len(inventory)}")
        dialog_log = state.setdefault('dialog_log', [])

        def exercise(window):
            raise ScenarioFailure('a declined startup must not build a window')

        state['dialog_result'] = 'Reject'
        result = drive(root, exercise, state=state)
        out.update(result)
        after = file_inventory(root)
        check(after == inventory, 'file inventory (names/bytes) changed after decline')
        check(result['exit_code'] == 0, f"decline exit {result['exit_code']} != 0")
        check(dialog_log == ['QDialog'], f"unexpected dialogs at decline: {dialog_log}")
        out['zero_write'] = True
        out['files_tracked'] = len(inventory)
        check(not out['launches'], f"process launches recorded: {out['launches']}")


@scenario('R5')
def run_r5(out, state):
    """Two profiles disagree on ok-ww: without a choice the migration commit
    is refused, and the dialog path needs an explicit selection. A schema-1
    legal disabled zzz row with a root but no game EXE defers when no
    complete candidate exists anywhere, and run fields survive."""
    from gachaops.registration_migration import (commit_registration_migration,
                                                 preview_registration_migration)
    from gachaops.planner import ConfigError
    from PySide6.QtWidgets import QDialog

    with tempfile.TemporaryDirectory() as temp:
        base_a = Path(temp) / 'install-a'
        base_a.mkdir()
        base_b = Path(temp) / 'install-b'
        base_b.mkdir()
        root = Path(temp) / 'data-root'
        root.mkdir()
        seal(out, temp, [base_a, base_b])
        plan_a = {'ok-ww': {'enabled': True, 'game_start': 'manual', 'timeout': 2400},
                  'zzz-onedragon': {'timeout': 2000}}  # registered, disabled, NO game EXE
        source_a = write_legacy_profile(root, UUID_A, '组合甲', base_a, plan_a, repeat_limit=2)
        source_b = write_legacy_profile(root, UUID_B, '组合乙', base_b,
                                        {'ok-ww': {'timeout': 3000}}, repeat_limit=0)
        # 乙's ok-ww carries a genuinely different root than 甲's.
        document_b = json.loads((root / 'config/profiles' / UUID_B / 'config.json').read_text(encoding='utf-8'))
        document_b['assistants'][0]['installation_root'] = str(base_b / 'ok-ww-other')
        (root / 'config/profiles' / UUID_B / 'config.json').write_text(
            json.dumps(document_b, ensure_ascii=False), encoding='utf-8')

        # Candidate census and the no-choice refusal, at the API level.
        preview = preview_registration_migration(root)
        adapters = {entry['adapter']: entry for entry in preview['adapters']}
        check('zzz-onedragon' not in adapters,
              'incomplete-only zzz appeared as an adoptable candidate')
        check(adapters['ok-ww']['conflict'] is True, 'ok-ww candidates did not conflict')
        candidate_paths = [candidate['fields']['installation_root']
                           for candidate in adapters['ok-ww']['candidates']]
        check(candidate_paths == [str(base_a / 'ok-ww'), str(base_b / 'ok-ww-other')],
              f"candidate order/paths unexpected: {candidate_paths}")
        try:
            commit_registration_migration(root, preview, {})
            raise ScenarioFailure('migration without choices unexpectedly succeeded')
        except ConfigError as error:
            check('选择' in str(error), f"unexpected no-choice error: {error}")
            out['no_choice_refused'] = str(error)
        out['preview'] = preview_snapshot(root)
        events = []

        def on_dialog(dialog_instance):
            chosen = select_combo(dialog_instance, 0)  # explicit candidate 1
            if chosen:
                # Structured evidence: never repr() a list of path-bearing
                # strings into one f-string; masking handles each element.
                events.append({'phase': 'conflict-choice', 'chosen': chosen})
            return QDialog.DialogCode.Accepted

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            snapshot = window_snapshot(window, 'R5/data-root')
            registry = snapshot['registry']
            check(registry['count'] == 6,
                  f"registry count {registry['count']} != 6 (zzz must defer): {registry['adapters']}")
            check('zzz-onedragon' not in registry['entries'],
                  f"incomplete zzz was adopted: {registry['entries'].get('zzz-onedragon')}")
            check(registry['entries']['ok-ww'] == {'installation_root': str(base_a / 'ok-ww')},
                  f"ok-ww is not candidate 1: {registry['entries'].get('ok-ww')}")
            for name, source in (('组合甲', source_a), ('组合乙', source_b)):
                profile = next(p for p in snapshot['profiles'] if p['name'] == name)
                assert_rows_match_source(profile['rows'], source['assistants'], f'R5/{name}')
                assert_composed(profile['composed'], registry['entries'],
                                source['assistants'], f'R5/{name}')
                zzz = next(row for row in profile['rows'] if row['adapter'] == 'zzz-onedragon')
                check('zzz-onedragon' not in registry['entries'],
                      'zzz row exists in the profile but not in the registry (待接入)')
            out['snapshot'] = snapshot
            out['events'] = list(events)
            check(events and events[0].get('chosen'), 'conflict choice was never recorded')
            window.close()

        result = drive(root, exercise, state={'dialog_handler': on_dialog})
        out.update(result)
        check(result['exit_code'] == 0, f"desktop exit {result['exit_code']} != 0")
        out['registry_after'] = registry_snapshot(root)
        check('zzz-onedragon' not in out['registry_after']['entries'],
              'zzz leaked into the registry after the run')
        check(not out['launches'], f"process launches recorded: {out['launches']}")


@scenario('R5b')
def run_r5b(out, state):
    """When a complete zzz candidate exists elsewhere, the complete one is
    adopted even though another profile only has the partial row."""
    with tempfile.TemporaryDirectory() as temp:
        base_a = Path(temp) / 'install-a'
        base_a.mkdir()
        base_b = Path(temp) / 'install-b'
        base_b.mkdir()
        root = Path(temp) / 'data-root'
        root.mkdir()
        seal(out, temp, [base_a, base_b])
        source_a = write_legacy_profile(root, UUID_A, '组合甲', base_a,
                                        {'ok-ww': {'enabled': True, 'timeout': 2400},
                                         'zzz-onedragon': {'timeout': 2000}}, repeat_limit=2)
        source_b = write_legacy_profile(root, UUID_B, '组合乙', base_b,
                                        {'zzz-onedragon': {'game': True, 'timeout': 2100}},
                                        repeat_limit=0)

        def exercise(window):
            window.timer.stop(); window.recheck_timer.stop()
            snapshot = window_snapshot(window, 'R5b/data-root')
            registry = snapshot['registry']
            check(registry['count'] == 7, f"registry count {registry['count']} != 7")
            check(registry['entries'].get('zzz-onedragon')
                  == {'installation_root': str(base_b / 'zzz-onedragon'),
                      'game_path': str(base_b / 'zzz-onedragon' / 'ZenlessZoneZero.exe')},
                  f"complete zzz not adopted: {registry['entries'].get('zzz-onedragon')}")
            check(registry['entries'].get('ok-ww') == {'installation_root': str(base_a / 'ok-ww')},
                  f"ok-ww not adopted: {registry['entries'].get('ok-ww')}")
            for name, source in (('组合甲', source_a), ('组合乙', source_b)):
                profile = next(p for p in snapshot['profiles'] if p['name'] == name)
                assert_rows_match_source(profile['rows'], source['assistants'], f'R5b/{name}')
                assert_composed(profile['composed'], registry['entries'],
                                source['assistants'], f'R5b/{name}')
            out['snapshot'] = snapshot
            window.close()

        result = drive(root, exercise, state=state)
        out.update(result)
        check(result['exit_code'] == 0, f"desktop exit {result['exit_code']} != 0")
        check(not out['launches'], f"process launches recorded: {out['launches']}")


@scenario('R6')
def run_r6(out, state):
    """After migration: switching through every combination composes each
    one, and a reopen in a separate OS process (different PID) finds the
    same registry and combinations. Launch attempts are kept per phase."""
    runner_path = Path(tempfile.gettempdir()) / f'issue4-reopen-{os.getpid()}.py'
    runner_path.write_text(REOPEN_RUNNER, encoding='utf-8')

    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp) / 'old-install'
        base.mkdir()
        root = Path(temp) / 'data-root'
        root.mkdir()
        seal(out, temp, [base])
        source_a = write_legacy_profile(root, UUID_A, '组合甲', base,
                                        {'ok-ww': {'enabled': True, 'game_start': 'manual', 'timeout': 2400},
                                         'zzz-onedragon': {'game': True, 'timeout': 2000}}, repeat_limit=2)
        source_b = write_legacy_profile(root, UUID_B, '组合乙', base, {}, repeat_limit=0)
        expected_registry = None

        def exercise(window):
            nonlocal expected_registry
            window.timer.stop(); window.recheck_timer.stop()
            snapshots = []
            window.create_profile_named('新组合')
            new_uuid = next(uuid for uuid in window.profile_ids.values()
                            if uuid not in {UUID_A, UUID_B})
            switch_plan = (
                (UUID_A, {'name': '组合甲', 'repeat': 2, 'first_timeout': 2400}, source_a['assistants']),
                (UUID_B, {'name': '组合乙', 'repeat': 0, 'first_timeout': 1800}, source_b['assistants']),
                (new_uuid, {'name': '新组合', 'repeat': 0, 'first_timeout': 1800}, []))
            for identity, expected, source_rows in switch_plan:
                path_by_uuid = {value: key for key, value in window.profile_ids.items()}
                index = window.profiles.findData(path_by_uuid[identity])
                check(index >= 0, f"profile {expected['name']} missing from the combo")
                window.profiles.setCurrentIndex(index)
                snapshot = window_snapshot(window, 'R6/data-root')
                check(snapshot['selected'] == {'uuid': identity, 'name': expected['name']},
                      f"switch selected {snapshot['selected']} != {expected['name']}")
                check(window.data is not None
                      and window.data.get('repeat_limit') == expected['repeat'],
                      f"{expected['name']} composed repeat {window.data.get('repeat_limit')} != {expected['repeat']}")
                check(window.data['assistants'][0]['timeout_seconds'] == expected['first_timeout'],
                      f"{expected['name']} first timeout {window.data['assistants'][0]['timeout_seconds']} "
                      f"!= {expected['first_timeout']}")
                # Every switched-to combination must really compose from the
                # shared registry with the expected paths; a blocked compose
                # fails here instead of hiding behind the run fields.
                profile = next(p for p in snapshot['profiles'] if p['name'] == expected['name'])
                assert_composed(profile['composed'], snapshot['registry']['entries'],
                                source_rows, f"R6/{expected['name']}")
                snapshots.append(snapshot)
            expected_registry = snapshots[0]['registry']
            check(all(snapshot['registry'] == expected_registry for snapshot in snapshots),
                  'registry changed while switching profiles')
            out['switch_snapshots'] = snapshots
            window.close()

        first = drive(root, exercise, state=state)
        out['launches_phase1'] = first['launches']
        out['exit_code'] = first['exit_code']
        check(first['exit_code'] == 0, f"desktop exit {first['exit_code']} != 0")
        registry_before_reopen = registry_snapshot(root)
        check(registry_before_reopen == expected_registry,
              f'registry drifted after phase 1: disk={registry_before_reopen} window={expected_registry}')

        # Reopen in a genuinely separate OS process sharing the same root.
        reopen_out = Path(temp) / 'reopen.json'
        try:
            completed = subprocess.run(
                [sys.executable, '-X', 'utf8', str(runner_path), str(REPO), str(root), str(reopen_out)],
                cwd=str(REPO), capture_output=True, text=True, timeout=240,
                encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            # TimeoutExpired embeds the whole command line; interpreter,
            # repository and runner paths all sit outside the scenario
            # masks, so only fixed text may describe this failure.
            check(False, 'reopen process timed out after 240s')
        check(completed.returncode == 0, f"reopen process failed: {completed.stderr[-500:]}")
        recorded = json.loads(reopen_out.read_text(encoding='utf-8'))
        check(recorded['pid'] != os.getpid(), f"reopen ran in the parent PID {recorded['pid']}")
        snapshot = recorded['snapshot']
        check(snapshot['registry'] == registry_before_reopen, 'reopen registry differs')
        check(snapshot['selected'] == {'uuid': UUID_A, 'name': '组合甲'},
              f"reopen selected {snapshot['selected']} != 组合甲")
        # The full combination set and every registration path must survive.
        reopened_names = {profile['name'] for profile in snapshot['profiles']}
        check(reopened_names == {'组合甲', '组合乙', '新组合'},
              f"reopen combination set {reopened_names}")
        check({profile['uuid'] for profile in snapshot['profiles']}
              == {UUID_A, UUID_B} | {profile['uuid'] for profile in out['switch_snapshots'][2]['profiles']},
              'reopen combination uuids differ')
        for profile in snapshot['profiles']:
            check(profile['composed'].get('blocked') is None if isinstance(profile['composed'], dict)
                  else False,
                  f"reopen compose blocked for {profile['name']}: {profile['composed']!r}")
            for adapter, merged in (profile['composed'] or {}).items():
                if isinstance(merged, dict) and merged.get('installation_root') is not None:
                    check(merged['installation_root']
                          == snapshot['registry']['entries'].get(adapter, {}).get('installation_root'),
                          f"reopen/{profile['name']}/{adapter}: composed path != registry path")
        check(next(profile for profile in snapshot['profiles']
                   if profile['name'] == '组合甲')['repeat_limit'] == 2,
              'reopen lost 甲 run settings')
        out['reopen'] = snapshot
        out['reopen_pid'] = recorded['pid']
        out['launches_reopen'] = recorded.get('launches', [])
        out['launches'] = list(out['launches_phase1']) + list(out['launches_reopen'])
        check(not out['launches'], f"process launches recorded: {out['launches']}")
        runner_path.unlink(missing_ok=True)


def mask_text(text, masks):
    """Single masking exit: every textual form of every prefix is replaced,
    case-insensitively - the leak assertion compares lowered text, so a
    differently-cased path must be replaced, not leaked."""
    for needle, label in masks:
        for variant in path_variants(needle):
            pattern = re.compile(re.escape(variant), re.IGNORECASE)
            text = pattern.sub(lambda _match, _label=label: _label, text)
    return text


def mask_tree(obj, masks):
    if isinstance(obj, str):
        return mask_text(obj, masks)
    if isinstance(obj, dict):
        return {key: mask_tree(value, masks) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_tree(value, masks) for value in obj]
    return obj


def assert_no_leak(obj, needles, path='out'):
    """Single leak exit: raw, forward-slash, doubled-backslash (repr-escaped)
    and case-insensitive forms are all refused."""
    if isinstance(obj, str):
        lowered = obj.lower()
        for needle in needles:
            for variant in path_variants(needle):
                check(variant.lower() not in lowered,
                      f'path leak at {path}: {obj[:120]!r}')
    elif isinstance(obj, dict):
        for key, value in obj.items():
            assert_no_leak(value, needles, f'{path}.{key}')
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            assert_no_leak(value, needles, f'{path}[{index}]')


def run_scenario_in_process(name, out_path):
    """The single owner of final serialization: mask every prefix variant,
    refuse to publish any leak, mask the stderr message too."""
    out = {'scenario': name}
    state = {}
    failure = None
    from unittest.mock import patch
    context = nullcontext()
    if os.environ.get('REPRO_NEGATIVE') == 'compose':
        # Injected outage: every composition blocks. Scenarios that do not
        # expect blocked compositions must fail loudly.
        from gachaops.planner import ConfigError as _ConfigError
        from gachaops import configuration as _configuration
        context = patch.object(_configuration, 'compose_configuration',
                               side_effect=_ConfigError('synthetic compose outage'))
    try:
        with context:
            SCENARIOS[name](out, state)
        out['ok'] = True
    except Exception as error:
        # Any exception — assertion or unexpected crash — becomes the same
        # masked failure record; a raw traceback would leak real paths and
        # bypass the single serialization exit.
        failure = f'{type(error).__name__}: {error}'
        out.update({'ok': False, 'failure': failure})
    private = out.pop('_private', {'tmp': '', 'samples': []})
    needles = [private['tmp']] + private['samples']
    masks = [(needle, label) for needle, label in
             zip(needles, ['<tmp>'] + ['<sample>'] * len(private['samples'])) if needle]
    # The boundary also masks prefixes no scenario records: the system temp
    # (where the reopen runner lives), the repository checkout and the
    # interpreter directory all identify the machine and must not survive
    # into any failure text, stderr excerpt or result file.
    foreign = [(tempfile.gettempdir(), '<systmp>'), (str(REPO), '<repo>'),
               (str(Path(sys.executable).resolve().parent), '<python>')]
    needles += [prefix for prefix, _label in foreign]
    masks += [(prefix, label) for prefix, label in foreign if prefix]
    try:
        out = mask_tree(out, masks)
        if private['tmp']:
            assert_no_leak(out, needles)
        failure = mask_text(failure, masks) if failure else failure
        Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
        if failure:
            print(failure, file=sys.stderr)
            return 1
        return 0
    except Exception:
        # The masking, leak-check or serialization exit itself failed. Only
        # fixed text may leave: anything derived from the original error
        # could carry an unmasked path or a bare traceback.
        try:
            Path(out_path).write_text(json.dumps(
                {'scenario': name, 'ok': False,
                 'failure': 'internal masking failure; details withheld'},
                ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass
        print('internal masking failure; scenario result withheld to avoid path leaks',
              file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--scenario', choices=sorted(SCENARIOS))
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--out', default='issue4-results.json')
    arguments = parser.parse_args()
    if arguments.scenario:
        return run_scenario_in_process(arguments.scenario, arguments.out)
    if not arguments.all:
        parser.error('choose --scenario NAME or --all')
    system_temp = tempfile.gettempdir()
    merged = {}
    failed = []
    for name in sorted(SCENARIOS):
        fresh = Path(arguments.out).with_name(f'{Path(arguments.out).stem}-{name}.this-run.json')
        if fresh.exists():
            fresh.unlink()  # never accept a stale result file as this run's output
        try:
            completed = subprocess.run(
                [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                 '--scenario', name, '--out', str(fresh)],
                cwd=str(REPO), capture_output=True, text=True, timeout=240,
                encoding='utf-8', errors='replace')
            returncode, timed_out = completed.returncode, False
            stderr = mask_text(completed.stderr[-2000:], [
                (tempfile.gettempdir(), '<systmp>'), (str(REPO), '<repo>'),
                (str(Path(sys.executable).resolve().parent), '<python>')])
        except subprocess.TimeoutExpired:
            returncode, timed_out, stderr = None, True, f'scenario {name} timed out after 240s'
        entry = {'returncode': returncode, 'timed_out': timed_out, 'stderr': stderr}
        try:
            entry['result'] = json.loads(fresh.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            entry['result'] = None
            entry['stderr'] = f'no fresh result file written this run; {stderr}'
        schema_ok = bool(entry['result']) and entry['result'].get('scenario') == name \
            and 'launches' in entry['result'] and entry['result'].get('exit_code') == 0 \
            and entry['result'].get('ok') is True
        if returncode != 0 or timed_out or not schema_ok:
            failed.append(name)
        entry['verdict'] = 'FAIL' if name in failed else 'PASS'
        merged[name] = entry
        fresh.unlink(missing_ok=True)
    summary = {'verdict': {name: entry['verdict'] for name, entry in merged.items()},
               'all_passed': not failed, 'scenarios': merged}
    # The aggregated output — including FAIL verdicts and stderr excerpts — is
    # scanned with the system temp prefix in every textual form before publish.
    try:
        assert_no_leak(summary, [system_temp, str(REPO),
                                 str(Path(sys.executable).resolve().parent)])
        Path(arguments.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    except AssertionError as error:
        print(mask_text(str(error), [(system_temp, '<tmp>')]), file=sys.stderr)
        return 1
    except Exception:
        # Fixed text only: the raw error could name unmasked paths.
        print('internal masking or serialization failure; aggregate withheld to avoid path leaks',
              file=sys.stderr)
        return 1
    print(json.dumps(summary['verdict'], ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
