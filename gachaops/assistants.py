"""Ordered external assistants. Native task presets remain opaque and untouched."""
import ast
import json
import math
import os
from pathlib import Path
import uuid

from .planner import ConfigError
from .preflight import desktop_report, runtime_desktop_report
from .windows_observation import elevation_report, find_matching_processes, ProcessObservationError
from .ok_completion import capture_cursor
from .ok_trial import run_prepared
from .recovery import ExecutionSession, save
from . import onedragon, nte_completion, maaend, march7, maagf2, bettergi

from .adapter_catalog import list_adapters, get_adapter

IMPLEMENTED_ADAPTERS = frozenset(s.adapter_id for s in list_adapters())
PENDING_ADAPTERS = set()


def disabled_entry(row):
    return {'id': row['id'], 'status': 'disabled',
            'integration_status': 'implemented' if row['adapter'] in IMPLEMENTED_ADAPTERS else 'pending',
            'installation_root': row.get('installation_root'),
            'entry_executable': row.get('entry_executable')}


def load_assistants(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    return validate_assistants(data)


def validate_assistants(data):
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('assistants'), list):
        raise ConfigError('expected schema_version 1 and ordered assistants list')
    if set(data) - {'schema_version', 'assistants', 'repeat_limit'}:
        raise ConfigError('unknown configuration field')
    repeat_limit(data)
    seen = set()
    for row in data['assistants']:
        if not isinstance(row, dict) or set(row) - {'id', 'adapter', 'installation_root', 'enabled', 'timeout_seconds', 'game_start', 'game_path', 'entry_executable'}:
            raise ConfigError('unknown assistant registration field')
        identity = row.get('id')
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ConfigError('assistant id must be unique and nonempty')
        seen.add(identity)
        if row.get('adapter') not in IMPLEMENTED_ADAPTERS | PENDING_ADAPTERS:
            raise ConfigError(f'{identity}: adapter not yet implemented')
        pending = row['adapter'] in PENDING_ADAPTERS
        if row['adapter'] == 'zzz-onedragon':
            game = row.get('game_path')
            if not (row.get('enabled') is False and game is None) and (not isinstance(game, str) or not Path(game).is_absolute()):
                raise ConfigError(f'{identity}: absolute game_path required for ZZZ process observation')
            if row.get('game_start', 'assistant') != 'assistant':
                raise ConfigError(f'{identity}: ZZZ currently supports assistant game start only')
        elif row['adapter'] in {'ok-ww', 'ok-nte'} and 'game_path' in row:
            raise ConfigError(f'{identity}: OK game path comes from its saved device configuration')
        if row['adapter'] in {'ok-nte', 'march7', 'maa-gf2', 'bettergi'} and row.get('game_start', 'assistant') != 'assistant':
            raise ConfigError(f'{identity}: adapter currently supports assistant game start only')
        if row['adapter'] in {'maa-end', 'march7', 'maa-gf2', 'bettergi'} and 'game_path' in row:
            raise ConfigError(f'{identity}: game path comes from its saved launch metadata')
        root = row.get('installation_root')
        unknown_disabled_root = row.get('enabled') is False and root is None
        if not unknown_disabled_root and (not isinstance(root, str) or not root or not Path(root).is_absolute()):
            raise ConfigError(f'{identity}: absolute installation_root required')
        if 'entry_executable' in row:
            entry = row['entry_executable']
            if (not pending and row['adapter'] not in {'ok-nte', 'maa-end', 'march7', 'maa-gf2', 'bettergi'}) or (entry is not None and (not isinstance(entry, str) or not Path(entry).is_absolute())):
                raise ConfigError(f'{identity}: entry_executable is absolute installation metadata')
            if entry is not None and (root is None or not Path(entry).resolve().is_relative_to(Path(root).resolve())):
                raise ConfigError(f'{identity}: entry_executable must be inside installation_root')
        if pending and 'game_path' in row:
            game = row['game_path']
            if not isinstance(game, str) or not Path(game).is_absolute():
                raise ConfigError(f'{identity}: absolute game_path required when provided')
        if type(row.get('enabled', True)) is not bool:
            raise ConfigError(f'{identity}: enabled must be boolean')
        if row.get('game_start', 'assistant') not in {'assistant', 'manual'}:
            raise ConfigError(f'{identity}: game_start must be assistant or manual')
        timeout = row.get('timeout_seconds', 1800)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError(f'{identity}: timeout_seconds must be positive and finite')
    return data


