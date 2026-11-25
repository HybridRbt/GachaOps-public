"""GUI-independent configuration snapshots and result presentation."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import uuid

from .assistants import load_assistants
from .configuration import compose_configuration
from .profiles import validate_profile_rows
from .recovery import save

from .adapter_catalog import list_adapters

NAMES = {s.game_id: s.display_name for s in list_adapters()}
STATUS = {'disabled': '未选择', 'ready': '检查通过', 'blocked': '需要处理', 'starting': '正在启动',
          'running': '运行中', 'success': '完成', 'partial_failure': '部分任务失败',
          'completion_unconfirmed': '结果未确认', 'completed_with_failures': '完成，含失败或未确认项',
          'nothing_enabled': '未选择助手', 'cancelled': '已停止', 'timeout': '超时',
          'interrupted': '已中断', 'execution_error': '运行异常',
          'ownership_or_cleanup_unconfirmed': '进程归属或清理未确认',
          'stopped_cleanup_unconfirmed': '已停止：清理未确认',
          'stopped_external_game_running': '已停止：手动游戏仍在运行',
          'stopped_preflight_blocked': '已停止：进程预检需要检查',
          'desktop_environment_interrupted': '已停止：桌面环境中断'}


def selected_configuration(data, order, selected):
    """Order and enable rows of a run-field profile; unregistered rows stay selectable."""
    rows = {r['id']: r for r in data['assistants']}
    if len(order) != len(rows) or set(order) != set(rows) or not set(selected) <= set(rows):
        raise ValueError('invalid assistant selection/order')
    result = deepcopy(data)
    result['assistants'] = [deepcopy(rows[i]) for i in order]
    for row in result['assistants']:
        row['enabled'] = row['id'] in selected
    return validate_profile_rows(result)


def compose_snapshot(selection, registry):
    """Merge the current selection with the global registration for the worker."""
    return compose_configuration(selection, registry)


def bettergi_inspection(executable):
    """Validate BetterGI.exe for the quick registration; never launches anything."""
    from .registration import inspect_registration
    try:
        path = Path(executable).resolve(strict=True)
    except OSError as error:
        raise ValueError('找不到所选的 BetterGI.exe') from None
    if path.name.casefold() != 'bettergi.exe':
        raise ValueError('请选择 BetterGI.exe')
    return inspect_registration('bettergi', {'installation_root': str(path.parent),
                                             'entry_executable': str(path)})


HANDOFF_KIND = 'desktop_handoff'


def _handoff_document(source_profile_id, source_profile_revision, registry_revision, draft):
    if source_profile_id is not None:
        try:
            if str(uuid.UUID(source_profile_id)) != source_profile_id:
                raise ValueError('invalid profile id')
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid handoff source profile id') from None
    if source_profile_revision is not None and (type(source_profile_revision) is not int
                                                or source_profile_revision < 1):
        raise ValueError('invalid handoff source revision')
    if type(registry_revision) is not int or registry_revision < 1:
        raise ValueError('invalid handoff registry revision')
    validate_profile_rows(draft)
    return {'schema_version': 1, 'kind': HANDOFF_KIND, 'source_profile_id': source_profile_id,
            'source_profile_revision': source_profile_revision,
            'registry_revision': registry_revision, 'draft': deepcopy(draft)}


def write_handoff(path, source_profile_id, source_profile_revision, registry_revision, draft):
    """Elevation handoff: run-field draft plus provenance; no registration paths."""
    document = _handoff_document(source_profile_id, source_profile_revision, registry_revision, draft)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, document)
    return document


def read_handoff(path):
    """Validate and load an elevation handoff; invalid files fail loudly."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('external handoff file')
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
    except ValueError as error:
        raise ValueError(f'提权交接文件损坏：{error}') from None
    if (not isinstance(raw, dict) or raw.get('schema_version') != 1
            or raw.get('kind') != HANDOFF_KIND
            or set(raw) != {'schema_version', 'kind', 'source_profile_id', 'source_profile_revision',
                            'registry_revision', 'draft'}):
        raise ValueError('提权交接文件格式无效')
    return _handoff_document(raw['source_profile_id'], raw['source_profile_revision'],
                             raw['registry_revision'], raw['draft'])


def save_configuration(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    save(path, data)
    return load_assistants(path)


def desktop_command():
    return ([sys.executable] if getattr(sys, 'frozen', False)
            else [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/desktop_entry.py')])


def worker_command(action, config, output, cancel, log, *, data_root=None):
    command = desktop_command()
    return command + ['--worker', '--log', str(log), action, *(['--data-root', str(data_root)] if data_root is not None else []), '--config', str(config),
                      '--output', str(output), '--cancel-file', str(cancel)]


def elevation_command(arguments):
    return subprocess.list2cmdline(arguments)
