"""BetterGI 0.66 saved one-dragon preset; never rewrite native settings."""
from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .preflight import desktop_report
from .windows_observation import elevation_report, find_matching_processes
from .recovery import save

SUPPORTED_VERSION = '0.66.0.0'
BUILTINS = {'领取邮件', '合成树脂', '自动秘境', '自动首领讨伐', '自动幽境危战',
            '自动地脉花', '领取每日奖励', '领取尘歌壶奖励'}
FLOW = 'BetterGenshinImpact.ViewModel.Pages.OneDragonFlowViewModel'
HEADER = re.compile(r'^\[\d{2}:\d{2}:\d{2}\.\d{3}\] \[(\w{3})\] '
                    r'\[Primary:S\d+:P(\d+):T(\d+)\] (\S*)\s*$')


def file_version(exe):
    if os.name != 'nt':
        raise ValueError('BetterGI version inspection requires Windows')
    api = ctypes.WinDLL('version', use_last_error=True)
    api.GetFileVersionInfoSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    api.GetFileVersionInfoSizeW.restype = ctypes.c_ulong
    api.GetFileVersionInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    api.VerQueryValueW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint)]
    handle = ctypes.c_ulong()
    size = api.GetFileVersionInfoSizeW(str(exe), ctypes.byref(handle))
    if not size:
        raise ValueError('BetterGI executable version unavailable')
    buffer = ctypes.create_string_buffer(size)
    ptr, length = ctypes.c_void_p(), ctypes.c_uint()
    if (not api.GetFileVersionInfoW(str(exe), 0, size, buffer)
            or not api.VerQueryValueW(buffer, '\\', ctypes.byref(ptr), ctypes.byref(length))
            or length.value < 52):
        raise ValueError('BetterGI executable version unavailable')
    words = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32))
    if words[0] != 0xFEEF04BD:
        raise ValueError('BetterGI executable version invalid')
    return '.'.join(map(str, (words[2] >> 16, words[2] & 65535, words[3] >> 16, words[3] & 65535)))


def native_file(root, relative):
    path = root / relative
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('missing or external BetterGI installation/configuration entry')
    return path


def prepare(row):
    root = Path(row['installation_root']).resolve(strict=True)
    exe = native_file(root, 'BetterGI.exe')
    if row.get('entry_executable') and Path(row['entry_executable']).resolve() != exe.resolve():
        raise ValueError('BetterGI entry must be installation_root/BetterGI.exe')
    version = file_version(exe)
    if version != SUPPORTED_VERSION:
        raise ValueError(f'BetterGI version {version} is not verified; supported: {SUPPORTED_VERSION}')
    config_path = native_file(root, 'User/config.json')
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    name = config.get('selectedOneDragonFlowConfigName')
    if not isinstance(name, str) or not name or any(c in name for c in '/\\\x00') or name in {'.', '..'}:
        raise ValueError('select and save a BetterGI one-dragon preset first')
    preset_path = native_file(root, f'User/OneDragon/{name}.json')
    preset = json.loads(preset_path.read_text(encoding='utf-8-sig'))
    if preset.get('Name') != name:
        raise ValueError('BetterGI preset name does not match its saved file')
    matches = 0
    for candidate in (root / 'User/OneDragon').glob('*.json'):
        native_file(root, candidate.relative_to(root))
        if json.loads(candidate.read_text(encoding='utf-8-sig')).get('Name') == name:
            matches += 1
    if matches != 1:
        raise ValueError('duplicate BetterGI preset names; automatic target is ambiguous')
    if preset.get('CompletionAction') != '关闭游戏和软件':
        raise ValueError('BetterGI completion action must be 关闭游戏和软件; change it in BetterGI')
    if preset.get('NextTaskId'):
        raise ValueError('BetterGI resume marker is set; choose full run in BetterGI first')
    enabled, definitions, order = (preset.get(k) for k in ('TaskEnabledList', 'TaskDefinitions', 'TaskOrder'))
    if (not isinstance(enabled, dict) or not isinstance(definitions, dict) or not isinstance(order, list)
            or any(type(v) is not bool for v in enabled.values())
            or any(not isinstance(k, str) for k in order) or len(set(order)) != len(order)
            or set(order) != set(enabled) or set(definitions) != set(enabled)):
        raise ValueError('BetterGI saved task order/definitions cannot be verified')
    tasks = [definitions[k] for k in order if enabled[k]]
    if not tasks or any(not isinstance(t, str) or t not in BUILTINS for t in tasks):
        raise ValueError('BetterGI first integration supports nonempty built-in one-dragon tasks only')
    start = config.get('genshinStartConfig') or {}
    game = start.get('installPath')
    blockers = []
    if (not isinstance(game, str) or not Path(game).is_absolute() or not Path(game).is_file()
            or Path(game).name.casefold() != 'genshinimpact.exe'):
        blockers.append('game_path_unverified')
    if start.get('autoEnterGameEnabled') is not True or start.get('startGameWithCmd') is not False:
        blockers.append('bettergi_direct_game_start_required')
    if start.get('genshinStartArgs'):
        blockers.append('bettergi_game_arguments_unverified')
    if (config.get('scriptConfig') or {}).get('autoUpdateBeforeCommandLineRun') is not False:
        blockers.append('bettergi_disable_cli_update_required')
    if elevation_report()['elevated'] is not True:
        blockers.append('supervisor_elevation_required')
    desktop = desktop_report()
    if desktop['status'] != 'observed_available':
        blockers.extend(desktop.get('blockers') or ['desktop_unavailable'])
    targets = [str(exe), game] if isinstance(game, str) else [str(exe)]
    processes = find_matching_processes(targets)
    if processes['status'] != 'observed' or processes['matches']:
        blockers.append('existing_runner_or_game_not_exclusive')
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (exe, config_path, preset_path)}
    return {'installation_root': str(root), 'cwd': str(root), 'launcher': str(exe),
            'argv_candidate': [str(exe), '--startOneDragon', name], 'game_path': game,
            'observation_targets': targets, 'runner_version': version, 'completion_kind': 'bettergi',
            'instance_name': name, 'task_names': tasks, 'launch_hashes': hashes, 'blockers': blockers,
            'game_start': 'assistant', 'borrowed_game': [],
            'task_configuration': 'assistant_current_saved_preset'}


