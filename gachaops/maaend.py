"""MXU saved-instance entry; read-only task configuration and native callbacks."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .preflight import desktop_report
from .windows_observation import elevation_report, find_matching_processes
from .recovery import save


def launcher_install_dir():
    """Read the Global launcher's exact official uninstall key, never execute it."""
    import winreg
    key = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\d9f71f86325c5b9733268efda40413c8'
    locations = set()
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
                    value, kind = winreg.QueryValueEx(handle, 'InstallLocation')
                if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
                    raise ValueError('invalid Global launcher install location')
                path = Path(os.path.expandvars(value.strip().strip('"')))
                if not path.is_absolute():
                    raise ValueError('Global launcher path must be absolute')
                locations.add(path.resolve(strict=True))
            except FileNotFoundError:
                continue
    if len(locations) != 1:
        raise ValueError('Global launcher installation missing or ambiguous')
    return locations.pop()


def native_launch_paths(root, instance, task):
    definition = root / 'tasks/pretasks/LaunchGamePC.json'
    if definition.is_symlink() or not definition.resolve().is_relative_to(root):
        raise ValueError('external native launch definition')
    pretask = json.loads(definition.read_text(encoding='utf-8-sig')).get('pretask', {})
    agent = root / 'agent/go-service.exe'
    if (pretask.get('name') != 'LaunchGamePC' or pretask.get('exec') != 'agent/go-service'
            or pretask.get('args') != ['--pretask', 'LaunchGamePC']
            or agent.is_symlink() or not agent.is_file() or not agent.resolve().is_relative_to(root)
            or instance.get('controllerName') != 'Win32-Front'):
        raise ValueError('native LaunchGamePC entry is not verified')
    options = task.get('optionValues', {})
    region = options.get('LaunchGamePCRegion', {})
    mode = options.get('LaunchGamePCLauncherPath', {})
    if (region.get('type') != 'select' or region.get('caseName') != 'Global'
            or mode.get('type') != 'select' or mode.get('caseName') != 'Auto'):
        raise ValueError('新版终末地启动目前仅核验 Global / Auto 启动器设置')
    install = launcher_install_dir()
    launcher = install / 'Launcher.exe'
    games = list((install / 'games').glob('*/Endfield.exe'))
    launcher_processes = list(install.glob('*/Games.exe'))
    paths = [launcher, *games, *launcher_processes]
    if (len(games) != 1 or not launcher_processes or any(
            p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(install) for p in paths)):
        raise ValueError('新版终末地游戏或启动器路径缺失、存在歧义或尚未核验')
    return str(games[0].resolve()), [str(p.resolve()) for p in [launcher, *launcher_processes]]


