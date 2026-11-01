"""GUI-independent configuration snapshots and result presentation."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys

from .assistants import PENDING_ADAPTERS, load_assistants
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
    rows = {r['id']: r for r in data['assistants']}
    if len(order) != len(rows) or set(order) != set(rows) or not set(selected) <= set(rows):
        raise ValueError('invalid assistant selection/order')
    result = deepcopy(data)
    result['assistants'] = [deepcopy(rows[i]) for i in order]
    for row in result['assistants']:
        if row['id'] in selected and (row['adapter'] in PENDING_ADAPTERS or not row.get('installation_root')):
            raise ValueError('assistant registration is incomplete')
        row['enabled'] = row['id'] in selected
    return result


def register_bettergi(data, identity, executable):
    """Change only local path metadata; retain the user's order and selection."""
    from .registration import inspect_registration, commit_registration
    path = Path(executable).resolve(strict=True)
    if path.name.casefold() != 'bettergi.exe':
        raise ValueError('请选择 BetterGI.exe')
    inspection = inspect_registration('bettergi', {'installation_root': str(path.parent), 'entry_executable': str(path)})
    return commit_registration(data, inspection, identity), dict(inspection.observations, blockers=list(inspection.blockers))


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