def log_paths(root):
    return sorted(p for p in (Path(root) / 'log').glob('*.log')
                  if re.fullmatch(r'better-genshin-impact\d{8}(?:_\d+)?\.log', p.name))


@dataclass(frozen=True)
class Cursor:
    root: str
    directory: str
    preset: str
    tasks: tuple
    captured_at_ns: int
    baseline: dict
    offset: int = 0


def bind_execution(current, directory):
    # Recheck read-only inputs immediately before the owned launch.
    for name, digest in current['launch_hashes'].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise ValueError('BetterGI executable or saved preset changed during preflight')
    baseline = {}
    for path in log_paths(current['cwd']):
        if path.is_symlink() or not path.resolve().is_relative_to(Path(current['cwd']).resolve()):
            raise ValueError('external BetterGI log')
        digest, size, last = hashlib.sha256(), 0, b''
        with path.open('rb') as stream:
            for content in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(content); size += len(content); last = content[-1:]
        baseline[path.name] = (size, digest.hexdigest(), not size or last == b'\n')
    cursor = Cursor(current['cwd'], str(directory), current['instance_name'],
                    tuple(current['task_names']), time.time_ns(), baseline)
    save(Path(directory) / 'bettergi-launch.json', {**current, 'captured_at_ns': cursor.captured_at_ns,
                                                  'log_baseline': baseline})
    return current, cursor


def records(text):
    record = None
    for line in text.splitlines():
        header = HEADER.fullmatch(line)
        if header:
            if record:
                yield record
            record = {'level': header[1], 'pid': int(header[2]), 'token': int(header[3]),
                      'source': header[4], 'header': line, 'lines': []}
        elif re.match(r'^\[\d{2}:\d{2}:\d{2}\.\d{3}\] \[', line):
            if record:
                yield record
            record = None  # A different instance's continuation must never leak into ours.
        elif record:
            record['lines'].append(line)
    if record:
        yield record


