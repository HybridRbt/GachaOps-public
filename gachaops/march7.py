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
NULL_SCALARS = frozenset({'~', 'null', 'Null', 'NULL'})
TRUE_SCALARS = frozenset({'true', 'True', 'TRUE'})
FALSE_SCALARS = frozenset({'false', 'False', 'FALSE'})
# YAML 1.2 double-quoted escape table, verified against ruamel.yaml 0.19.1
# with the exact loader upstream uses (module/config/config.py: YAML()).
DOUBLE_ESCAPES = {'0': '\0', 'a': '\a', 'b': '\b', 't': '\t', 'n': '\n', 'v': '\v',
                  'f': '\f', 'r': '\r', 'e': '\x1b', ' ': ' ', '"': '"', '/': '/',
                  '\\': '\\', 'N': '\x85', '_': '\xa0', 'L': '\u2028', 'P': '\u2029'}


class March7ConfigUnreadable(ValueError):
    """A needed config entry appears but its value cannot be determined.

    Carries the key name (None for file-structure problems) so the user is
    told which setting to re-save in the assistant.
    """

    def __init__(self, key):
        super().__init__('unrecognized March7 config entry: ' + str(key))
        self.key = key


def _advance_scan(line, state):
    """Track quotes/flow depth/comments across opaque lines so lines inside a
    multi-line flow or quoted scalar are never mistaken for mapping keys."""
    in_double, in_single, depth = state
    index = 0
    while index < len(line):
        char = line[index]
        if in_double:
            if char == '\\':
                index += 2
                continue
            if char == '"':
                in_double = False
            index += 1
            continue
        if in_single:
            if char == "'":
                if index + 1 < len(line) and line[index + 1] == "'":
                    index += 2
                    continue
                in_single = False
            index += 1
            continue
        if char == '#' and (index == 0 or line[index - 1] in ' \t'):
            break
        if char == '"':
            in_double = True
        elif char == "'":
            in_single = True
        elif char in '[{':
            depth += 1
        elif char in ']}':
            depth = max(0, depth - 1)
        index += 1
    return [in_double, in_single, depth]


def _hex_unit(text, index, digits, key):
    if index + digits > len(text):
        raise March7ConfigUnreadable(key)
    chunk = text[index:index + digits]
    if not all(c in '0123456789abcdefABCDEF' for c in chunk):
        raise March7ConfigUnreadable(key)
    return chr(int(chunk, 16)), index + digits


def _double_quoted(key, text):
    result = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == '\\':
            escape = text[index + 1] if index + 1 < len(text) else ''
            if escape in DOUBLE_ESCAPES:
                result.append(DOUBLE_ESCAPES[escape])
                index += 2
            elif escape == 'x':
                unit, index = _hex_unit(text, index + 2, 2, key)
                result.append(unit)
            elif escape == 'u':
                unit, index = _hex_unit(text, index + 2, 4, key)
                result.append(unit)
            elif escape == 'U':
                unit, index = _hex_unit(text, index + 2, 8, key)
                result.append(unit)
            else:
                raise March7ConfigUnreadable(key)
            continue
        if char == '"':
            remainder = text[index + 1:].strip()
            if remainder and not remainder.startswith('#'):
                raise March7ConfigUnreadable(key)
            return ''.join(result)
        result.append(char)
        index += 1
    raise March7ConfigUnreadable(key)


def _single_quoted(key, text):
    result = []
    index = 1
    while index < len(text):
        char = text[index]
        if char == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                result.append("'")
                index += 2
                continue
            remainder = text[index + 1:].strip()
            if remainder and not remainder.startswith('#'):
                raise March7ConfigUnreadable(key)
            return ''.join(result)
        result.append(char)
        index += 1
    raise March7ConfigUnreadable(key)


def _scalar(text):
    """YAML 1.2 core resolution for the forms a lifecycle value can take.
    Unresolved forms keep their literal text; zero-valued numbers are the
    only numeric distinction kept, because the guards reason by truthiness
    exactly like upstream (`if cfg.<key>:` / `get_value(...) or`)."""
    if text in NULL_SCALARS:
        return None
    if text in TRUE_SCALARS:
        return True
    if text in FALSE_SCALARS:
        return False
    if re.fullmatch(r'[-+]?(0[xX][0-9A-Fa-f]+|0o[0-7]+|[0-9]+)', text):
        try:
            return int(text, 0)
        except ValueError:
            return int(text, 10)
    if re.fullmatch(r'[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?', text):
        try:
            return float(text)
        except ValueError:
            return text
    return text