def prepare(row):
    root = Path(row['installation_root']).resolve(strict=True)
    exe = root / 'MaaEnd.exe'
    for path in [exe, root / 'interface.json', root / 'config/mxu-MaaEnd.json', root / 'maafw/MaaFramework.dll']:
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('missing or external MaaEnd installation entry')
    binary = exe.read_bytes()
    if not all(flag.encode() in binary for flag in ['--autostart', '--instance', '--quit-after-run']):
        raise ValueError('MXU automatic saved-instance entry unavailable')
    metadata = json.loads((root / 'interface.json').read_text(encoding='utf-8-sig'))
    config = json.loads((root / 'config/mxu-MaaEnd.json').read_text(encoding='utf-8-sig'))
    instances = config.get('instances', [])
    selected = [i for i in instances if i.get('id') == config.get('lastActiveInstanceId')]
    if len(selected) != 1 or not isinstance(selected[0].get('name'), str) or not selected[0]['name']:
        raise ValueError('select and save a MaaEnd GUI instance first')
    instance = selected[0]
    if sum(i.get('name') == instance['name'] for i in instances) != 1:
        raise ValueError('duplicate MXU instance names; automatic target is ambiguous')
    # Inspect only launch metadata. Never inspect/rebuild the native tasks/options.
    manual = row.get('game_start', 'assistant') == 'manual'
    actions = instance.get('preActions') or []
    tasks = instance.get('tasks')
    native = [t for t in tasks if isinstance(t, dict) and t.get('taskName') == '__MXU_PRETASK__LaunchGamePC'] if isinstance(tasks, list) else []
    launcher_targets = []
    launches = [a for a in actions if (manual or a.get('enabled') is True)
                and isinstance(a.get('program'), str) and Path(a['program']).name.lower() == 'endfield.exe']
    if native:
        if len(native) != 1 or (not manual and native[0].get('enabled') is not True):
            raise ValueError('新版终末地需要唯一且已启用的 LaunchGamePC 启动任务')
        if any(a.get('enabled') is not False for a in actions):
            raise ValueError('请在原助手禁用旧启动前动作，避免与新版 LaunchGamePC 重复启动')
        game, launcher_targets = native_launch_paths(root, instance, native[0])
    else:
        if len(launches) != 1 or launches[0].get('useCmd') is not False:
            raise ValueError('save a direct Endfield.exe pre-action in the selected MaaEnd GUI instance')
        game = launches[0]['program']
    blockers = []
    # A borrowed game must never be replaced by a launch pre-action. Read the
    # disabled direct entry only to learn its path; leave all native tasks intact.
    if manual and any(a.get('enabled') is not False for a in actions):
        blockers.append('maaend_disable_launch_preactions')
    if manual and native and native[0].get('enabled') is not False:
        blockers.append('maaend_disable_native_launch_task')
    if not Path(game).is_absolute() or not Path(game).is_file():
        blockers.append('game_path_unverified')
    if elevation_report()['elevated'] is not True:
        blockers.append('supervisor_elevation_required')
    desktop = desktop_report()
    if desktop['status'] != 'observed_available':
        blockers.extend(desktop.get('blockers') or ['desktop_unavailable'])
    targets = [str(exe), game, str(root / 'agent/go-service.exe'), str(root / 'agent/cpp-algo.exe'), *launcher_targets]
    observed = find_matching_processes(targets)
    borrowed = []
    if observed['status'] != 'observed':
        blockers.append('existing_runner_or_game_not_exclusive')
    elif manual:
        borrowed = [r for r in observed['matches']
                    if os.path.normcase(r['image']) == os.path.normcase(game)]
        if any(r not in borrowed for r in observed['matches']):
            blockers.append('existing_runner_not_exclusive')
        if len(borrowed) != 1:
            blockers.append('manual_game_requires_exactly_one_running_instance')
    elif observed['matches']:
        blockers.append('existing_runner_or_game_not_exclusive')
    return {'installation_root': str(root), 'cwd': str(root),
            'argv_candidate': [str(exe), '--autostart', '--instance', instance['name'], '--quit-after-run'],
            'launcher': str(exe), 'game_path': game, 'observation_targets': targets,
            'runner_version': metadata.get('version'), 'completion_kind': 'maa-end',
            'instance_name': instance['name'], 'instance_id': instance['id'],
            'controller_name': instance.get('controllerName'), 'blockers': blockers,
            'game_start': 'manual' if manual else 'assistant', 'borrowed_game': borrowed,
            'task_configuration': 'assistant_current_saved_preset'}


def log_paths(root):
    directory = Path(root) / 'debug'
    return sorted(p for p in directory.glob('*.log') if p.name == 'maafw.log'
                  or re.fullmatch(r'maafw\.bak\..+\.log', p.name)
                  or re.fullmatch(r'\d{4}-\d{2}-\d{2}-\d+\.log', p.name))


@dataclass(frozen=True)
class Cursor:
    root: str
    instance: str
    captured_at_ns: int
    baseline: dict
    offset: int = 0


def bind_execution(current, directory):
    baseline = {}
    for path in log_paths(current['cwd']):
        if path.is_symlink():
            raise ValueError('external MaaEnd log')
        with path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            content = stream.read(stat.st_size)
        baseline[(stat.st_dev, stat.st_ino)] = (len(content), hashlib.sha256(content).hexdigest(), not content or content.endswith(b'\n'))
    cursor = Cursor(current['cwd'], current['instance_name'], time.time_ns(), baseline)
    save(Path(directory) / 'native-log-baseline.json', {
        'instance_name': cursor.instance, 'captured_at_ns': cursor.captured_at_ns,
        'files': [{'identity': list(identity), 'offset': value[0], 'prefix_sha256': value[1],
                   'at_line_boundary': value[2]} for identity, value in baseline.items()]})
    return current, cursor


FRAME = re.compile(r'^\[(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]\[\w+\]\[Px(?P<pid>\d+)\].*')
GUI = re.compile(r'^(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \w+\s+\[(?:Task|App)\] (?P<message>.*)$')
EVENT = re.compile(r'\[msg=Tasker\.Task\.(Starting|Succeeded|Failed)\] \[details=(\{.*\})\]\s*$')