def evaluate(events, exit_code, pid, creation_ticks, preset, tasks, captured_at_ns):
    created_ms = (creation_ticks - 116444736000000000) // 10000
    selected = [e for e in events if e['pid'] == pid]
    tokens = {e['token'] for e in selected}
    if len(tokens) != 1 or not created_ms <= next(iter(tokens)) <= created_ms + 30000:
        raise ValueError('BetterGI log lifetime does not match owned root')
    if next(iter(tokens)) < captured_at_ns // 1000000 - 1000:
        raise ValueError('BetterGI log belongs to an earlier launch')
    starts, ends, counts, dispatch = 0, 0, [], []
    errors, warnings, failed, evidence = [], [], [], []
    task = '启动 / 配置'
    cancelled = False
    runners_ended = 0
    quit_seen = False
    started = False
    for event in selected:
        message = '\n'.join(event['lines']).strip()
        if (ends == 1 and event['source'] == 'BetterGenshinImpact.View.MainWindow'
                and event['level'] == 'DBG' and message == '主窗体退出'):
            quit_seen = True
        if event['source'] == FLOW:
            if message.startswith('启用一条龙配置：'):
                if message != f'启用一条龙配置：{preset}':
                    raise ValueError('BetterGI executed another preset')
                starts += 1
                started = True
            if message.startswith('启用任务总数量: '):
                counts.append(int(message.split(': ')[1]))
            match = re.fullmatch(r'一条龙任务执行: (\d+)/(\d+)', message)
            if match:
                index, total = map(int, match.groups())
                dispatch.append((index, total))
                task = tasks[index - 1] if 0 < index <= len(tasks) else '未知任务'
            if message == '一条龙和配置组任务结束' and event['level'] == 'INF':
                ends += 1
            if '被取消' in message or '没有配置,退出执行' in message:
                cancelled = True
        if not started:
            # Startup exceptions still matter, even before the preset is dispatched.
            if event['level'] in {'ERR', 'FTL'}:
                errors.append(message)
                failed.append(task)
                evidence.append(f'{task}：{message}')
            elif event['level'] == 'WRN':
                warnings.append(message)
            continue
        if event['source'] == 'BetterGenshinImpact.GameTask.TaskRunner':
            if message in {'→ 任务结束', '→ "任务结束"'}:
                runners_ended += 1
            elif message in {'→ 任务启动！', '→ "任务启动！"'} and runners_ended == len(tasks):
                task = '结束前奖励检查'
        if message.startswith('任务中断:'):
            cancelled = True
            errors.append(message)
            failed.append(task)
            evidence.append(f'{task}：{message}')
        elif event['level'] in {'ERR', 'FTL'}:
            errors.append(message)
            failed.append(task)
            evidence.append(f'{task}：{message}')
        elif event['level'] == 'WRN':
            warnings.append(message)
        elif event['level'] == 'INF' and re.search(r'失败|未完成|无法|放弃', message):
            # Native business failures can be INFO. Without a terminal task API,
            # preserve these as uncertainty rather than guessing resource conditions.
            warnings.append(f'{task}：{message}')
    complete = (starts == ends == 1 and quit_seen and counts == [len(tasks)] and not cancelled
                and dispatch == [(i, len(tasks)) for i in range(1, len(tasks) + 1)])
    status = 'completion_unconfirmed'
    if exit_code not in (None, 0):
        status = 'failed'
    elif complete and exit_code == 0:
        status = 'partial_failure' if errors else ('completion_unconfirmed' if warnings else 'success')
    return {'status': status, 'exit_code': exit_code, 'writer_verified': True, 'instance_name': preset,
            'native_quit_seen': quit_seen, 'task_names': list(tasks),
            'failed_applications': list(dict.fromkeys(failed)), 'errors': errors,
            'failure_warnings': warnings, 'task_failure_evidence': evidence,
            'evidence': 'owned_bettergi_one_dragon_end' if complete else 'native_end_unconfirmed',
            'reason': None if status in {'success', 'partial_failure'} else
                      '原生任务中断、警告或结束记录不足；不能确认全部完成。'}


def observe(cursor, exit_code, root_pid, owned):
    try:
        roots = [r for r in owned if r.get('pid') == root_pid and r.get('in_owned_job') is True
                 and str(r.get('image', '')).casefold() == str(Path(cursor.root) / 'BetterGI.exe').casefold()]
        if len(roots) != 1 or type(roots[0].get('creation_ticks')) is not int:
            raise ValueError('BetterGI root ownership not observed')
        events, archived = [], []
        for path in log_paths(cursor.root):
            if path.is_symlink() or not path.resolve().is_relative_to(Path(cursor.root).resolve()):
                raise ValueError('external BetterGI log')
            old = cursor.baseline.get(path.name)
            with path.open('rb') as stream:
                if old:
                    digest, remaining = hashlib.sha256(), old[0]
                    while remaining:
                        chunk = stream.read(min(remaining, 1024 * 1024))
                        if not chunk: break
                        digest.update(chunk); remaining -= len(chunk)
                    if remaining or digest.hexdigest() != old[1]:
                        raise ValueError('BetterGI log truncated or replaced')
                elif path.stat().st_mtime_ns < cursor.captured_at_ns:
                    raise ValueError('untracked historical BetterGI log')
                content = stream.read()
            if old and not old[2]:
                content = content.partition(b'\n')[2]
            text = content[:content.rfind(b'\n') + 1].decode('utf-8-sig', errors='strict')
            own = [e for e in records(text) if e['pid'] == root_pid]
            events.extend(own)
            archived.extend(e['header'] + '\n' + '\n'.join(e['lines']) for e in own)
        (Path(cursor.directory) / 'bettergi-output.log').write_text('\n'.join(archived), encoding='utf-8')
        return evaluate(events, exit_code, root_pid, roots[0]['creation_ticks'], cursor.preset,
                        cursor.tasks, cursor.captured_at_ns)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {'status': 'completion_unconfirmed', 'exit_code': exit_code, 'reason': str(error)}