def prepare_assistant(row):
    """Read external entry/session information, never read native daily presets."""
    from .compatibility import capture_installation_stamp
    return capture_installation_stamp(get_adapter(row.get('adapter', 'ok-ww')).prepare(row))


def _prepare_ok_assistant(row):
    root = Path(row['installation_root']).resolve(strict=True)
    adapter = row.get('adapter', 'ok-ww')
    app = root / 'data/apps' / adapter
    working = app / 'working'
    runtime, main = app / 'python/pythonw.exe', working / 'main.py'
    config_path = working / ('src/config.py' if adapter == 'ok-nte' else 'config.py')
    binding = (['src.tasks.daily.DailyRoutineTask', 'DailyRoutineTask'] if adapter == 'ok-nte'
               else ['src.task.DailyTask', 'DailyTask'])
    for path in (runtime, main, config_path):
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('missing or external installation entry')
    metadata = json.loads((app / 'app.json').read_text(encoding='utf-8-sig'))
    profile = next((p for p in metadata.get('profiles', []) if p.get('name') == metadata.get('current_profile')), {})
    tree = ast.parse(config_path.read_text(encoding='utf-8-sig'))
    tasks = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'onetime_tasks':
                    tasks = ast.literal_eval(value)
    if not isinstance(tasks, list) or tasks.count(binding) != 1:
        raise ValueError(f'{adapter} saved daily entry unavailable or ambiguous; integration must be checked')
    # NTE's installed CLI accepts a one-based integer, not a class name.
    selector = str(tasks.index(binding) + 1) if adapter == 'ok-nte' else 'DailyTask'
    device = json.loads((working / 'configs/devices.json').read_text(encoding='utf-8-sig'))
    game = device.get('pc_full_path')
    blockers = []
    if not isinstance(game, str) or not Path(game).is_absolute() or not Path(game).is_file():
        blockers.append('game_path_unverified')
    if profile.get('admin') is not True:
        blockers.append('admin_requirement_unknown')
    if elevation_report()['elevated'] is not True:
        blockers.append('supervisor_elevation_required')
    desktop = desktop_report()
    if desktop['status'] != 'observed_available':
        blockers.extend(desktop['blockers'] or ['desktop_unavailable'])
    additional_roots = []
    official_launcher = None
    if adapter == 'ok-nte' and row.get('game_start', 'assistant') != 'manual':
        launcher_config = json.loads((working / 'configs/LauncherTask.json').read_text(encoding='utf-8-sig'))
        official_launcher = launcher_config.get('Launcher Path')
        if (not isinstance(official_launcher, str) or not Path(official_launcher).is_absolute()
                or not Path(official_launcher).is_file()):
            blockers.append('official_launcher_path_unverified')
            official_launcher = None
        else:
            official_launcher = str(Path(official_launcher).resolve(strict=True))
            additional_roots = [[official_launcher]]
    targets = [str(runtime), str(root / (adapter + '.exe')), game, official_launcher]
    observed = find_matching_processes(targets)
    borrowed = []
    manual = row.get('game_start', 'assistant') == 'manual'
    if observed['status'] != 'observed':
        blockers.append('existing_runner_or_game_not_exclusive')
    elif manual:
        borrowed = [r for r in observed['matches'] if os.path.normcase(r['image']) == os.path.normcase(game)]
        runners = [r for r in observed['matches'] if r not in borrowed]
        if runners:
            blockers.append('existing_runner_not_exclusive')
        if len(borrowed) != 1:
            blockers.append('manual_game_requires_exactly_one_running_instance')
        options = json.loads((working / 'configs/Basic Options.json').read_text(encoding='utf-8-sig'))
        if options.get('Auto Start Game When App Starts') is not False:
            blockers.append('disable_auto_start_game_in_assistant_gui')
    elif observed['matches']:
        blockers.append('existing_runner_or_game_not_exclusive')
    return {'installation_root': str(root), 'cwd': str(working),
            'argv_candidate': [str(runtime), str(main), '-t', selector, '-e'],
            'completion_kind': adapter,
            'additional_roots': additional_roots, 'observation_targets': targets,
            'focus_targets': [game, official_launcher],
            'launcher': str(root / (adapter + '.exe')), 'game_path': game, 'blockers': blockers,
            'runner_version': metadata.get('current_version'),
            'game_start': row.get('game_start', 'assistant'), 'borrowed_game': borrowed,
            'task_configuration': 'assistant_current_saved_preset'}


