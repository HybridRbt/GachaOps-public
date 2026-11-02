"""Evidence-backed display matrix and per-preparation installation drift guard."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path

from .adapter_catalog import list_adapters


def validate_support_matrix(data):
    specs = {s.adapter_id: s for s in list_adapters()}
    if not isinstance(data, dict) or data.get('schema_version') != 1 or not isinstance(data.get('adapters'), list):
        raise ValueError('invalid support matrix')
    rows = data['adapters']
    if len(rows) != len(specs) or {r.get('adapter_id') for r in rows} != set(specs):
        raise ValueError('support matrix must describe exactly the built-in catalog')
    for row in rows:
        spec = specs[row['adapter_id']]
        if (row.get('upstream') != spec.official_url or row.get('start_modes') != list(spec.start_modes)
                or row.get('status') not in {'implemented', 'offline_verified', 'windows_verified'}
                or not isinstance(row.get('verified_versions'), list) or not row.get('contract')
                or not isinstance(row.get('limitations'), list) or not row.get('evidence')):
            raise ValueError('invalid compatibility evidence')
        date.fromisoformat(row['evidence_date'])
        for ref in row['evidence']:
            if not isinstance(ref, str) or not ref.startswith('docs/') or '..' in Path(ref).parts or not (Path(__file__).resolve().parents[1] / ref).is_file():
                raise ValueError('invalid compatibility evidence reference')
        if row['status'] == 'windows_verified' and (not row['verified_versions'] or not row.get('windows_scenario')):
            raise ValueError('Windows support requires exact versions and a scenario')
    return data


def load_support_matrix():
    return validate_support_matrix(json.loads((Path(__file__).parent / 'assets/support_matrix.json').read_text(encoding='utf-8')))


def _stamp(probe):
    root = Path(probe['installation_root']).resolve(strict=True)
    candidates = {Path(arg) for arg in probe.get('argv_candidate', []) if Path(arg).is_absolute() and Path(arg).is_file()}
    for pattern in ('*.exe', '*.json', '*.yaml', 'assets/config/version.txt', 'resource/interface.json',
                    'config/*.json', 'User/config.json', 'User/OneDragon/*.json', 'tasks/pretasks/*.json',
                    'data/apps/*/app.json', 'data/apps/*/working/config.py',
                    'data/apps/*/working/src/config.py',
                    'data/apps/*/working/configs/Basic Options.json',
                    'data/apps/*/working/configs/devices.json',
                    'data/apps/*/working/configs/LauncherTask.json',
                    'src/one_dragon/version.py'):
        candidates.update(root.glob(pattern))
    rows = []
    for path in sorted(candidates):
        if not path.is_file(): continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            # Python bridge interpreters are resources outside the installation;
            # only bind installation-owned files into this guard.
            if path in {Path(arg) for arg in probe.get('argv_candidate', [])}: continue
            raise ValueError('external installation metadata')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        rows.append({'path': str(path), 'sha256': digest})
    return rows


def capture_installation_stamp(descriptor):
    if not descriptor.get('installation_root') or not descriptor.get('argv_candidate'):
        return descriptor
    probe = {key: deepcopy(descriptor[key]) for key in ('installation_root', 'argv_candidate')}
    files = _stamp(probe)
    # Bind the bytes whose executable identity was inspected, rather than a
    # replacement installed between that inspection and this snapshot.
    if descriptor.get('launcher_sha256') is not None:
        expected = {'path': descriptor['launcher'], 'sha256': descriptor['launcher_sha256']}
        if expected not in files:
            raise ValueError('助手入口在身份检查期间发生变化，请重新检查；不会启动游戏。')
    stamp = {'probe': probe, 'files': files}
    if descriptor.get('completion_kind') == 'maa-gf2':
        from .maagf2 import launch_metadata
        probe['maa_gf2_instance'] = descriptor['instance_id']
        metadata = launch_metadata(probe['installation_root'], probe['maa_gf2_instance'])
        if metadata != descriptor.get('native_launch_metadata'):
            raise ValueError('助手启动配置在预检期间发生变化，请重新检查；不会启动游戏。')
        stamp['native_launch_metadata'] = metadata
    return dict(descriptor, installation_stamp=stamp)


def verify_installation_stamp(descriptor):
    stamp = descriptor.get('installation_stamp')
    if stamp is None:
        return  # Legacy exact-SHA trials have their own stronger manifest guard.
    try:
        if _stamp(stamp['probe']) != stamp['files']:
            raise ValueError('installation changed')
        if 'maa_gf2_instance' in stamp['probe']:
            from .maagf2 import launch_metadata
            if launch_metadata(stamp['probe']['installation_root'], stamp['probe']['maa_gf2_instance']) != stamp.get('native_launch_metadata'):
                raise ValueError('native startup metadata changed')
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('助手安装在预检后发生变化，请重新检查；不会启动游戏。') from None