def evaluate(frame_lines, gui_lines, exit_code, root_pid, instance):
    initialized = False
    submitted, statuses, entries, starts = [], {}, {}, set()
    quit_seen = False
    for line in frame_lines:
        event = FRAME.fullmatch(line)
        if not event:
            continue
        if int(event['pid']) != root_pid:
            raise ValueError('MaaFramework log writer is not owned root')
        if '[Logger] MAA Process Start' in line:
            initialized = True
        callback = EVENT.search(line)
        if callback:
            details = json.loads(callback[2])
            task_id = details.get('task_id')
            if type(task_id) is not int or task_id <= 0:
                raise ValueError('invalid Maa task identity')
            if callback[1] == 'Starting':
                starts.add(task_id)
            else:
                if task_id in statuses:
                    raise ValueError('duplicate Maa task terminal callback')
                statuses[task_id] = callback[1]
                entries[task_id] = details.get('entry', str(task_id))
    for line in gui_lines:
        event = GUI.fullmatch(line)
        if not event:
            continue
        message = event['message']
        prefix = f'实例 {instance}: 任务已提交, task_ids: '
        if message.startswith(prefix):
            ids = json.loads(message[len(prefix):])
            if not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
                raise ValueError('invalid submitted Maa task list')
            submitted.append(ids)
        if message == '自动执行任务完成，关闭自身':
            quit_seen = True
    # Started task IDs must equal the single submitted batch: an extra Starting
    # without a terminal callback is unfinished work, never confirmed success.
    complete = (initialized and len(submitted) == 1 and set(submitted[0]) == set(statuses)
                and starts == set(submitted[0]) and quit_seen)
    failed = [entries[i] for i, status in statuses.items() if status == 'Failed']
    status = 'completion_unconfirmed'
    if initialized and exit_code is not None and exit_code != 0:
        status = 'failed'
    elif complete and exit_code == 0:
        status = 'partial_failure' if failed else 'success'
    return {'status': status, 'exit_code': exit_code, 'writer_verified': initialized,
            'instance_name': instance, 'submitted_batches': submitted,
            'task_statuses': {str(k): v for k, v in statuses.items()},
            'failed_applications': failed, 'native_quit_seen': quit_seen}


def observe(cursor, exit_code, root_pid, owned):
    try:
        frames, gui = [], []
        auxiliary_writers = {}
        lower = cursor.captured_at_ns / 1e9 - 1
        if not any(r.get('pid') == root_pid and r.get('in_owned_job') is True for r in owned):
            raise ValueError('MaaEnd root ownership not observed')
        agent_images = {str(Path(cursor.root) / 'agent' / name).casefold()
                        for name in ('go-service.exe', 'cpp-algo.exe')}
        agents = {r['pid']: r for r in owned
                  if r.get('in_owned_job') is True
                  and str(r.get('image', '')).casefold() in agent_images
                  and type(r.get('creation_ticks')) is int and r['creation_ticks'] > 0}
        for path in log_paths(cursor.root):
            if path.is_symlink():
                raise ValueError('external MaaEnd log')
            with path.open('rb') as stream:
                stat = os.fstat(stream.fileno())
                content = stream.read(stat.st_size)
            old = cursor.baseline.get((stat.st_dev, stat.st_ino))
            if old and len(content) >= old[0] and hashlib.sha256(content[:old[0]]).hexdigest() == old[1]:
                content = content[old[0]:]
                if not old[2]:
                    content = content.partition(b'\n')[2]
            # Native autoClearLogsOnLaunch may delete/replace the logs. A fresh
            # file still must prove this run's timestamp and exact root writer.
            content = content[:content.rfind(b'\n') + 1]
            for line in content.decode('utf-8', errors='strict').splitlines():
                event = FRAME.fullmatch(line) if path.name.startswith('maafw') else GUI.fullmatch(line)
                if not event:
                    continue
                fmt = '%Y-%m-%d %H:%M:%S.%f' if path.name.startswith('maafw') else '%Y-%m-%d %H:%M:%S'
                stamp = datetime.strptime(event['time'], fmt).timestamp()
                if stamp < lower:
                    raise ValueError('MaaEnd log contains old events after replacement')
                if path.name.startswith('maafw') and int(event['pid']) != root_pid:
                    # MaaFramework's agent uses the same native log. Only an
                    # observed Job-owned installation agent may write alongside
                    # the root; its initialization/callbacks cannot prove the
                    # GUI's submitted batch completed.
                    writer = agents.get(int(event['pid']))
                    if writer is None:
                        raise ValueError('MaaFramework log writer is not owned root or installation agent')
                    created = writer['creation_ticks'] / 10_000_000 - 11_644_473_600
                    if stamp < created - 1:
                        raise ValueError('MaaFramework agent log predates owned process')
                    auxiliary_writers[writer['pid']] = writer
                    continue
                (frames if path.name.startswith('maafw') else gui).append(line)
        result = evaluate(frames, gui, exit_code, root_pid, cursor.instance)
        result['auxiliary_log_writers'] = list(auxiliary_writers.values())
        return result
    except (OSError, ValueError, UnicodeError, TypeError) as exc:
        return {'status': 'completion_unconfirmed', 'reason': str(exc), 'exit_code': exit_code}
