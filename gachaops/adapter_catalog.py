"""Immutable built-in integration declarations. No user imports or argv hooks."""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class AdapterSpec:
    adapter_id: str
    game_id: str
    display_name: str
    official_url: str
    registration_fields: tuple[str, ...]
    start_modes: tuple[str, ...]
    prepare: Callable


def validate_catalog(specs):
    result = tuple(specs)
    if (any(not isinstance(s, AdapterSpec) or not s.adapter_id or not s.game_id
            or not callable(s.prepare) or not s.official_url.startswith('https://')
            or not s.start_modes or not set(s.start_modes) <= {'assistant', 'manual'}
            or not set(s.registration_fields) <= {'installation_root', 'entry_executable', 'game_path'}
            for s in result)
            or len({s.adapter_id for s in result}) != len(result)
            or len({s.game_id for s in result}) != len(result)):
        raise ValueError('invalid or duplicate adapter declaration')
    return result


def _ok(row):
    from .assistants import _prepare_ok_assistant
    return _prepare_ok_assistant(row)


def _zzz(row):
    from .onedragon import prepare
    return prepare(row)


def _end(row):
    from .maaend import prepare
    return prepare(row)


def _gf2(row):
    from .maagf2 import prepare
    return prepare(row)


def _rail(row):
    from .march7 import prepare
    return prepare(row)


def _gi(row):
    from .bettergi import prepare
    return prepare(row)


_CATALOG = validate_catalog((
    AdapterSpec('ok-ww', 'wuthering-waves', '鸣潮', 'https://github.com/ok-oldking/ok-wuthering-waves',
                ('installation_root',), ('assistant', 'manual'), _ok),
    AdapterSpec('zzz-onedragon', 'zenless-zone-zero', '绝区零', 'https://github.com/OneDragon-Anything/ZenlessZoneZero-OneDragon',
                ('installation_root', 'game_path'), ('assistant',), _zzz),
    AdapterSpec('maa-end', 'endfield', '终末地', 'https://github.com/MaaEnd/MaaEnd',
                ('installation_root', 'entry_executable'), ('assistant', 'manual'), _end),
    AdapterSpec('maa-gf2', 'girls-frontline-2', '少女前线2', 'https://github.com/DarkLingYun/MaaGF2Exilium',
                ('installation_root', 'entry_executable'), ('assistant',), _gf2),
    AdapterSpec('ok-nte', 'neverness-to-everness', '异环', 'https://github.com/BnanZ0/ok-nte',
                ('installation_root', 'entry_executable'), ('assistant',), _ok),
    AdapterSpec('bettergi', 'genshin', '原神', 'https://github.com/babalae/better-genshin-impact',
                ('installation_root', 'entry_executable'), ('assistant',), _gi),
    AdapterSpec('march7', 'honkai-star-rail', '星铁', 'https://github.com/moesnow/March7thAssistant',
                ('installation_root', 'entry_executable'), ('assistant',), _rail),
))


def list_adapters():
    return _CATALOG


def get_adapter(adapter_id):
    for spec in _CATALOG:
        if spec.adapter_id == adapter_id:
            return spec
    raise ValueError('unknown adapter: ' + str(adapter_id))
