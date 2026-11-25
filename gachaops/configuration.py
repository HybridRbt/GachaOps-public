"""Compose run-field profiles with the global registration into schema 1 snapshots."""
from copy import deepcopy

from .assistants import validate_assistants
from .planner import ConfigError
from .profiles import validate_profile_rows
from .registry import validate_registry


def registration_of(registry, adapter_id):
    entry = registry['assistants'].get(adapter_id)
    return {} if entry is None else deepcopy(entry)


def compose_configuration(profile, registry):
    """Merge one profile's run fields with registration paths.

    Returns a schema 1 full configuration for the executor. Selected rows whose
    adapter has no registration fail loudly; unselected rows never block.
    Neither input is modified.
    """
    validate_profile_rows(profile)
    validate_registry(registry)
    result = {'schema_version': 1, 'repeat_limit': profile.get('repeat_limit', 0), 'assistants': []}
    missing = []
    for row in profile['assistants']:
        merged = {field: deepcopy(row[field]) for field in row}
        entry = registry['assistants'].get(row['adapter'])
        if entry is None:
            if row.get('enabled', True):
                missing.append(row['id'])
        else:
            merged.update(deepcopy(entry))
        result['assistants'].append(merged)
    if missing:
        raise ConfigError('以下助手尚未登记接入，不能运行：' + '、'.join(missing))
    return validate_assistants(result)
