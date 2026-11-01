"""Stage only after binary review and separate distribution material approval."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from check_public_artifact import inspect
from check_document_links import check_links
from check_release_binary import (audit, canonical_path, candidate_identity, check_tree,
                                  file_sha256, is_reparse_point, read_record, valid_hash)

REQUIRED = {name: name for name in (
    'LICENSE', 'THIRD_PARTY_NOTICES.md', 'SECURITY.md',
    'docs/QUICKSTART.md', 'docs/SUPPORT_MATRIX.md', 'docs/USAGE_NOTICE.md',
    'docs/DATA_DIRECTORIES.md', 'docs/ADDING_ADAPTER.md', 'docs/VALIDATION.md',
    'docs/THIRD_PARTY_ASSISTANTS.md')}
REQUIRED['PACKAGE_README.md'] = 'README.md'
MATERIAL_CHECKS = {'dependency_notices', 'corresponding_source', 'replacement_rebuild', 'bridge_asset_rights'}


class StageBlocked(ValueError):
    def __init__(self, gate, binary_report=None):
        super().__init__('release gate blocked')
        self.gate = gate
        self.binary_report = binary_report


def source_bytes(source, relative):
    path = canonical_path(relative)
    current = source
    for part in path.parts:
        current = current / part
        if is_reparse_point(current):
            raise ValueError('external source material')
    if not current.resolve().is_relative_to(source):
        raise ValueError('external source material')
    return current.read_bytes()


def distribution_materials(source, app_dir):
    """Freeze explicitly reviewed material bytes before creating any output."""
    source_bytes(source, 'docs/release/DISTRIBUTION_APPROVAL.json')
    approval = read_record(source / 'docs/release/DISTRIBUTION_APPROVAL.json')
    if (type(approval.get('schema_version')) is not int or approval['schema_version'] != 1
            or approval.get('approved') is not True or approval.get('status') != 'approved_distribution_materials'
            or not isinstance(approval.get('checks'), dict) or set(approval['checks']) != MATERIAL_CHECKS
            or any(value is not True for value in approval['checks'].values())):
        raise ValueError('distribution materials not approved')
    candidate_identity(app_dir, approval)
    required_hashes = approval.get('required_sha256')
    if (not isinstance(required_hashes, dict) or set(required_hashes) != set(REQUIRED)
            or approval.get('required_destinations') != REQUIRED):
        raise ValueError('missing material hashes')
    frozen = {}
    for relative, destination in REQUIRED.items():
        data = source_bytes(source, relative)
        if not valid_hash(required_hashes[relative]) or hashlib.sha256(data).hexdigest() != required_hashes[relative]:
            raise ValueError('changed required material')
        frozen[destination] = data
    materials = approval.get('materials')
    if not isinstance(materials, list) or not materials or len(materials) > 2000:
        raise ValueError('missing dependency/source material')
    seen_source, seen_dest = set(), set()
    for row in materials:
        if not isinstance(row, dict) or set(row) != {'source', 'destination', 'sha256'}:
            raise ValueError('invalid material')
        relative, destination = canonical_path(row['source']), canonical_path(row['destination'])
        if (not row['source'].startswith(('docs/release/dependency-licenses/', 'docs/release/source-delivery/'))
                or destination.parts[0] not in {'THIRD_PARTY_LICENSES', 'SOURCE_DELIVERY'} or len(destination.parts) < 2
                or not valid_hash(row['sha256'])):
            raise ValueError('material outside allowed scope')
        if row['source'].casefold() in seen_source or row['destination'].casefold() in seen_dest:
            raise ValueError('duplicate material')
        seen_source.add(row['source'].casefold()); seen_dest.add(row['destination'].casefold())
        data = source_bytes(source, relative.as_posix())
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('changed dependency/source material')
        frozen[row['destination']] = data
    frozen['DISTRIBUTION_APPROVAL.json'] = (json.dumps(approval, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if any(inspect(path, data) for path, data in frozen.items()):
        raise ValueError('source material privacy gate failed')
    if check_links(frozen, REQUIRED.values()):
        raise ValueError('package document link missing or outside package')
    return frozen


def tree_hashes(root):
    check_tree(root)
    return {path.relative_to(root).as_posix(): file_sha256(path)
            for path in sorted(root.rglob('*')) if path.is_file()}


def verify_tree(root, expected):
    if tree_hashes(root) != expected:
        raise ValueError('artifact file set or reviewed bytes changed')


def stage_release(source, app_dir, output, binary_dispositions=None):
    source, app_dir, output = Path(source).resolve(), Path(app_dir).absolute(), Path(output).absolute()
    if (output.exists() or output.is_symlink() or output.resolve().is_relative_to(source)
            or output.resolve().is_relative_to(app_dir.resolve())):
        raise StageBlocked('output_path')
    try:
        policy = read_record(binary_dispositions) if binary_dispositions is not None else None
        app_hashes = tree_hashes(app_dir)
        if any(path not in {'GachaOps.exe', 'build-manifest.json'} and not path.startswith('_internal/')
               for path in app_hashes):
            raise ValueError('unexpected top-level app file')
    except (OSError, ValueError, TypeError):
        raise StageBlocked('binary_privacy') from None
    binary_report = audit(app_dir, policy)
    if binary_report['status'] != 'passed':
        raise StageBlocked('binary_privacy', binary_report)
    try:
        frozen = distribution_materials(source, app_dir)
    except (OSError, ValueError, TypeError, KeyError):
        raise StageBlocked('distribution_materials', binary_report) from None
    if policy is not None:
        frozen['BINARY_DISPOSITIONS.json'] = (json.dumps(policy, sort_keys=True, indent=2) + '\n').encode('utf-8')
        if inspect('BINARY_DISPOSITIONS.json', frozen['BINARY_DISPOSITIONS.json']):
            raise StageBlocked('source_privacy', binary_report)
    output.mkdir(parents=True, exist_ok=False)
    complete = False
    try:
        hashes = copy_and_validate(app_dir, output, policy, frozen, app_hashes)
        complete = True
        return hashes
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, StageBlocked):
            raise
        raise StageBlocked('final_integrity') from None
    finally:
        if not complete:
            (output / 'checksums.json').unlink(missing_ok=True)


def copy_and_validate(app_dir, output, policy, frozen, app_hashes):
    shutil.copy2(app_dir / 'GachaOps.exe', output / 'GachaOps.exe')
    if (app_dir / '_internal').exists(): shutil.copytree(app_dir / '_internal', output / '_internal')
    if (app_dir / 'build-manifest.json').is_file(): shutil.copy2(app_dir / 'build-manifest.json', output / 'build-manifest.json')
    for relative, data in frozen.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    final_report = audit(output, policy)
    if final_report['status'] != 'passed':
        raise StageBlocked('copied_binary_privacy', final_report)
    try:
        candidate_identity(output, json.loads(frozen['DISTRIBUTION_APPROVAL.json']))
        for relative, data in frozen.items():
            if (output / relative).read_bytes() != data:
                raise ValueError('copied material changed')
    except (OSError, ValueError, TypeError, KeyError):
        raise StageBlocked('copied_distribution_materials', final_report) from None
    report_text = json.dumps(final_report, sort_keys=True, indent=2) + '\n'
    (output / 'BINARY_PRIVACY_REPORT.json').write_text(report_text, encoding='utf-8', newline='\n')
    final_report = audit(output, policy)
    if final_report['status'] != 'passed':
        raise StageBlocked('final_privacy', final_report)
    hashes = dict(app_hashes)
    hashes.update({relative: hashlib.sha256(data).hexdigest() for relative, data in frozen.items()})
    hashes['BINARY_PRIVACY_REPORT.json'] = hashlib.sha256(report_text.encode('utf-8')).hexdigest()
    verify_tree(output, hashes)
    checksum_path = output / 'checksums.json'
    checksum_text = json.dumps(hashes, sort_keys=True, indent=2) + '\n'
    checksum_path.write_text(checksum_text, encoding='utf-8', newline='\n')
    final_report = audit(output, policy)
    if final_report['status'] != 'passed':
        raise StageBlocked('final_integrity', final_report)
    expected = dict(hashes)
    expected['checksums.json'] = hashlib.sha256(checksum_text.encode('utf-8')).hexdigest()
    verify_tree(output, expected)
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--app-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binary-dispositions', type=Path)
    args = parser.parse_args()
    try:
        result = stage_release(args.source, args.app_dir, args.output, args.binary_dispositions)
        print(json.dumps({'status': 'staged', 'files': len(result), 'public_release_approved': False}))
        return 0
    except StageBlocked as exc:
        report = {'status': 'blocked', 'reason': 'release_or_privacy_gate_failed', 'gate': exc.gate}
        if exc.binary_report is not None:
            report['binary_audit'] = exc.binary_report
        print(json.dumps(report, ensure_ascii=True))
        return 1
    except (OSError, ValueError):
        print(json.dumps({'status': 'blocked', 'reason': 'release_or_privacy_gate_failed'}))
        return 1


if __name__ == '__main__': raise SystemExit(main())
