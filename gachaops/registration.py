"""Inspect local paths separately from committing a user's profile."""
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from .adapter_catalog import get_adapter
from .assistants import prepare_assistant, validate_assistants


@dataclass(frozen=True)
class RegistrationInspection:
    candidate_row: dict
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    observations: dict


def inspect_registration(adapter_id, fields):
    spec = get_adapter(adapter_id)
    if not isinstance(fields, dict) or set(fields) - (set(spec.registration_fields) | {'game_start'}):
        raise ValueError('unknown registration field')
    mode = fields.get('game_start', 'assistant')
    if mode not in spec.start_modes:
        raise ValueError('unsupported game start mode')
    value = fields.get('installation_root')
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('请选择已安装助手的完整目录')
    root = Path(value)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('助手未安装或目录不可访问')
    root = root.resolve(strict=True)
    row = {'id': spec.game_id, 'adapter': adapter_id, 'installation_root': str(root),
           'game_start': mode, 'enabled': False}
    for field in ('entry_executable', 'game_path'):
        value = fields.get(field)
        if value is None:
            continue
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError('登记文件不存在或为外部链接')
        path = path.resolve(strict=True)
        if field == 'entry_executable' and not path.is_relative_to(root):
            raise ValueError('助手入口必须位于所选安装目录内')
        row[field] = str(path)
    if adapter_id == 'zzz-onedragon' and 'game_path' not in row:
        raise ValueError('绝区零需要登记游戏 EXE')
    try:
        observed = prepare_assistant(row)
        blockers = tuple(observed.get('blockers', ()))
    except (OSError, ValueError, SyntaxError, KeyError, TypeError, StopIteration) as error:
        text = str(error)
        category = ('version_unsupported' if any(w in text.lower() for w in ('version', 'protocol', '版本'))
                    else 'permission_or_desktop' if isinstance(error, PermissionError) else 'native_setup_required')
        blockers = (category,)
        observed = {'error': text}
    return RegistrationInspection(row, blockers, tuple(observed.get('warnings', ())), observed)


def commit_registration(profile, inspection, identity=None):
    if not isinstance(inspection, RegistrationInspection):
        raise ValueError('registration inspection required')
    row = deepcopy(inspection.candidate_row)
    spec = get_adapter(row['adapter'])
    if row.get('game_start', 'assistant') not in spec.start_modes:
        raise ValueError('unsupported game start mode')
    result = deepcopy(profile)
    targets = [r for r in result['assistants'] if r['adapter'] == spec.adapter_id
               and (identity is None or r['id'] == identity)]
    if len(targets) != 1:
        raise ValueError('registration target is missing or ambiguous')
    old = targets[0]
    enabled = old.get('enabled', True) if not inspection.blockers else False
    timeout = old.get('timeout_seconds', 1800)
    old.update(row, id=old['id'], enabled=enabled, timeout_seconds=timeout)
    validate_assistants(result)
    return result
