"""MFAAvalonia saved-instance entry and independent MaaFramework completion."""
from datetime import datetime
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import time

from . import maaend
from .preflight import desktop_report
from .recovery import save
from .mfa_command import instance_key, send_start
from .foreground import request_foreground
from .windows_observation import elevation_report, find_matching_processes

# Pipe names and JSON semantics were checked against this installed Core build.
SUPPORTED_CORE_SHA256 = '4f68e82c3d23dbb77ccd122343f7d4fc892fc752d42395bd52e642a4be3eb29f'
# MFAAvalonia v2.16.2: AppRuntime source equals v2.16.1; installed Core
# IL was independently checked for the key, JSON fields and line/ACK protocol.
ADDITIONAL_VERIFIED_CORE_SHA256 = frozenset({
    '4cc6a10ef5c7cb6c728070d8e28730ea0fa79e104f77d2781314fd55d917c8e7',
})


def prepare(row):
    root = Path(row['installation_root']).resolve(strict=True)
    exe = root / 'MaaGF2Exilium.exe'
    core = root / 'libs/MFAAvalonia.Core.dll'
    for path in (exe, core, root / 'interface.json', root / 'config/config.json',
                 root / 'runtimes/win-x64/native/MaaFramework.dll'):
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('missing or external MaaGF2 installation entry')
    if row.get('entry_executable') and Path(row['entry_executable']).resolve() != exe:
        raise ValueError('MaaGF2 entry must be the installation GUI')
    binary = core.read_bytes()
    if not all(flag.encode('utf-16le') in binary for flag in ('autostart', 'quit-after-run', 'instance')):
        raise ValueError('MFAAvalonia automatic saved-instance entry unavailable')
    config = json.loads((root / 'config/config.json').read_text(encoding='utf-8-sig'))
    all_files = sorted((root / 'config/instances').glob('*.json'))
    files = all_files
    selected = config.get('Instances.LastActive')
    if selected:
        files = [p for p in files if p.stem == selected]
    if (len(files) != 1 or files[0].is_symlink()
            or not files[0].resolve().is_relative_to(root)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', files[0].stem)):
        raise ValueError('saved MaaGF2 instance unavailable or ambiguous')
    instance = json.loads(files[0].read_text(encoding='utf-8-sig'))
    # Only launch/controller metadata is inspected. TaskItems and options stay opaque.
    if (type(instance.get('CurrentController')) is not int or instance['CurrentController'] != 1
            or instance.get('BeforeTask') != 'StartupSoftware'):
        raise ValueError('save the desktop controller and StartupSoftware in MaaGF2 GUI')
    game = instance.get('SoftwarePath')
    global_path = root / 'appsettings.json'
    if global_path.exists() and (global_path.is_symlink() or not global_path.resolve().is_relative_to(root)):
        raise ValueError('external MFAAvalonia global startup configuration')
    global_config = json.loads(global_path.read_text(encoding='utf-8-sig')) if global_path.exists() else {}
    native_global = global_config.get('GlobalStartEnabled', 'False') in (True, 'True')
    blockers = []
    core_digest = hashlib.sha256(binary).hexdigest()
    if core_digest != SUPPORTED_CORE_SHA256 and core_digest not in ADDITIONAL_VERIFIED_CORE_SHA256:
        blockers.append('native_command_protocol_unverified')
    if str(global_config.get('NoAutoStart', 'false')).lower() != 'false':
        blockers.append('native_single_instance_startup_required')
    if native_global and (len(all_files) != 1 or instance.get('IncludeInGlobalStart', True) is not True
                          or str(global_config.get('GlobalExtraLaunchCount', '0')) != '0'):
        blockers.append('native_global_start_must_target_only_saved_instance')
    if instance.get('AutoDetectOnConnectionFailed', True) is not True:
        blockers.append('native_window_detection_required')
    if not isinstance(game, str) or not Path(game).is_absolute() or not Path(game).is_file():
        blockers.append('game_path_unverified')
    if elevation_report()['elevated'] is not True:
        blockers.append('supervisor_elevation_required')
    desktop = desktop_report()
    if desktop['status'] != 'observed_available':
        blockers.extend(desktop.get('blockers') or ['desktop_unavailable'])
    targets = [str(exe), game, str(root / 'python/python.exe'), str(root / 'python/pythonw.exe')]
    # This game's installed launcher can reopen after the game exits. Observe
    # it without launching or adopting it, including when it escapes the Job.
    official_launcher = Path(game).parent.parent / 'PCLauncher.exe'
    launcher_path = None
    if official_launcher.is_file():
        if official_launcher.is_symlink() or official_launcher.resolve().parent != Path(game).parent.parent.resolve():
            raise ValueError('external GF2 launcher entry')
        launcher_path = str(official_launcher.resolve())
        targets.append(launcher_path)
    observed = find_matching_processes(targets)
    if observed['status'] != 'observed' or observed['matches']:
        blockers.append('existing_runner_or_game_not_exclusive')
    metadata = json.loads((root / 'interface.json').read_text(encoding='utf-8-sig'))
    controllers = [c for c in metadata.get('controller', []) if c.get('type') == 'Win32'
                   and c.get('name') == instance.get('CurrentControllerName', '桌面端')]
    if len(controllers) != 1:
        raise ValueError('saved desktop controller window specification unavailable')
    window_filter = controllers[0].get('win32', {})
    for key in ('window_regex', 'class_regex'):
        if not isinstance(window_filter.get(key), str) or not window_filter[key]:
            raise ValueError('saved desktop controller window specification unavailable')
        re.compile(window_filter[key])
    return {'installation_root': str(root), 'cwd': str(root), 'launcher': str(exe),
            'argv_candidate': [str(exe), '--instance', files[0].stem],
            'game_path': game, 'observation_targets': targets, 'focus_targets': [game],
            'official_launcher': launcher_path,
            'native_startup': True, 'command_pipe_key': instance_key(exe, core),
            'command_protocol_sha256': core_digest,
            'native_startup_timeout': 120,
            'native_startup_mode': 'global' if native_global else 'command',
            'startup_window_filter': {k: window_filter[k] for k in ('window_regex', 'class_regex')},
            'runner_version': metadata.get('version'), 'completion_kind': 'maa-gf2',
            'instance_id': files[0].stem, 'instance_name': instance.get('InstanceName'),
            'blockers': blockers, 'game_start': 'assistant', 'borrowed_game': [],
            'task_configuration': 'assistant_current_saved_preset'}


def log_paths(root):
    root = Path(root)
    return maaend.log_paths(root) + sorted((root / 'logs').glob('log-*.log'))


@dataclass(frozen=True)
class Cursor(maaend.Cursor):
    command: dict = field(default_factory=dict)
    startup_mode: str = 'command'
    audit_path: str | None = None
    corruption: dict = field(default_factory=dict)


def bind_execution(current, directory):
    baseline = {}
    for path in log_paths(current['cwd']):
        if path.is_symlink() or not path.resolve().is_relative_to(Path(current['cwd']).resolve()):
            raise ValueError('external MaaGF2 log')
        with path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            content = stream.read(stat.st_size)
        baseline[(stat.st_dev, stat.st_ino)] = (len(content), hashlib.sha256(content).hexdigest(),
                                             not content or content.endswith(b'\n'))
    cursor = Cursor(current['cwd'], current['instance_id'], time.time_ns(), baseline,
                    startup_mode=current.get('native_startup_mode', 'command'),
                    audit_path=str(Path(directory) / 'native-log-corruption.json'))
    save(Path(directory) / 'native-log-baseline.json', {
        'instance_id': cursor.instance, 'captured_at_ns': cursor.captured_at_ns,
        'files': [{'identity': list(identity), 'offset': value[0], 'prefix_sha256': value[1],
                   'at_line_boundary': value[2]} for identity, value in baseline.items()]})
    return current, cursor


GUI = re.compile(r'^\[(?P<time>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]\[\w+\] (?P<message>.*)$')


def evaluate(frames, gui, code, pid, instance, command_verified=False, native_global=False, owned_stop=False):
    starts, statuses, entries = set(), {}, {}
    initialized = False
    for line in frames:
        event = maaend.FRAME.fullmatch(line)
        if not event:
            continue
        if int(event['pid']) != pid:
            raise ValueError('MaaGF2 callback writer is not owned root')
        initialized |= '[Logger] MAA Process Start' in line
        callback = maaend.EVENT.search(line)
        if not callback:
            continue
        data = json.loads(callback[2])
        if data.get('entry') == 'MaaTaskerPostStop':
            continue  # cancellation acknowledgements cannot prove task completion
        identity = data.get('task_id')
        if type(identity) is not int or identity <= 0:
            raise ValueError('invalid MaaGF2 task identity')
        if callback[1] == 'Starting':
            if identity in starts:
                raise ValueError('duplicate MaaGF2 task start')
            starts.add(identity)
        else:
            if identity in statuses or identity not in starts:
                raise ValueError('duplicate or unstarted MaaGF2 terminal callback')
            statuses[identity] = callback[1]
            entries[identity] = data.get('entry', str(identity))
    expected, selected_starts = [], []
    finished = abandoned = failed = closed = False
    launch = command_verified
    global_starts = 0
    stop_state = None
    for line in gui:
        event = GUI.fullmatch(line)
        if not event:
            continue
        message = event['message']
        if message == '[cfg=Default] 全局启动完成：已启动 1 个，跳过 0 个，失败 0 个':
            global_starts += 1
        if '启动参数：' in message and all(x in message for x in ('autostart', 'quit-after-run', instance)):
            launch = True
        closed |= message == '[cfg=Default] 应用已关闭'
        scoped = re.search(r'\[inst=[^\]]*/' + re.escape(instance) + r'\]', message)
        if not scoped:
            continue
        count = re.search(r'准备执行任务队列：任务数量=(\d+)$', message)
        if count:
            expected.append(int(count[1]))
            finished = False  # startup-only completion cannot finish business tasks
            stop_state = None
        stopped = re.search(r'\[src=Worker\]\[op=StopTask\] 停止前状态：(SUCCEEDED|FAILED|STOPPED|STOPPING)$', message)
        if stopped and expected:
            stop_state = stopped[1]
        task = re.search(r'\[op=MonitorLog\] 开始任务：(.*)$', message)
        if task:
            selected_starts.append(task[1])
        if '[op=MonitorLog] ' in message:
            text = message.split('[op=MonitorLog] ', 1)[1]
            finished |= bool(expected) and text.startswith('任务已全部完成')
            abandoned |= text == '已放弃本次任务'
            failed |= text == '任务失败' or text.startswith('任务失败：')
    failures = [entries[i] for i, state in statuses.items() if state == 'Failed']
    launch |= native_global and global_starts == 1
    # QuitAfterRun closes on IsRunning=false before the asynchronous completion
    # toast can be logged. Still require the entire queue and every callback.
    verified_quit_stop = command_verified and closed and code == 0 and stop_state in {'SUCCEEDED', 'FAILED'}
    queue_verified = (initialized and launch and not abandoned and len(expected) == 1
                and expected[0] > 0 and len(selected_starts) == expected[0]
                and bool(starts) and starts == set(statuses) and (finished or failed or verified_quit_stop)
                and stop_state not in {'STOPPED', 'STOPPING'})
    status = 'completion_unconfirmed'
    if initialized and code is not None and code != 0:
        status = 'failed'
    elif queue_verified and ((closed and code == 0) or (owned_stop and code is None)):
        status = 'partial_failure' if failures or failed or stop_state == 'FAILED' else 'success'
    return {'status': status, 'exit_code': code, 'writer_verified': initialized,
            'instance_id': instance, 'queue_task_counts': expected, 'started_applications': selected_starts,
            'task_statuses': {str(k): v for k, v in statuses.items()}, 'failed_applications': failures,
            'native_queue_completed': finished, 'native_queue_failed': failed, 'native_quit_seen': closed,
            'task_queue_verified': queue_verified, 'native_global_started': native_global and global_starts == 1,
            'native_stop_state': stop_state, 'quit_after_run_terminal_verified': verified_quit_stop,
            'supervisor_closed_after_verified_completion': owned_stop}


BYTE_FRAME = re.compile(rb'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\]\[\w+\]\[Px(\d+)\]')


def noncritical_agent_line(raw, root_pid, agents, lower):
    """Only discard a damaged, owned Agent node event, never task evidence.

    Shared writes can splice a root AgentClient trace into an Agent node line.
    Every visible frame must still have a known writer and a fresh timestamp.
    Unattributed fragments and damaged GUI/root callbacks stay blocking.
    """
    headers = list(BYTE_FRAME.finditer(raw))
    if (not headers or headers[0].start() != 0 or b'Tasker.Task.' in raw
            or b'MAA Process Start' in raw):
        return False
    for index, header in enumerate(headers):
        pid = int(header[2])
        stamp = datetime.strptime(header[1].decode('ascii'), '%Y-%m-%d %H:%M:%S.%f').timestamp()
        if stamp < lower:
            return False
        chunk = raw[header.start():headers[index + 1].start() if index + 1 < len(headers) else len(raw)]
        if pid == root_pid:
            if (index == 0 or b'[TRC]' not in chunk or b'AgentClient::handle_event_response' not in chunk
                    or b'[msg=' in chunk or b'[Logger]' in chunk):
                return False
        else:
            agent = agents.get(pid)
            if (agent is None or stamp < agent['creation_ticks'] / 1e7 - 11644473600 - 1
                    or not re.search(rb'\[msg=Node\.PipelineNode\.(Starting|Succeeded|Failed)\]', chunk)):
                return False
    return True


def persist_corruption(cursor):
    if not cursor.audit_path:
        raise ValueError('MaaGF2 damaged log requires an audit destination')
    save(Path(cursor.audit_path), {'schema_version': 1, 'lines': list(cursor.corruption.values())})


def record_corruption(cursor, path, identity, offset, raw, exc, skipped):
    digest = hashlib.sha256(raw).hexdigest()
    key = (str(path), identity, offset, digest)
    record = {'path': str(path), 'identity': list(identity), 'byte_offset': offset,
              'error_offset': exc.start, 'length': len(raw), 'sha256': digest,
              'raw_hex': raw[:4096].hex(), 'raw_hex_truncated': len(raw) > 4096,
              'skipped': skipped}
    if cursor.corruption.get(key) != record:
        if key not in cursor.corruption and len(cursor.corruption) >= 64:
            raise ValueError('too many damaged MaaGF2 log lines')
        cursor.corruption[key] = record
    # A persisted audit is required before a malformed line can be discarded.
    # Retry failed writes on later polls rather than accepting in-memory proof.
    persist_corruption(cursor)


def read_events(cursor, root_pid, owned):
    try:
        if not any(r.get('pid') == root_pid and r.get('in_owned_job') is True for r in owned):
            raise ValueError('MaaGF2 root ownership not observed')
        agents = {r['pid']: r for r in owned if r.get('in_owned_job') is True and
                  str(r.get('image', '')).casefold() in
                  {str(Path(cursor.root) / 'python' / p).casefold() for p in ('python.exe', 'pythonw.exe')}}
        frames, gui = [], []
        lower = cursor.captured_at_ns / 1e9 - 1
        for path in log_paths(cursor.root):
            if path.is_symlink() or not path.resolve().is_relative_to(Path(cursor.root).resolve()):
                raise ValueError('external MaaGF2 log')
            with path.open('rb') as stream:
                stat = os.fstat(stream.fileno())
                content = stream.read(stat.st_size)
            old = cursor.baseline.get((stat.st_dev, stat.st_ino))
            matched = old and len(content) >= old[0] and hashlib.sha256(content[:old[0]]).hexdigest() == old[1]
            if not matched and path.name.startswith('maafw.bak.'):
                # MaaFramework copies the active log to a new archive identity
                # and truncates the original. Authenticate the copied prefix,
                # never skip old timestamps merely because a file is an archive.
                candidates = [value for value in cursor.baseline.values() if value[0] > 0
                              and len(content) >= value[0]
                              and hashlib.sha256(content[:value[0]]).hexdigest() == value[1]]
                old = max(candidates, key=lambda value: value[0], default=None)
                matched = old is not None
            skipped_bytes = 0
            if matched:
                content = content[old[0]:]
                skipped_bytes = old[0]
                if not old[2]:
                    head, separator, content = content.partition(b'\n')
                    skipped_bytes += len(head) + len(separator)
            content = content[:content.rfind(b'\n') + 1]
            native = path.name.startswith('maafw')
            offset = skipped_bytes
            for raw in content.splitlines(keepends=True):
                line_offset = offset
                offset += len(raw)
                try:
                    line = raw.decode('utf-8', errors='strict').rstrip('\r\n')
                except UnicodeDecodeError as exc:
                    safe = native and noncritical_agent_line(raw, root_pid, agents, lower)
                    record_corruption(cursor, path, (stat.st_dev, stat.st_ino), line_offset, raw, exc, safe)
                    if not safe:
                        raise ValueError('damaged MaaGF2 log may contain critical or unknown evidence') from exc
                    continue
                event = (maaend.FRAME if native else GUI).fullmatch(line)
                if not event:
                    continue
                stamp = datetime.strptime(event['time'], '%Y-%m-%d %H:%M:%S.%f').timestamp()
                if stamp < lower:
                    raise ValueError('MaaGF2 log contains old events after replacement')
                if native and int(event['pid']) != root_pid:
                    agent = agents.get(int(event['pid']))
                    if agent is None or stamp < agent['creation_ticks'] / 1e7 - 11644473600 - 1:
                        raise ValueError('unknown or stale MaaGF2 framework writer')
                    continue
                (frames if native else gui).append(line)
        if cursor.corruption:
            persist_corruption(cursor)
            if any(not row['skipped'] for row in cursor.corruption.values()):
                raise ValueError('previous damaged MaaGF2 evidence remains unconfirmed')
        return frames, gui
    except (OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
        raise ValueError(str(exc)) from exc


def bootstrap_ready(frames, gui, pid, instance, hwnd):
    if not any('[Logger] MAA Process Start' in line and
               (match := maaend.FRAME.fullmatch(line)) and int(match['pid']) == pid for line in frames):
        return False
    connected = False
    finished = False
    selected = None
    for line in gui:
        match = GUI.fullmatch(line)
        if not match:
            continue
        text = match['message']
        handle = re.search(r'窗口句柄：(\d+)$', text)
        if handle:
            selected = int(handle[1])
        if '连接状态变更：未连接。' in text:
            connected = False
        if re.search(r'启动后连接在第 \d+ 次尝试时成功，继续执行前等待 5 秒。$', text):
            connected = selected == hwnd and bool(hwnd)
        if re.search(r'\[inst=[^\]]*/' + re.escape(instance) + r'\]', text):
            if '准备执行任务队列：' in text:
                raise ValueError('native business queue started before command; stop without retry')
            if '[op=MonitorLog] 任务失败' in text or '[op=MonitorLog] 已放弃本次任务' in text:
                raise ValueError('native startup failed or cancelled')
            if connected and '[op=MonitorLog] 任务已全部完成' in text:
                finished = True
    return finished and connected and selected == hwnd


def advance_startup(current, cursor, process, owned, directory, elapsed, stop_requested=None):
    if cursor.startup_mode == 'global':
        result = observe(cursor, None, process.pid, owned)
        if result.get('native_global_started'):
            cursor.command.update(status='native_auto_started', requests=0, instance_id=cursor.instance)
            return dict(cursor.command, task_queue_verified=result.get('task_queue_verified', False))
        if elapsed >= current['native_startup_timeout']:
            raise ValueError('native global startup did not start saved instance within deadline')
        return {'status': 'waiting_for_native_auto_start'}
    if cursor.command:
        if cursor.command.get('status') == 'acknowledged' and not cursor.command.get('queue_seen'):
            try:
                _, gui = read_events(cursor, process.pid, owned)
            except ValueError as exc:
                if ('unknown or stale MaaGF2 framework writer' in str(exc)
                        and time.monotonic() - cursor.command['acknowledged_monotonic'] < 30):
                    return cursor.command
                raise
            scoped = re.compile(r'\[inst=[^\]]*/' + re.escape(cursor.instance) + r'\].*准备执行任务队列：任务数量=\d+$')
            if any(scoped.search(line) for line in gui):
                cursor.command['queue_seen'] = True
            elif time.monotonic() - cursor.command['acknowledged_monotonic'] >= 30:
                raise ValueError('native command acknowledged but task queue never started; no retry')
        return cursor.command
    if elapsed >= current['native_startup_timeout']:
        raise ValueError('native startup connection timeout; daily tasks not commanded')
    games = [r for r in owned if r.get('in_owned_job') is True and
             os.path.normcase(r['image']) == os.path.normcase(current['game_path'])]
    window = request_foreground(games, [current['game_path']], window_filter=current['startup_window_filter'])
    if window['status'] not in {'foreground', 'foreground_denied'}:
        if window['status'] != 'waiting_for_window':
            raise ValueError('native startup window identity uncertain')
        return {'status': 'waiting_for_native_connection'}
    try:
        frames, gui = read_events(cursor, process.pid, owned)
    except ValueError as exc:
        if 'unknown or stale MaaGF2 framework writer' not in str(exc):
            raise
        # An Agent can appear between this tick's process and log snapshots.
        # No command until a later tick has verified every writer's ownership.
        return {'status': 'waiting_for_native_writer_ownership'}
    if not bootstrap_ready(frames, gui, process.pid, cursor.instance, window['hwnd']):
        return {'status': 'waiting_for_native_connection', 'window': window}
    root = Path(current['cwd'])
    if instance_key(root / 'MaaGF2Exilium.exe', root / 'libs/MFAAvalonia.Core.dll') != current['command_pipe_key']:
        raise ValueError('native command protocol changed during startup')
    identity = next(r for r in owned if r['pid'] == process.pid and r.get('in_owned_job') is True)
    cursor.command.update(status='attempted', root_pid=process.pid, creation_ticks=identity['creation_ticks'],
                          instance_id=cursor.instance, requests=1, window=window)
    path = Path(directory) / 'native-start-command.json'
    save(path, cursor.command)  # durable intent before any command; never resend
    cursor.command.update(send_start(process, current['command_pipe_key'], cursor.instance, stop_requested=stop_requested))
    cursor.command['acknowledged_monotonic'] = time.monotonic()
    save(path, cursor.command)
    return cursor.command


def observe(cursor, exit_code, root_pid, owned):
    try:
        frames, gui = read_events(cursor, root_pid, owned)
        command = getattr(cursor, 'command', {})
        verified = (command.get('status') == 'acknowledged' and command.get('root_pid') == root_pid
                    and command.get('pipe_server_pid') == root_pid and command.get('instance_id') == cursor.instance
                    and command.get('requests') == 1 and command.get('force_start') is False
                    and any(r['pid'] == root_pid and r.get('creation_ticks') == command.get('creation_ticks')
                            and r.get('in_owned_job') is True for r in owned))
        result = evaluate(frames, gui, exit_code, root_pid, cursor.instance, command_verified=verified,
                        native_global=cursor.startup_mode == 'global', owned_stop=command.get('owned_stop') is True)
    except (OSError, ValueError, UnicodeError, TypeError, KeyError) as exc:
        result = {'status': 'completion_unconfirmed', 'reason': str(exc), 'exit_code': exit_code}
    if cursor.corruption:
        result['log_corruption'] = {
            'skipped_lines': sum(row['skipped'] for row in cursor.corruption.values()),
            'blocked_lines': sum(not row['skipped'] for row in cursor.corruption.values()),
            'audit_path': cursor.audit_path}
    return result
