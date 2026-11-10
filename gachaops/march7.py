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

from .ok_adapter import runner_environment
from .preflight import desktop_report
from .recovery import save
from .windows_observation import (elevation_report, find_matching_processes,
                                  observation_api, process_identity)
from .onedragon_bridge import creation_ticks
from .app_paths import workspace_root

SUPPORTED_VERSION = 'v2026.9.30'
# v2026.10.16 and v2026.11.8 were reviewed against v2026.9.30: CLI, pause
# control and completion markers are unchanged. Unknown builds still require
# review.
SUPPORTED_VERSIONS = frozenset({SUPPORTED_VERSION, 'v2026.10.16', 'v2026.11.8'})
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def launch_metadata(path):
    # Read only five top-level lifecycle scalars. Tasks/accounts stay opaque.
    keys = {'game_path', 'cloud_game_enable', 'after_finish', 'auto_update', 'log_level'}
    values = {}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        match = re.fullmatch(r'([a-z_]+):\s*(.*?)\s*', line)
        if not match or match[1] not in keys:
            continue
        key, value = match.groups()
        if key in values:
            raise ValueError('duplicate March7 launch metadata: ' + key)
        if value.startswith('"'):
            value, consumed = json.JSONDecoder().raw_decode(value)
            remainder = match[2][consumed:].strip()
            if remainder and not remainder.startswith('#'):
                raise ValueError('unsupported March7 launch scalar')
        elif value.startswith("'"):
            quoted = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", value)
            if not quoted:
                raise ValueError('unsupported March7 launch scalar')
            value = quoted[1].replace("''", "'")
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
        values[key] = value
    if set(values) != keys:
        raise ValueError('missing March7 launch metadata')
    return values


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
    meta = launch_metadata(config)
    blockers = []
    if meta['cloud_game_enable'] != 'false':
        blockers.append('march7_local_game_required')
    if meta['after_finish'] != 'Exit':
        blockers.append('march7_select_exit_in_assistant_gui')
    if meta['auto_update'] != 'true':
        blockers.append('march7_complete_native_first_setup')
    if meta['log_level'] not in ('INFO', 'DEBUG'):
        blockers.append('march7_info_logging_required')
    game = meta['game_path']
    if not Path(game).is_absolute() or not Path(game).is_file():
        blockers.append('game_path_unverified')
    if elevation_report()['elevated'] is not True:
        blockers.append('supervisor_elevation_required')
    desktop = desktop_report()
    if desktop['status'] != 'observed_available':
        blockers.extend(desktop.get('blockers') or ['desktop_unavailable'])
    targets = [str(exe), str(launcher), str(root / 'March7th Updater.exe'), game]
    observed = find_matching_processes(targets)
    if observed['status'] != 'observed' or observed['matches']:
        blockers.append('existing_runner_or_game_not_exclusive')
    return {'installation_root': str(root), 'cwd': str(root), 'launcher': str(launcher),
            'argv_candidate': [str(exe), 'main'], 'game_path': game,
            'observation_targets': targets, 'runner_version': version,
            'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
            'completion_kind': 'march7', 'blockers': blockers,
            'game_start': 'assistant', 'borrowed_game': [],
            'task_configuration': 'assistant_current_saved_preset'}


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
        if message == '每日实训未完成':
            daily_incomplete = True
        elif message == '每日实训已完成':
            daily_incomplete = False
            failed.clear()
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
