"""Exact MXU saved-preset contract, independent of the legacy MFA pipe protocol."""
from datetime import datetime
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

from . import maaend
from .preflight import desktop_report
from .recovery import save
from .windows_observation import elevation_report, find_matching_processes

# Read from the official v2.8.3 archive, SHA256 a7c813ef0392cd563ed75c4a254954ed
# 043ecf320ce7d509c961cdc9709b4a2c. The MXU framework itself is v2.8.1.
EXPECTED_COMPONENTS = {
    'MaaGF2Exilium.exe': 'ec594d4d59912403f409a8ef0681da99a6002131c6568e6b56b768282a6cfc26',
    'maafw/MaaFramework.dll': '1c5e4112ca1dca12021a847836fbf935c46df334449b9073e4eeff2b0c294fd4',
    'python/python.exe': '8244fbfd1f5bc4ba79309ac0abd0b3a51293ca41bfcad8e080d9480546760fe6',
    'agent/main.py': '458e2496d30a342e395552f1a60e02d55e6fc462edea941d4c9378b6c09bdbbe',
}
RESOURCE_VERSION = 'v2.8.3'
FRAMEWORK_VERSION = '2.8.1'


def _redirected(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _inside(root, value, directory=False):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('MXU installation path is missing')
    part = Path(value)
    if part.is_absolute() or '..' in part.parts:
        raise ValueError('MXU installation path must stay inside its directory')
    path = root / part
    current = root
    for name in part.parts:
        current = current / name
        if _redirected(current):
            raise ValueError('MXU external or redirected installation entry')
    if not path.resolve(strict=True).is_relative_to(root) or not (path.is_dir() if directory else path.is_file()):
        raise ValueError('MXU installation entry unavailable')
    return path


def _digest(path):
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        result = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('MXU files changed while checking; 请重新检查')
    return result


def _game(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('MXU requires a direct absolute game EXE')
    path = Path(value)
    if '..' in path.parts or any(_redirected(parent) for parent in (path, *path.parents)):
        raise ValueError('MXU game path or ancestor is redirected')
    if not path.is_file() or path.name.casefold() != 'gf2_exilium.exe':
        raise ValueError('MXU direct GF2_Exilium.exe is unavailable')
    return path.resolve(strict=True)


def installation_metadata(root, config_file, game_path):
    """Hash opaque native data; never copy account values into the descriptor."""
    root = Path(root).resolve(strict=True)
    paths = {_inside(root, config_file), _inside(root, 'interface.json')}
    paths.update(_inside(root, name) for name in EXPECTED_COMPONENTS)
    # Include imported task/option definitions, Python code and all resources;
    # limiting the guard to EXE/interface/config misses native startup changes.
    pending = [root / name for name in ('agent', 'python', 'maafw', 'resource', 'tasks', 'options')
               if (root / name).exists()]
    inspected = 0
    while pending:
        path = pending.pop()
        inspected += 1
        if inspected > 100_000 or _redirected(path):
            raise ValueError('MXU installation tree is too large or redirected')
        if path.is_dir():
            pending.extend(path.iterdir())
        elif path.is_file():
            paths.add(path)
        else:
            raise ValueError('MXU installation has an unknown file type')
    files = [{'path': path.relative_to(root).as_posix(), 'sha256': _digest(path)}
             for path in sorted(paths)]
    game = _game(game_path)
    return {'files': files, 'game': {'path': str(game), 'sha256': _digest(game)}}


def _json(root, name, consumed):
    path = _inside(root, name)
    raw = path.read_bytes()
    # installation_metadata records POSIX-relative paths; compare the same
    # representation so alternate import spellings are not rejected as drift.
    consumed[path.relative_to(root).as_posix()] = hashlib.sha256(raw).hexdigest()
    value = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('MXU configuration must be an object')
    return value


def _imports(root, interface, consumed):
    names = set()
    visited = {'interface.json'}

    def visit(data, depth):
        if depth > 32:
            raise ValueError('MXU interface import depth exceeded')
        tasks = data.get('task', [])
        if not isinstance(tasks, list) or any(not isinstance(t, dict) or not isinstance(t.get('name'), str) for t in tasks):
            raise ValueError('MXU task declarations are invalid')
        names.update(t['name'] for t in tasks)
        imports = data.get('import', [])
        if not isinstance(imports, list):
            raise ValueError('MXU interface imports are invalid')
        for name in imports:
            if not isinstance(name, str) or name in visited:
                raise ValueError('MXU duplicate or cyclic interface import')
            visited.add(name)
            child = _json(root, name, consumed)
            if {'controller', 'resource', 'agent', 'name', 'version'} & set(child):
                raise ValueError('MXU imported startup selectors are unsupported')
            visit(child, depth + 1)

    visit(interface, 0)
    return names


def _unique(rows, field, value):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('MXU saved metadata is invalid')
    matches = [row for row in rows if row.get(field) == value]
    if len(matches) != 1:
        raise ValueError('MXU selected metadata is missing or ambiguous')
    return matches[0]


def _instance(config):
    instances = config.get('instances')
    instance = _unique(instances, 'id', config.get('lastActiveInstanceId'))
    ids = [row.get('id') for row in instances]
    names = [row.get('name') for row in instances]
    if (any(not isinstance(v, str) or not re.fullmatch(r'[^\x00-\x1f]{1,200}', v) for v in ids + names)
            or len(set(ids)) != len(ids) or len(set(names)) != len(names)
            or instance['name'].startswith('-')):
        raise ValueError('MXU instance IDs/names must be unique and explicit')
    return instance, instances


def _triggers(config, instances):
    settings = config.get('settings')
    if not isinstance(settings, dict):
        raise ValueError('MXU settings unavailable')
    hotkeys = settings.get('hotkeys', {})
    if not isinstance(hotkeys, dict):
        raise ValueError('MXU hotkey settings invalid')
    unsafe = (settings.get('webServerEnabled') is not False
              or hotkeys.get('globalEnabled', False) is not False
              or settings.get('autoRunOnLaunch', False) is not False)
    for row in instances:
        policies = row.get('schedulePolicies', [])
        if not isinstance(policies, list) or any(not isinstance(p, dict) or type(p.get('enabled')) is not bool for p in policies):
            raise ValueError('MXU schedule settings invalid')
        unsafe |= any(p['enabled'] for p in policies)
    return unsafe


def prepare(row, *, root, exe, launcher_sha256):
    error = 'MXU 构建身份未经核验，请取消勾选此助手；不要使用残留 MFA 配置。'
    if _redirected(Path(row['installation_root'])):
        raise ValueError('MXU installation root is redirected')
    for name, expected in EXPECTED_COMPONENTS.items():
        if _digest(_inside(root, name)) != expected:
            raise ValueError(error)
    if launcher_sha256 != EXPECTED_COMPONENTS['MaaGF2Exilium.exe']:
        raise ValueError(error)
    consumed = {}
    interface = _json(root, 'interface.json', consumed)
    project = interface.get('name')
    if (interface.get('version') != RESOURCE_VERSION or interface.get('interface_version') != 2
            or not isinstance(project, str) or not re.fullmatch(r'[^<>:"/\\|?*\x00-\x1f]{1,100}', project)):
        raise ValueError('MXU resource/interface version is unsupported')
    task_names = _imports(root, interface, consumed)
    config_file = 'config/mxu-' + project + '.json'
    config = _json(root, config_file, consumed)
    if config.get('version') != '1.0':
        raise ValueError('MXU configuration schema version is unsupported')
    instance, instances = _instance(config)
    controller = _unique(interface.get('controller'), 'name', instance.get('controllerName'))
    options = controller.get('win32', {})
    expected = {'桌面端-前台': ('Foreground', 'Seize', 'Seize'),
                '桌面端-后台': ('Background', 'PostMessageWithWindowPos', 'PostMessageWithWindowPos')}
    if (controller.get('type') != 'Win32' or controller['name'] not in expected
            or not isinstance(options, dict)
            or tuple(options.get(k) for k in ('screencap', 'mouse', 'keyboard')) != expected[controller['name']]):
        raise ValueError('MXU saved desktop controller is unsupported')
    resource = _unique(interface.get('resource'), 'name', instance.get('resourceName'))
    if resource.get('name') != '中文资源(PC端)' or resource.get('path') != ['resource/base', 'resource/resource_pc']:
        raise ValueError('MXU saved PC resource is unsupported')
    for path in resource['path']:
        _inside(root, path, directory=True)
    if interface.get('agent') != {'child_exec': 'python/python.exe', 'child_args': ['-u', './agent/main.py']}:
        raise ValueError('MXU agent entry is unsupported')
    device = instance.get('savedDevice')
    if not isinstance(device, dict) or not isinstance(device.get('windowName'), str):
        raise ValueError('MXU saved desktop window is unavailable')
    try:
        if any(not isinstance(options.get(key), str) or not options[key] for key in ('class_regex', 'window_regex')):
            raise ValueError('MXU saved window specification unavailable')
        if re.search(options['window_regex'], device['windowName']) is None:
            raise ValueError('MXU saved window does not match the selected controller')
    except re.error as exc:
        raise ValueError('MXU saved window specification invalid') from exc
    game = _game(device.get('connectedProgramPath'))
    actions = instance.get('preActions')
    if not isinstance(actions, list) or any(not isinstance(a, dict) or type(a.get('enabled')) is not bool for a in actions):
        raise ValueError('MXU launch pre-actions are invalid')
    active = [a for a in actions if a['enabled']]
    if len(active) != 1 or instance.get('preAction'):
        raise ValueError('MXU requires one direct game launch pre-action')
    action = active[0]
    if (action.get('useCmd') is not False or action.get('args') != ''
            or action.get('waitForExit') is not False or type(action.get('skipIfRunning')) is not bool
            or _game(action.get('program')) != game):
        raise ValueError('MXU requires a direct game EXE, empty args and no shell/wait')
    if row.get('game_start', 'assistant') != 'assistant':
        raise ValueError('MXU manual game start is unsupported')
    tasks = instance.get('tasks')
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('MXU saved tasks are unavailable')
    blockers = []; ids = set(); selected = 0
    for task in tasks:
        if (not isinstance(task, dict) or not isinstance(task.get('id'), str) or not task['id']
                or task['id'] in ids or type(task.get('enabled')) is not bool
                or not isinstance(task.get('taskName'), str)):
            raise ValueError('MXU saved task identity is invalid')
        ids.add(task['id'])
        cache = task.get('enabledByController', {})
        if (not isinstance(cache, dict) or any(type(value) is not bool for value in cache.values())
                or cache.get(controller['name'], task['enabled']) != task['enabled']):
            raise ValueError('MXU task controller state is inconsistent; save the native preset')
        if not task['enabled']:
            continue
        selected += 1
        if task['taskName'].startswith('__MXU_'):
            blockers.append('maagf2_mxu_unsafe_native_action')
        elif task['taskName'] not in task_names:
            raise ValueError('MXU saved task is missing from the resource interface')
    if not selected:
        raise ValueError('MXU saved preset has no enabled tasks')
    if _triggers(config, instances):
        blockers.append('maagf2_mxu_disable_external_triggers')
    if elevation_report().get('elevated') is not True:
        blockers.append('supervisor_elevation_required')
    desktop = desktop_report()
    if desktop.get('status') != 'observed_available':
        blockers.extend(desktop.get('blockers') or ['desktop_unavailable'])
    targets = [str(exe), str(game), str(root / 'python/python.exe')]
    # This game's installed launcher can reopen after the game exits. Observe
    # it without launching or adopting it, including when it escapes the Job.
    official_launcher = game.parent.parent / 'PCLauncher.exe'
    launcher_path = None
    if official_launcher.is_file():
        if _redirected(official_launcher) or official_launcher.resolve().parent != game.parent.parent.resolve():
            raise ValueError('MXU external GF2 launcher entry')
        launcher_path = str(official_launcher.resolve())
        targets.append(launcher_path)
    observation = find_matching_processes(targets)
    if observation.get('status') != 'observed' or observation.get('matches'):
        blockers.append('existing_runner_or_game_not_exclusive')
    metadata = installation_metadata(root, config_file, device['connectedProgramPath'])
    actual = {record['path']: record['sha256'] for record in metadata['files']}
    if any(actual.get(name) != digest for name, digest in {**EXPECTED_COMPONENTS, **consumed}.items()):
        raise ValueError('MXU files changed during preparation; 请重新检查')
    return {'installation_root': str(root), 'cwd': str(root), 'launcher': str(exe),
            'launcher_sha256': launcher_sha256,
            'argv_candidate': [str(exe), '--autostart', '--instance', instance['name'], '--quit-after-run'],
            'game_path': str(game), 'observation_targets': targets, 'official_launcher': launcher_path,
            'game_start': 'assistant', 'borrowed_game': [],
            'runner_version': RESOURCE_VERSION, 'mxu_framework_version': FRAMEWORK_VERSION,
            'completion_kind': 'maa-gf2-mxu', 'instance_id': instance['id'], 'instance_name': instance['name'],
            'mxu_agent_image': str(root / 'python/python.exe'),
            'controller_name': controller['name'], 'blockers': sorted(set(blockers)),
            'mxu_config_file': config_file, 'mxu_game_source_path': device['connectedProgramPath'],
            'native_launch_metadata': metadata,
            'task_configuration': 'assistant_current_saved_preset'}


@dataclass
class Cursor:
    root: str
    instance: str
    captured_at_ns: int
    baseline: dict
    launcher: str
    agent_image: str
    offset: int = 0
    root_identity: tuple | None = None
    log_error: str | None = None


def _log_paths(root):
    directory = root / 'debug'
    if not directory.exists():
        return []
    _inside(root, 'debug', directory=True)
    return maaend.log_paths(root)


def _read_log(root, path):
    _inside(root, path.relative_to(root).as_posix())
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if info.st_size > 64 * 1024 * 1024:
            raise ValueError('MXU native log exceeds the bounded observation limit')
        content = stream.read(info.st_size)
    return (info.st_dev, info.st_ino), content


def bind_execution(current, directory):
    root = Path(current['cwd']).resolve(strict=True)
    baseline = {}
    for path in _log_paths(root):
        identity, content = _read_log(root, path)
        baseline[identity] = (len(content), hashlib.sha256(content).hexdigest(),
                              not content or content.endswith(b'\n'))
    cursor = Cursor(str(root), current['instance_name'], time.time_ns(), baseline,
                    current['launcher'], current['mxu_agent_image'])
    save(Path(directory) / 'native-log-baseline.json', {
        'instance_name': cursor.instance, 'captured_at_ns': cursor.captured_at_ns,
        'files': [{'identity': list(identity), 'offset': value[0], 'prefix_sha256': value[1],
                   'at_line_boundary': value[2]} for identity, value in baseline.items()]})
    return current, cursor


def _image(value):
    return os.path.normcase(str(Path(value).resolve()))


def _created(record):
    ticks = record.get('creation_ticks')
    if type(ticks) is not int or ticks <= 0:
        raise ValueError('MXU writer creation time is unknown')
    return ticks / 10_000_000 - 11_644_473_600


def _read_events(cursor, root_pid, agents, root_created):
    frames, gui, auxiliary = [], [], {}
    lower = max(cursor.captured_at_ns / 1e9 - 1, root_created - 1)
    upper = time.time() + 1
    root = Path(cursor.root)
    try:
        for path in _log_paths(root):
            identity, content = _read_log(root, path)
            old = cursor.baseline.get(identity)
            def matches(value):
                return len(content) >= value[0] and hashlib.sha256(content[:value[0]]).hexdigest() == value[1]
            if not (old and matches(old)) and path.name.startswith('maafw.bak.'):
                copied = [value for value in cursor.baseline.values() if value[0] and matches(value)]
                old = max(copied, key=lambda value: value[0], default=None)
            if old and matches(old):
                content = content[old[0]:]
                if not old[2]:
                    content = content.partition(b'\n')[2]
            boundary = content.rfind(b'\n') + 1
            # Observation runs after exit/cleanup. Even the event name may be
            # truncated, so a marker allowlist cannot authenticate this tail.
            if content[boundary:].strip():
                raise ValueError('MXU log tail is incomplete')
            content = content[:boundary]
            native = path.name.startswith('maafw')
            for line in content.decode('utf-8', errors='strict').splitlines():
                event = (maaend.FRAME if native else maaend.GUI).fullmatch(line)
                if not event:
                    if any(marker in line for marker in ('Tasker.Task.', 'MAA Process Start', '任务已提交', '自动执行任务完成')):
                        raise ValueError('MXU critical log framing is invalid')
                    continue
                stamp = datetime.strptime(event['time'], '%Y-%m-%d %H:%M:%S.%f' if native else '%Y-%m-%d %H:%M:%S').timestamp()
                if not lower <= stamp <= upper:
                    raise ValueError('MXU log predates this run or has an unknown future timestamp')
                if native and 'Tasker.Task.' in line:
                    callback = maaend.EVENT.search(line)
                    if callback is None or line.count('Tasker.Task.') != 1:
                        raise ValueError('MXU Tasker callback is unsupported or malformed')
                    details = json.loads(callback[2])
                    if (not isinstance(details, dict) or type(details.get('task_id')) is not int
                            or details['task_id'] <= 0 or not isinstance(details.get('entry'), str)
                            or not details['entry']):
                        raise ValueError('MXU Tasker callback fields are invalid')
                if native and int(event['pid']) != root_pid:
                    writer = agents.get(int(event['pid']))
                    if writer is None or stamp < _created(writer) - 1:
                        raise ValueError('MXU framework writer is not an owned installation agent')
                    auxiliary[writer['pid']] = writer
                    continue
                if not native and '任务已提交, task_ids: ' in event['message']:
                    if not event['message'].startswith(f'实例 {cursor.instance}: 任务已提交, task_ids: '):
                        raise ValueError('MXU submitted a different native instance')
                (frames if native else gui).append(line)
    except (OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
        cursor.log_error = str(exc)
        raise
    return frames, gui, auxiliary


def observe(cursor, exit_code, root_pid, owned):
    try:
        if cursor.log_error:
            raise ValueError(cursor.log_error)
        roots = [record for record in owned if record.get('pid') == root_pid and record.get('in_owned_job') is True]
        if len(roots) != 1 or _image(roots[0].get('image', '')) != _image(cursor.launcher):
            raise ValueError('MXU root image/Job ownership is not observed')
        root = roots[0]
        created = _created(root)
        if created < cursor.captured_at_ns / 1e9 - 1 or created > time.time() + 1:
            raise ValueError('MXU root predates this run')
        identity = (root_pid, root['creation_ticks'], _image(root['image']))
        if cursor.root_identity is not None and cursor.root_identity != identity:
            cursor.log_error = 'MXU root process identity changed'
            raise ValueError(cursor.log_error)
        cursor.root_identity = identity
        agents = {}
        for record in owned:
            if record.get('pid') == root_pid or record.get('in_owned_job') is not True:
                continue
            if _image(record.get('image', '')) != _image(cursor.agent_image):
                continue
            if _created(record) < cursor.captured_at_ns / 1e9 - 1:
                continue
            if record['pid'] in agents:
                raise ValueError('MXU agent process identity is ambiguous')
            agents[record['pid']] = record
        frames, gui, auxiliary = _read_events(cursor, root_pid, agents, created)
        starts = sum('[Logger] MAA Process Start' in line for line in frames)
        if starts > 1:
            raise ValueError('MXU root initialization is ambiguous')
        result = maaend.evaluate(frames, gui, exit_code, root_pid, cursor.instance)
        result['auxiliary_log_writers'] = list(auxiliary.values())
        return result
    except (OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
        return {'status': 'completion_unconfirmed', 'reason': str(exc), 'exit_code': exit_code}
