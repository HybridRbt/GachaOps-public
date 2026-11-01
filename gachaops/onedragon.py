"""External ZZZ-OneDragon application entry and per-run completion protocol."""
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import uuid

from .preflight import desktop_report
from .windows_observation import elevation_report, find_matching_processes


def prepare(row):
    bridge = Path(__file__).with_name('onedragon_bridge.py')
    if not bridge.is_file():
        raise ValueError('OneDragon bridge missing from GachaOps package')
    root = Path(row['installation_root']).resolve(strict=True)
    runtime = root / '.venv/Scripts/python.exe'
    venv_config = root / '.venv/pyvenv.cfg'
    if not venv_config.is_file() or not venv_config.resolve().is_relative_to(root):
        raise ValueError('missing or external OneDragon virtual environment')
    home = next((line.partition('=')[2].strip() for line in venv_config.read_text(encoding='utf-8-sig').splitlines()
                 if line.partition('=')[0].strip() == 'home'), '')
    base = Path(home)
    if not home or not base.is_absolute() or not base.resolve().is_relative_to(root) or not (base / 'python.exe').is_file():
        raise ValueError('OneDragon base interpreter unavailable or outside installation')
    paths = [runtime, root / 'src/zzz_od/application/zzz_application_launcher.py',
             root / 'src/one_dragon/launcher/application_launcher.py',
             root / 'src/one_dragon/base/operation/application/application_run_context.py',
             root / 'src/one_dragon/base/operation/operation.py']
    for path in paths:
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('missing or external OneDragon application entry')
    # Check the observed public contract, not a folder name or hardcoded release.
    required = [('ZApplicationLauncher', 'create_context'),
                ('run_application', 'close_game', 'after_app_shutdown'),
                ('get_application', 'last_run_result', 'last_application_result'),
                ('op_callback', 'after_operation_done')]
    for path, names in zip(paths[1:], required):
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        identifiers = {getattr(node, key) for node in ast.walk(tree)
                       for key in ('id', 'attr', 'name') if hasattr(node, key)}
        if not set(names) <= identifiers:
            raise ValueError('OneDragon result/entry API changed; adapter review required')
    game = row['game_path']
    blockers = []
    if not Path(game).is_file():
        blockers.append('game_path_unverified')
    if elevation_report()['elevated'] is not True:
        blockers.append('supervisor_elevation_required')
    desktop = desktop_report()
    if desktop['status'] != 'observed_available':
        blockers.extend(desktop['blockers'] or ['desktop_unavailable'])
    targets = [str(runtime), str(root / '.venv/Scripts/pythonw.exe'),
               str(root / 'OneDragon-Launcher.exe'), game, str(base / 'python.exe'), str(base / 'pythonw.exe')]
    observed = find_matching_processes(targets)
    if observed['status'] != 'observed' or observed['matches']:
        blockers.append('existing_runner_or_game_not_exclusive')
    version = None
    version_path = root / 'src/one_dragon/version.py'
    if version_path.is_file():
        for node in ast.walk(ast.parse(version_path.read_text(encoding='utf-8-sig'))):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    version = node.value.value
    return {'installation_root': str(root), 'cwd': str(root),
            'argv_candidate': [str(runtime), '-B', str(bridge)],
            'observation_targets': targets, 'launcher': targets[2], 'game_path': game,
            'blockers': blockers, 'runner_version': version,
            'completion_kind': 'onedragon', 'game_start': 'assistant', 'borrowed_game': [],
            'task_configuration': 'assistant_current_saved_preset'}


@dataclass(frozen=True)
class ResultCursor:
    path: Path
    token: str
    offset: int = 0


def bind_execution(current, directory):
    cursor = ResultCursor(Path(directory) / 'runner-result.json', uuid.uuid4().hex)
    if cursor.path.exists():
        raise ValueError('OneDragon result destination already exists')
    current = dict(current, argv_candidate=current['argv_candidate'] + [
        '--root', current['installation_root'], '--output', str(cursor.path), '--token', cursor.token])
    return current, cursor


def observe(cursor, exit_code, root_pid, owned_identities=None):
    if not cursor.path.is_file():
        return {'status': 'completion_unconfirmed', 'reason': 'missing_runner_result'}
    data = json.loads(cursor.path.read_text(encoding='utf-8'))
    if (not isinstance(data, dict) or data.get('schema_version') != 1
            or data.get('token') != cursor.token or type(data.get('pid')) is not int):
        return {'status': 'completion_unconfirmed', 'reason': 'runner_result_identity_mismatch'}
    # Windows venv python.exe can redirect to a child interpreter. Accept its
    # result only with the exact PID/creation identity observed inside our Job.
    if owned_identities is None:
        writer_owned = data['pid'] == root_pid  # Direct-process protocol fixtures.
    else:
        writer_owned = type(data.get('creation_ticks')) is int and any(
            r.get('in_owned_job') is True and r.get('pid') == data['pid']
            and r.get('creation_ticks') == data['creation_ticks'] for r in owned_identities)
    if not writer_owned:
        return {'status': 'completion_unconfirmed', 'reason': 'runner_result_writer_not_owned'}
    if data.get('shutdown_completed') is not True:
        return {'status': 'runner_failed', 'reason': 'native_shutdown_unconfirmed'}
    if (exit_code != 0 or data.get('finish_reason') != 'COMPLETED'
            or data.get('operation_success') is not True
            or data.get('app_id') != 'one_dragon' or data.get('group_id') != 'one_dragon'):
        return {'status': 'runner_failed', 'finish_reason': data.get('finish_reason'), 'exit_code': exit_code}
    results = data.get('application_results')
    if (not isinstance(results, list) or not results
            or any(not isinstance(r, dict) or not isinstance(r.get('app_id'), str)
                   or type(r.get('success')) is not bool for r in results)
            or not any(r['app_id'] == 'one_dragon' and r['success'] for r in results)):
        return {'status': 'completion_unconfirmed', 'reason': 'application_results_unconfirmed'}
    failed = [r['app_id'] for r in results if not r['success']]
    return {'status': 'partial_failure' if failed else 'success', 'failed_applications': failed,
            'writer_pid': data['pid'], 'application_results': results}
