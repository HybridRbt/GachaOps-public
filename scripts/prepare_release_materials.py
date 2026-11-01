"""Assemble verified local release inputs without downloading or granting approval."""
import argparse
import hashlib
import json
from pathlib import Path

from check_release_binary import canonical_path, is_reparse_point, read_record, valid_hash
from stage_release import REQUIRED, source_bytes

RELEASE = 'docs/release/'
BUNDLE = RELEASE + 'source-delivery/SOURCE_BUNDLE_MANIFEST.json'
APPROVAL = RELEASE + 'DISTRIBUTION_APPROVAL.json'
PROPOSAL = RELEASE + 'DISTRIBUTION_DELIVERY_PROPOSAL.json'
PREFIXES = (RELEASE + 'dependency-licenses/', RELEASE + 'source-delivery/')


def prepare(source, output, archive_dirs, proposal=False, proposal_file=None):
    source, output = Path(source).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(source):
        raise ValueError('output must be new and outside source')
    if proposal_file is not None:
        record_name = canonical_path(proposal_file).as_posix()
        if not record_name.startswith(RELEASE) or not proposal:
            raise ValueError('proposal requires explicit proposal mode and release path')
    else:
        record_name = PROPOSAL if proposal else APPROVAL
    record = read_record(source / record_name)
    required = record.get('required_sha256')
    materials = record.get('materials')
    if (record.get('schema_version') != 1 or not isinstance(required, dict)
            or set(required) != set(REQUIRED) or record.get('required_destinations') != REQUIRED
            or not isinstance(materials, list) or not materials):
        raise ValueError('incomplete material record')
    bundle = read_record(source / BUNDLE)
    archive_rows = bundle.get('archives')
    if bundle.get('schema_version') != 1 or not isinstance(archive_rows, list):
        raise ValueError('invalid archive manifest')
    archives = {}
    for row in archive_rows:
        relative = canonical_path(row['source']).as_posix()
        file = row['file']
        if (not relative.startswith(RELEASE + 'source-delivery/archives/')
                or canonical_path(file).as_posix() != file or len(Path(file).parts) != 1
                or Path(relative).name != file or not valid_hash(row['sha256'])
                or type(row['bytes']) is not int or row['bytes'] <= 0 or relative.casefold() in archives):
            raise ValueError('invalid archive record')
        archives[relative.casefold()] = row
    approval_bytes = source_bytes(source, APPROVAL)
    actual = read_record(source / APPROVAL)
    if json.loads(approval_bytes) != actual:
        raise ValueError('approval changed during read')
    frozen = {APPROVAL: approval_bytes, record_name: source_bytes(source, record_name), BUNDLE: source_bytes(source, BUNDLE)}
    for relative, expected in required.items():
        data = source_bytes(source, relative)
        if not valid_hash(expected) or hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('required notice changed')
        frozen[relative] = data
    seen_sources, seen_destinations = set(), set()
    for row in materials:
        relative = canonical_path(row['source']).as_posix()
        destination = canonical_path(row['destination'])
        if (not relative.startswith(PREFIXES) or destination.parts[0] not in {'THIRD_PARTY_LICENSES', 'SOURCE_DELIVERY'}
                or len(destination.parts) < 2 or not valid_hash(row['sha256'])
                or relative.casefold() in seen_sources or str(destination).casefold() in seen_destinations):
            raise ValueError('invalid material path or duplicate')
        seen_sources.add(relative.casefold()); seen_destinations.add(str(destination).casefold())
        path = source / relative
        if path.exists() or path.is_symlink():
            data = source_bytes(source, relative)
        else:
            archive = archives.get(relative.casefold())
            if archive is None or archive['sha256'] != row['sha256']:
                raise ValueError('missing declared material')
            data = None
            for directory in archive_dirs:
                candidate = Path(directory).resolve() / archive['file']
                if candidate.is_file() and not is_reparse_point(candidate):
                    contents = candidate.read_bytes()
                    if len(contents) == archive['bytes'] and hashlib.sha256(contents).hexdigest() == archive['sha256']:
                        data = contents
                        break
            if data is None:
                raise ValueError('missing or changed cached archive')
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('changed material')
        frozen[relative] = data
    policy = RELEASE + 'BINARY_DISPOSITIONS.json'
    if (source / policy).exists():
        frozen[policy] = source_bytes(source, policy)
    # All validation and archive reads finish before creating an output directory.
    output.mkdir(parents=True)
    for relative, data in frozen.items():
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return {'status': 'prepared', 'files': len(frozen), 'materials': len(materials),
            'distribution_approved': not proposal and actual.get('approved') is True,
            'historical_approval_preserved': True, 'proposal_mode': bool(proposal),
            'downloads_performed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive-dir', type=Path, action='append', default=[])
    parser.add_argument('--proposal', action='store_true')
    parser.add_argument('--proposal-file', help='Explicit source-relative pending review record; requires --proposal')
    args = parser.parse_args()
    try:
        result = prepare(args.source, args.output, args.archive_dir, args.proposal, args.proposal_file)
    except (ValueError, OSError, TypeError, KeyError):
        print(json.dumps({'status': 'blocked', 'reason': 'invalid_or_missing_material_input'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