def _split_entry(stripped):
    """Split one top-level line into (key, rest-after-colon).

    Returns (None, None) for lines that are not mapping entries. A needed key
    spelled in an unusable colon form raises instead of disappearing."""
    care = REQUIRED_KEYS | PLATFORM_KEYS
    if stripped[0] in '"\'':
        quote = stripped[0]
        index = 1
        result = []
        closed = False
        while index < len(stripped):
            char = stripped[index]
            if quote == '"' and char == '\\':
                result.append(stripped[index + 1] if index + 1 < len(stripped) else '')
                index += 2
                continue
            if char == quote:
                if quote == "'" and index + 1 < len(stripped) and stripped[index + 1] == "'":
                    result.append("'")
                    index += 2
                    continue
                closed = True
                break
            result.append(char)
            index += 1
        if not closed:
            return None, None
        key = ''.join(result)
        after = stripped[index + 1:]
        colon = after.find(':')
        if colon == -1 or (colon + 1 < len(after) and after[colon + 1] not in ' \t'):
            if key in REQUIRED_KEYS | PLATFORM_KEYS:
                raise March7ConfigUnreadable(key)
            return None, None
        return key, after[colon + 1:]
    position = stripped.find(':')
    while position != -1:
        if position + 1 == len(stripped) or stripped[position + 1] in ' \t':
            break
        position = stripped.find(':', position + 1)
    if position == -1:
        first = re.match(r'[A-Za-z0-9_]+', stripped)
        if first and first[0] in care:
            raise March7ConfigUnreadable(first[0])
        return None, None
    key = stripped[:position].rstrip()
    rest = stripped[position + 1:]
    if key in care and rest and rest[0] not in ' \t':
        raise March7ConfigUnreadable(key)
    return key, rest


def _peek_null(key, lines, index, top_indent):
    """Resolve `key:` with nothing after the colon on its own line: the value
    is null unless a nested block or a same-indent sequence follows."""
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            index += 1
            continue
        indent = len(line) - len(line.lstrip(' '))
        if indent > top_indent or (indent == top_indent and stripped.startswith('-')):
            raise March7ConfigUnreadable(key)
        return None, index
    return None, index


def _parse_value(key, rest, lines, index, top_indent):
    value_part = rest.strip()
    if not value_part or value_part.startswith('#'):
        return _peek_null(key, lines, index, top_indent)
    first = value_part[0]
    if first in '[{':
        raise March7ConfigUnreadable(key)
    if first in '|>':
        raise March7ConfigUnreadable(key)
    if first in '&*!':
        raise March7ConfigUnreadable(key)
    if first == '"':
        return _double_quoted(key, value_part), index
    if first == "'":
        return _single_quoted(key, value_part), index
    match = re.search(r'\s#', value_part)
    if match:
        value_part = value_part[:match.start()]
    value_part = value_part.rstrip(' \t')
    if not value_part:
        return _peek_null(key, lines, index, top_indent)
    return _scalar(value_part), index


def launch_metadata(path):
    """Read the top-level lifecycle scalars from the assistant's config.

    Upstream loads this YAML 1.2 file with ruamel.yaml. We stay
    dependency-free but fail closed: a needed key that appears in any form
    this reader cannot resolve exactly raises March7ConfigUnreadable instead
    of being skipped. Quoted-key spellings, uniform top-level indentation,
    spaced colons, trailing comments and CRLF are resolved; lists, nested
    blocks, block scalars, anchors/aliases, tags and multi-line values are
    not, and values keep their YAML types.
    """
    lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
    values = {}
    seen = set()
    top_indent = None
    content_seen = False
    state = [False, False, 0]
    block_indent = None
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if block_indent is not None:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            if len(line) - len(line.lstrip(' ')) > block_indent:
                continue
            block_indent = None
        if state != [False, False, 0]:
            state = _advance_scan(line, state)
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = len(line) - len(line.lstrip(' '))
        if '\t' in line[:indent]:
            continue
        if stripped in ('---', '...'):
            if content_seen:
                raise March7ConfigUnreadable(None)
            continue
        content_seen = True
        if top_indent is None:
            top_indent = indent
        elif indent < top_indent:
            raise March7ConfigUnreadable(None)
        if indent > top_indent or stripped == '-' or stripped.startswith('- '):
            state = _advance_scan(line, state)
            continue
        key, rest = _split_entry(stripped)
        if key is None:
            state = _advance_scan(line, state)
            continue
        if key in seen:
            raise ValueError('duplicate March7 launch metadata: ' + key)
        seen.add(key)
        if key in REQUIRED_KEYS | PLATFORM_KEYS:
            values[key], index = _parse_value(key, rest, lines, index, top_indent)
        else:
            # Opaque key: a block scalar's literal content must never be
            # mistaken for mapping structure; quotes/flows still tracked.
            match = re.match(r'[|>][+-]?[0-9]*([+-][0-9]*)?\s*(#.*)?$', rest.strip())
            if match:
                block_indent = indent
            else:
                state = _advance_scan(line, state)
    if state != [False, False, 0] or block_indent is not None:
        raise March7ConfigUnreadable(None)
    if not REQUIRED_KEYS.issubset(values):
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
    blockers = []
    meta = None
    unreadable_key = None
    try:
        meta = launch_metadata(config)
    except March7ConfigUnreadable as unreadable:
        # A needed entry exists but its spelling cannot be resolved. Block on
        # a code like every other adapter so blocker lists stay codes; the
        # offending key travels in its own field for the desktop to name it.
        # Re-saving in the assistant rewrites the file in the canonical form
        # upstream itself writes.
        blockers.append('march7_config_entry_unreadable' if unreadable.key
                        else 'march7_config_structure_unreadable')
        unreadable_key = unreadable.key
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
        if not isinstance(game, str) or not Path(game).is_absolute() or not Path(game).is_file():
            blockers.append('game_path_unverified')
        targets = [str(exe), str(launcher), str(root / 'March7th Updater.exe'), game]
    else:
        # The config could not be read; the named-setting blocker above is the
        # actionable message, and the game path is simply unknown.
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
            'game_path': meta.get('game_path') if meta else None,
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
