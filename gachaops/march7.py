"""March7th's saved full-run CLI, isolated output and owned bridge receipt."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .ok_adapter import runner_environment
from .preflight import desktop_report
from .recovery import save
from .windows_observation import (elevation_report, find_matching_processes,
                                  observation_api, process_identity)
from .onedragon_bridge import creation_ticks
from .app_paths import workspace_root

SUPPORTED_VERSION = 'v2026.9.30'
# v2026.10.16 and v2026.11.8 were reviewed against v2026.9.30: CLI entry,
# GUI-started skip and the full-run markers are unchanged. v2026.11.24 was
# reviewed offline against its published source: the same lifecycle surface
# is unchanged, the daily-training report text changed (both formats are
# parsed), and its config gained platform keys that are read and gated to
# the reviewed local-PC mode. GachaOps does not integrate March7th's native
# pause/resume/stop file protocol; stopping and timeouts rely on owned-Job
# cleanup. Unknown builds still require review.
SUPPORTED_VERSIONS = frozenset({SUPPORTED_VERSION, 'v2026.10.16', 'v2026.11.8',
                                'v2026.11.24'})
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


REQUIRED_KEYS = frozenset({'game_path', 'cloud_game_enable', 'after_finish', 'auto_update', 'log_level'})
PLATFORM_KEYS = frozenset({'ui_profile', 'control_backend', 'local_control_mode', 'no_run_immediately'})
# ruamel reports duplicate keys with the key name quoted inside its problem
# phrase, so a duplicated needed setting can still be named to the user
# instead of falling back to the file-level refusal.
DUPLICATE_KEY_PROBLEM = re.compile(r'found duplicate key "([^"]+)"')


class March7ConfigUnreadable(ValueError):
    """One specific config setting could be named as unreadable.

    Carries the key name so the user is told which setting to re-save in
    the assistant; file-level refusals take the plain ValueError path.
    """

    def __init__(self, key):
        super().__init__('unrecognized March7 config entry: ' + str(key))
        self.key = key


def _encodes_utf8(text):
    """True when the string survives the UTF-8 write the launch descriptor
    performs. Windows APIs accept names with unpaired surrogates, so such a
    path can be saved and even exist, but the JSON write would fail later;
    it is unusable for a run and must be re-saved in the assistant."""
    try:
        text.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def metadata_from_text(text):
    """Load the lifecycle values exactly like the assistant itself does.

    Upstream reads this file with ruamel.yaml's YAML() (module/config/
    config.py), so the same loader runs here and the values it returns are
    passed on untouched: types, truthiness and refusal cases match upstream
    by construction. Only the nine top-level lifecycle keys are projected;
    tasks, accounts and every other part of the file stay opaque.
    """
    try:
        document = YAML().load(text)
    except YAMLError as error:
        duplicate = DUPLICATE_KEY_PROBLEM.search(getattr(error, 'problem', '') or '')
        if duplicate and duplicate[1] in REQUIRED_KEYS | PLATFORM_KEYS:
            raise March7ConfigUnreadable(duplicate[1]) from error
        raise ValueError('March7 config cannot be read; re-save the settings in the assistant. '
                         + str(error)) from error
    if not isinstance(document, dict):
        raise ValueError('March7 config must be a mapping; re-save the settings in the assistant')
    missing = REQUIRED_KEYS.difference(document)
    if missing:
        raise March7ConfigUnreadable(sorted(missing)[0])
    return {key: document[key] for key in REQUIRED_KEYS | PLATFORM_KEYS if key in document}


def launch_metadata(path):
    return metadata_from_text(Path(path).read_text(encoding='utf-8-sig'))


def prepare(row):
    root = Path(row['installation_root']).resolve(strict=True)
    exe = root / 'March7th Assistant.exe'
    launcher = root / 'March7th Launcher.exe'
    config = root / 'config.yaml'
    version_file = root / 'assets/config/version.txt'
    for path in (exe, launcher, config, version_file):
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('missing or external March7 installation entry')
    version = version_file.read_text(encoding='utf-8-sig').strip()
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f'三月七版本尚未核验：{version}。请更新 GachaOps 兼容支持后重新检查。')
    blockers = []
    meta = None
    unreadable_key = None
    try:
        meta = launch_metadata(config)
    except March7ConfigUnreadable as unreadable:
        # One specific setting could be blamed: a required key is missing or
        # the same setting appears twice with different values. Block on a
        # code like every other adapter so blocker lists stay codes; the key
        # travels in its own field for the desktop to name it.
        blockers.append('march7_config_entry_unreadable' if unreadable.key
                        else 'march7_config_structure_unreadable')
        unreadable_key = unreadable.key
    except ValueError:
        # The loader's own refusals point at line and column marks rather
        # than key names, so no single setting can be blamed; re-saving in
        # the assistant rewrites the file in the form upstream itself writes.
        blockers.append('march7_config_structure_unreadable')
    if meta is not None:
        backend = meta.get('control_backend')
        # Upstream resolution (module/config/platform.py get_control_backend):
        # explicit control_backend wins; None falls back to cloud_game_enable
        # truthiness; anything but 'local'/'cloud'/'adb' raises upstream.
        if backend is not None and backend != 'local':
            blockers.append('march7_local_backend_required')
        elif backend is None and meta.get('cloud_game_enable'):
            blockers.append('march7_local_game_required')
        # Upstream defaults a missing/null ui_profile to pc_1080
        # (get_ui_profile), so only a present non-matching value blocks.
        profile = meta.get('ui_profile')
        if profile is not None and profile != 'pc_1080':
            blockers.append('march7_local_pc_profile_required')
        # Upstream treats anything but 'background' as foreground
        # (module/automation/input_base.py), and a null entry keeps the
        # foreground default.
        control_mode = meta.get('local_control_mode')
        if control_mode is not None and control_mode != 'foreground':
            blockers.append('march7_local_foreground_control_required')
        # Upstream runs immediately unless this value is truthy
        # (main.py: args.no_run_immediately or cfg.get_value(...)), so a YAML
        # bool False passes while the string "false" blocks.
        if meta.get('no_run_immediately'):
            blockers.append('march7_run_immediately_required')
        if meta['after_finish'] != 'Exit':
            blockers.append('march7_select_exit_in_assistant_gui')
        if not meta['auto_update']:
            blockers.append('march7_complete_native_first_setup')
        if meta['log_level'] not in ('INFO', 'DEBUG'):
            blockers.append('march7_info_logging_required')
        game = meta['game_path']
        if (isinstance(game, str) and _encodes_utf8(game)
                and Path(game).is_absolute() and Path(game).is_file()):
            targets = [str(exe), str(launcher), str(root / 'March7th Updater.exe'), game]
        else:
            # A value the loader returned but that is not a usable path (a
            # list, mapping, tag or date, or a string containing an unpaired
            # surrogate: Windows names allow it, UTF-8 does not) must not
            # leak past prepare: it would only fail later, inside
            # observation or the launch-descriptor write, so it is dropped
            # here and the blocker states the fact instead.
            blockers.append('game_path_unverified')
            game = None
            targets = [str(exe), str(launcher), str(root / 'March7th Updater.exe')]
    else:
        # The config could not be read; the named-setting blocker above is the
        # actionable message, and the game path is simply unknown.
        game = None
        targets = [str(exe), str(launcher), str(root / 'March7th Updater.exe')]
    if elevation_report()['elevated'] is not True:
        blockers.append('supervisor_elevation_required')
    desktop = desktop_report()
    if desktop['status'] != 'observed_available':
        blockers.extend(desktop.get('blockers') or ['desktop_unavailable'])
    observed = find_matching_processes(targets)
    if observed['status'] != 'observed' or observed['matches']:
        blockers.append('existing_runner_or_game_not_exclusive')
    return {'installation_root': str(root), 'cwd': str(root), 'launcher': str(launcher),
            'argv_candidate': [str(exe), 'main'],
            'game_path': game,
            'observation_targets': targets, 'runner_version': version,
            'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
            'completion_kind': 'march7', 'blockers': blockers,
            'game_start': 'assistant', 'borrowed_game': [],
            'task_configuration': 'assistant_current_saved_preset',
            'unreadable_config_key': unreadable_key}


@dataclass(frozen=True)
class Cursor:
    directory: str
    token: str
    native_executable: str
    offset: int = 0


def bind_execution(current, directory):
    directory = Path(directory).resolve()
    if any((directory / name).exists() for name in ('march7-launch.json', 'march7-output.log', 'march7-result.json')):
        raise ValueError('March7 output destinations must be fresh')
    cursor = Cursor(str(directory), uuid.uuid4().hex, current['argv_candidate'][0])
    descriptor = dict(current, token=cursor.token)
    path = directory / 'march7-launch.json'
    save(path, descriptor)
    command = ([sys.executable, '--march7-worker'] if getattr(sys, 'frozen', False)
               else [sys.executable, '-m', 'gachaops.march7'])
    return dict(current, cwd=str(workspace_root()),
                argv_candidate=command + ['--launch', str(path)]), cursor


def run_child(arguments):
    parser = argparse.ArgumentParser()
    parser.add_argument('--launch', required=True)
    args = parser.parse_args(arguments)
    path = Path(args.launch).resolve(strict=True)
    descriptor = json.loads(path.read_text(encoding='utf-8'))
    root = Path(descriptor['installation_root'])
    if (descriptor['argv_candidate'] != [str(root / 'March7th Assistant.exe'), 'main']
            or descriptor['cwd'] != str(root)):
        raise ValueError('invalid March7 native command')
    if hashlib.sha256((root / 'config.yaml').read_bytes()).hexdigest() != descriptor['config_sha256']:
        raise ValueError('March7 configuration changed before launch; check again')
    version = (root / 'assets/config/version.txt').read_text(encoding='utf-8-sig').strip()
    if version not in SUPPORTED_VERSIONS or version != descriptor.get('runner_version'):
        raise ValueError('三月七版本在预检后发生变化或尚未核验，请重新检查；不会启动游戏。')
    if getattr(sys, 'frozen', False) and os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    env = runner_environment()
    env = {k: v for k, v in env.items() if not k.startswith('MARCH7TH_')}
    env['MARCH7TH_GUI_STARTED'] = 'true'
    receipt = {'schema_version': 1, 'token': descriptor['token'], 'pid': os.getpid(),
               'creation_ticks': creation_ticks(), 'native_argv': descriptor['argv_candidate']}
    with (path.parent / 'march7-output.log').open('xb') as output:
        with subprocess.Popen(descriptor['argv_candidate'], cwd=str(root), env=env,
                              stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW) as child:
            # Retained process handle pins this identity even after native exit.
            receipt['native_identity'] = process_identity(observation_api(), int(child._handle), child.pid)
            code = child.wait()
    receipt.update(exit_code=code, output_sha256=hashlib.sha256(
        (path.parent / 'march7-output.log').read_bytes()).hexdigest())
    save(path.parent / 'march7-result.json', receipt)
    return code


def task_details(lines):
    """Read explicit native outcomes, never infer a cause from a task name."""
    failed = {}
    daily_incomplete = False
    score = None
    recovered = []
    unresolved = []
    for index, line in enumerate(lines):
        message = re.sub(r'^.*?\|\s*(?:INFO|DEBUG|WARNING|ERROR|CRITICAL)\s*\|\s*', '', line)
        match = re.fullmatch(r'任务无法完成[:：]\s*(.+)', message)
        if match:
            failed[match[1]] = line
        match = re.match(r'完成任务[:：]\s*(.+?)\s*\(\+\d+分\)', message)
        if match:
            failed.pop(match[1], None)
        match = re.search(r'(?:当前累计分数|当前分数)[:：]\s*(\d+/\d+)', message)
        if match:
            score = match[1]
        progress = re.search(r'每日实训活跃度\s*(\d+)/(\d+)', message)
        if progress:
            # v2026.11.24 logs the current activity on every record_progress
            # (tasks/daily/training.py), claim or not.
            score = f'{progress[1]}/{progress[2]}'
            if int(progress[1]) < int(progress[2]):
                daily_incomplete = True
                failed['每日实训活跃度'] = line
            else:
                daily_incomplete = False
                failed.pop('每日实训活跃度', None)
        claim = re.search(r'已领取实训奖励，活跃度\s*(\d+)/(\d+)', message)
        if claim:
            # A full claim only settles the training's own failure entry; it
            # says nothing about other tasks, so nothing else is cleared.
            score = f'{claim[1]}/{claim[2]}'
            if int(claim[1]) < int(claim[2]):
                daily_incomplete = True
                failed['每日实训活跃度'] = line
            else:
                daily_incomplete = False
                failed.pop('每日实训活跃度', None)
        match = re.fullmatch(r'实训未完成[:：]\s*(.+)', message)
        if match:
            failed[match[1].strip()] = line
        if message == '每日实训未完成':
            daily_incomplete = True
        elif message == '每日实训已完成':
            # The daily-training completion settles only its own failure
            # entry; it says nothing about other tasks.
            daily_incomplete = False
            failed.pop('每日实训活跃度', None)
        if re.search(r'\|\s*WARNING\s*\|', line) and re.search(r'失败|超时|异常', line):
            switch = re.fullmatch(r'切换到 (.+) 超时，准备重试', message)
            # Only the immediate retry with an explicit matching destination is
            # recovery evidence. A later unrelated visit must not erase failure.
            success = switch and any(re.search(
                r'\|\s*INFO\s*\|\s*切换到[:：]\s*' + re.escape(switch[1]) + r'\s*$', later)
                for later in lines[index + 1:index + 5])
            (recovered if success else unresolved).append(line)
    return {'failed_applications': list(failed), 'task_failure_evidence': list(failed.values()),
            'daily_training_incomplete': daily_incomplete, 'daily_training_score': score,
            'recovered_warnings': recovered, 'failure_warnings': unresolved}


def evaluate(text, exit_code):
    lines = ANSI.sub('', text).splitlines()
    starts = [i for i, line in enumerate(lines) if re.fullmatch(r'\|\s*开始运行\s*\|', line)]
    stops = [i for i, line in enumerate(lines) if re.fullmatch(r'\|\s*停止运行\s*\|', line)]
    errors = [line for line in lines if re.search(r'\|\s*(ERROR|CRITICAL)\s*\|', line)
              or line.startswith('Traceback (most recent call last):')]
    warnings = [line for line in lines if re.search(r'\|\s*WARNING\s*\|', line)
                and re.search(r'失败|超时|异常', line)]
    if exit_code is not None and exit_code != 0:
        return {'status': 'runner_failed', 'exit_code': exit_code, 'errors': errors}
    if exit_code != 0 or len(starts) != 1 or len(stops) != 1 or starts[0] >= stops[0]:
        return {'status': 'completion_unconfirmed', 'reason': 'native_full_run_not_completed', 'errors': errors}
    tail = [line.strip() for line in lines[stops[0]+1:] if line.strip()]
    # The supported CLI prints this fixed welcome box after normal shutdown.
    # Strip only the exact known footer, never arbitrary post-completion output.
    footer = [r'\+-+\+', r'\|\s*欢迎使用 March7th Assistant\s*\|',
              r'\|\s*https://github.com/moesnow/March7thAssistant\s*\|',
              r'\|\s*\|', r'\|\s*此程序为免费开源项目 如果你付了钱请立刻退款\s*\|', r'\+-+\+']
    if len(tail) >= 6 and all(re.fullmatch(pattern, line) for pattern, line in zip(footer, tail[-6:])):
        tail = tail[:-6]
    if not tail or not re.fullmatch(r'-+\s*完成\s*-+', tail[-1]):
        return {'status': 'completion_unconfirmed', 'reason': 'native_stop_not_completed', 'errors': errors}
    details = task_details(lines[starts[0] + 1:stops[0]])
    # Keep pre-run errors/warnings conservatively; only in-cycle explicit retry
    # recovery can discharge a warning.
    details['failure_warnings'] = [line for line in warnings if line not in details['recovered_warnings']]
    failure = errors or details['failure_warnings'] or details['failed_applications'] or details['daily_training_incomplete']
    return {'status': 'partial_failure' if failure else 'success', 'errors': errors, **details,
            'evidence': 'owned_native_full_run_and_stop_completed'}


def observe(cursor, exit_code, root_pid, owned_identities=None):
    try:
        directory = Path(cursor.directory)
        receipt = json.loads((directory / 'march7-result.json').read_text(encoding='utf-8'))
        if (receipt.get('schema_version') != 1 or receipt.get('token') != cursor.token
                or receipt.get('exit_code') != exit_code
                or receipt.get('native_argv') != [cursor.native_executable, 'main']):
            raise ValueError('March7 receipt identity mismatch')
        native = receipt['native_identity']
        if native['image'].casefold() != cursor.native_executable.casefold():
            raise ValueError('March7 native writer image mismatch')
        for identity in (receipt, native):
            if not any(r.get('in_owned_job') is True and r.get('pid') == identity['pid']
                       and r.get('creation_ticks') == identity['creation_ticks'] for r in owned_identities or []):
                raise ValueError('March7 bridge or native writer not owned')
        output = (directory / 'march7-output.log').read_bytes()
        if hashlib.sha256(output).hexdigest() != receipt['output_sha256']:
            raise ValueError('March7 output changed after receipt')
        return dict(evaluate(output.decode('utf-8'), exit_code), native_pid=native['pid'])
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
        return {'status': 'completion_unconfirmed', 'reason': str(error)}


def worker_main(arguments):
    try:
        return run_child(arguments)
    except Exception as error:
        # Windowed builds must not show an unhandled-exception dialog and wait.
        if '--launch' in arguments:
            path = Path(arguments[arguments.index('--launch') + 1])
            try:
                (path.parent / 'march7-error.txt').write_text(str(error), encoding='utf-8')
            except OSError:
                pass
        return 2


if __name__ == '__main__':
    raise SystemExit(worker_main(sys.argv[1:]))