def check_assistants(data):
    rows = []
    for row in data['assistants']:
        if not row.get('enabled', True):
            rows.append(disabled_entry(row))
            continue
        try:
            current = prepare_assistant(row)
            rows.append(dict(current, id=row['id'], status='blocked' if current['blockers'] else 'ready'))
        except (OSError, ValueError, SyntaxError, KeyError, TypeError, StopIteration) as exc:
            rows.append({'id': row['id'], 'status': 'blocked', 'error': str(exc)})
    return {'schema_version': 1, 'mode': 'check_only', 'game_execution': False, 'assistants': rows}


def windows_available():
    return os.name == 'nt'


def repeat_limit(data):
    limit = data.get('repeat_limit', 0)
    if type(limit) is not int or limit < 0:
        raise ConfigError('repeat_limit must be a nonnegative integer (additional rounds)')
    return limit


def run_assistants(data, output, state, progress=print, stop_requested=None):
    if not windows_available():
        raise OSError('Windows required for real assistants')
    limit = repeat_limit(data)
    directory = Path(output).resolve() / ('assistants-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    report = {'schema_version': 1, 'mode': 'assistants', 'status': 'running', 'assistants': [],
              'repeat_limit': limit, 'current_round': 1, 'rounds': [], 'repeat_limit_reached': False}
    with ExecutionSession(state, 'configured_assistants') as session:
        report['run_id'] = session.record['run_id']
        session.register_chain(directory / 'summary.json')
        save(directory / 'summary.json', report)
        try:
            rows = list(data['assistants'])
            latest, history = {}, {}
            pending = list(enumerate(rows, 1))
            for round_number in range(1, limit + 2):
                if stop_requested and stop_requested():
                    report['status'] = 'cancelled'
                    break
                if limit and any(row.get('enabled', True) for _, row in pending):
                    desktop = runtime_desktop_report()
                    if desktop.get('status') != 'observed_available':
                        report.update(status='desktop_environment_interrupted', desktop_interruption=desktop)
                        break
                report['current_round'] = round_number
                round_report = {'round': round_number, 'assistants': []}
                report['rounds'].append(round_report)
                progress(f'第 {round_number} / {limit + 1} 轮')
                save(directory / 'summary.json', report)
                for index, row in pending:
                    if stop_requested and stop_requested():
                        report['status'] = 'cancelled'
                        break
                    if not row.get('enabled', True):
                        latest[row['id']] = disabled_entry(row)
                        report['assistants'] = [latest[r['id']] for r in rows if r['id'] in latest]
                        save(directory / 'summary.json', report)
                        continue
                    progress(f"[{index}/{len(data['assistants'])}] {row['id']}: checking saved assistant entry...")
                    node_number = (round_number - 1) * len(rows) + index
                    node_directory = directory / f'node-{node_number:02d}'
                    node_directory.mkdir()
                    report['active_node_summary'] = str(node_directory / 'summary.json')
                    save(directory / 'summary.json', report)
                    try:
                        current = prepare_assistant(row)
                        if current['blockers']:
                            raise ValueError(', '.join(current['blockers']))
                        if current.get('completion_kind') == 'onedragon':
                            current, cursor = onedragon.bind_execution(current, node_directory)
                        elif current.get('completion_kind') == 'maa-end':
                            current, cursor = maaend.bind_execution(current, node_directory)
                        elif current.get('completion_kind') == 'march7':
                            current, cursor = march7.bind_execution(current, node_directory)
                        elif current.get('completion_kind') == 'maa-gf2':
                            current, cursor = maagf2.bind_execution(current, node_directory)
                        elif current.get('completion_kind') == 'bettergi':
                            current, cursor = bettergi.bind_execution(current, node_directory)
                        else:
                            cursor = capture_cursor(Path(current['cwd']) / 'logs/ok-script.log')
                    except (OSError, ValueError, SyntaxError, KeyError, TypeError, StopIteration) as exc:
                        result = {'id': row['id'], 'status': 'blocked', 'error': str(exc), 'cleanup_verified': True,
                                  'game_execution': False}
                        # No process was launched here. An exclusivity/identity
                        # blocker requires intervention, not repeated admission.
                        # Manual admission can fail with an external game already
                        # open (for example a still-enabled launch pre-action).
                        # Never skip into another game in that situation.
                        result['repeat_blocked'] = row.get('game_start') == 'manual' or isinstance(exc, ProcessObservationError) or any(blocker in str(exc) for blocker in (
                            'existing_runner_or_game_not_exclusive', 'existing_runner_not_exclusive',
                            'manual_game_requires_exactly_one_running_instance', 'identity uncertain'))
                        result['run_id'] = report['run_id']
                        save(node_directory / 'summary.json', result)
                    else:
                        progress(f"{row['id']}: starting current saved preset (Ctrl+C to stop)")
                        observers = {'onedragon': onedragon.observe, 'ok-nte': nte_completion.observe,
                                     'maa-end': maaend.observe, 'march7': march7.observe, 'maa-gf2': maagf2.observe,
                                     'bettergi': bettergi.observe}
                        observer = observers.get(current.get('completion_kind'))
                        options = {'completion_observer': observer} if observer else {}
                        result, _ = run_prepared(current, session, node_directory, cursor,
                                                 row.get('timeout_seconds', 1800),
                                                 metadata={'id': row['id'], 'runner_version': current['runner_version'],
                                                           'task_configuration': current['task_configuration'],
                                                           'round': round_number},
                                                 finish_session=False, progress=progress,
                                                 desktop_check=runtime_desktop_report,
                                                 **({'stop_requested': stop_requested} if stop_requested else {}), **options)
                    attempt = dict(result, round=round_number, node_summary=str(node_directory / 'summary.json'))
                    history.setdefault(row['id'], []).append(attempt)
                    latest[row['id']] = dict(attempt, attempts=list(history[row['id']]))
                    round_report['assistants'].append(attempt)
                    report['assistants'] = [latest[r['id']] for r in rows if r['id'] in latest]
                    progress(f"{row['id']}: {result['status']}")
                    failed_apps = result.get('completion', {}).get('failed_applications', [])
                    if failed_apps:
                        progress(f"{row['id']}: assistant reported failed applications: {', '.join(failed_apps)}")
                    save(directory / 'summary.json', report)
                    if result.get('cleanup_verified') is not True:
                        report['status'] = 'stopped_cleanup_unconfirmed'
                        break
                    if result['status'] in {'cancelled', 'desktop_environment_interrupted'}:
                        report['status'] = result['status']
                        break
                    if result.get('external_game_left_running'):
                        report['status'] = 'stopped_external_game_running'
                        break
                    if result.get('repeat_blocked'):
                        report['status'] = 'stopped_preflight_blocked'
                        break
                if report['status'] != 'running':
                    round_report['status'] = report['status']
                    break
                pending = [(index, row) for index, row in enumerate(rows, 1)
                           if row.get('enabled', True) and latest[row['id']]['status'] != 'success']
                round_report['status'] = 'completed_with_failures' if pending else 'success'
                report.pop('active_node_summary', None)
                save(directory / 'summary.json', report)
                if not pending:
                    report['status'] = ('success' if any(r.get('enabled', True) for r in rows)
                                        else 'nothing_enabled')
                    break
            else:
                report['status'] = 'completed_with_failures'
                report['repeat_limit_reached'] = limit > 0
            report.pop('active_node_summary', None)
            if report['status'] != 'stopped_cleanup_unconfirmed':
                session.finish(report['status'], cleanup_verified=True)
        except BaseException as exc:
            report.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'execution_error', error=str(exc))
            save(directory / 'summary.json', report)
            raise
        save(directory / 'summary.json', report)
    return report, directory
