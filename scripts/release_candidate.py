"""Capture a build for review, then finalize only the exact approved artifact.

Approval JSON is a reviewed sidecar, not cryptographic maintainer authentication.
This tool never grants approval, builds an EXE, downloads inputs or publishes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from check_release_binary import (_unique_object, candidate_identity, canonical_path,
                                  file_sha256, read_record, valid_hash)
from prepare_release_materials import prepare, APPROVAL, BUNDLE
from stage_release import stage_release, tree_hashes, verify_tree, source_bytes

POLICY = 'docs/release/BINARY_DISPOSITIONS.json'
SIDECARS = {'distribution_approval': APPROVAL, 'source_bundle_manifest': BUNDLE,
            'binary_dispositions': POLICY}


def capture(app):
    app = Path(app).absolute()
    files = tree_hashes(app)
    manifest = read_record(app / 'build-manifest.json')
    record = {'schema_version': 1, 'approved': False, 'scope': 'local_candidate_only',
              'candidate_code_commit': manifest.get('commit'),
              'candidate_executable_sha256': str(manifest.get('executable_sha256', '')).lower(),
              'version': manifest.get('version'), 'app_files': files,
              'sidecar_sha256': {name: None for name in SIDECARS}}
    candidate_identity(app, record)
    if not isinstance(record['version'], str) or not record['version']:
        raise ValueError('missing package version')
    verify_tree(app, files)
    return record


def validate_review(app, review):
    if (type(review.get('schema_version')) is not int or review['schema_version'] != 1
            or review.get('approved') is not True or review.get('scope') != 'local_candidate_only'):
        raise ValueError('exact artifact review required')
    candidate_identity(app, review)
    if read_record(app / 'build-manifest.json').get('version') != review.get('version'):
        raise ValueError('reviewed version changed')
    expected = review.get('app_files')
    if (not isinstance(expected, dict) or not expected
            or any(not valid_hash(value) for value in expected.values())):
        raise ValueError('missing complete artifact hashes')
    verify_tree(app, expected)


def verify_sidecars(source, review):
    hashes = review.get('sidecar_sha256')
    if not isinstance(hashes, dict) or set(hashes) != set(SIDECARS):
        raise ValueError('missing reviewed sidecar hashes')
    for name, relative in SIDECARS.items():
        expected = hashes[name]
        if name == 'binary_dispositions' and expected is None:
            if (source / relative).exists():
                raise ValueError('unexpected binary policy')
            continue
        if not valid_hash(expected) or hashlib.sha256(source_bytes(source, relative)).hexdigest() != expected:
            raise ValueError('review sidecar changed')


def verify_archive_contract(source):
    bundle = read_record(source / BUNDLE)
    approval = read_record(source / APPROVAL)
    materials = approval.get('materials')
    archives = bundle.get('archives')
    if not isinstance(materials, list) or not isinstance(archives, list) or not archives:
        raise ValueError('missing source delivery contract')
    mapping = {row['source']: row for row in materials}
    for row in archives:
        material = mapping.get(row['source'])
        if (material is None or material['sha256'] != row['sha256']
                or material['destination'] != 'SOURCE_DELIVERY/archives/' + row['file']):
            raise ValueError('declared source archive missing from delivery')
    manifest_row = mapping.get(BUNDLE)
    if (manifest_row is None or manifest_row['destination'] != 'SOURCE_DELIVERY/SOURCE_BUNDLE_MANIFEST.json'
            or manifest_row['sha256'] != file_sha256(source / BUNDLE)):
        raise ValueError('source manifest not delivered as reviewed')


def verify_zip(path, expected):
    if not isinstance(expected, dict) or any(
            canonical_path(name).as_posix() != name or not valid_hash(digest)
            for name, digest in expected.items()):
        raise ValueError('invalid ZIP file manifest')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if (len(names) != len(set(names)) or set(names) != set(expected)
                or archive.testzip() is not None):
            raise ValueError('ZIP inventory or CRC mismatch')
        for name, digest in expected.items():
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                    raise ValueError('ZIP entry hash mismatch')


def verify_package(path, summary):
    if (summary.get('schema_version') != 1 or summary.get('status') != 'candidate_verified'
            or not valid_hash(summary.get('zip_sha256'))
            or file_sha256(path) != summary['zip_sha256']):
        raise ValueError('candidate ZIP identity mismatch')
    with zipfile.ZipFile(path) as archive:
        checksum_bytes = archive.read('checksums.json')
        expected = json.loads(checksum_bytes, object_pairs_hook=_unique_object)
        if not isinstance(expected, dict) or 'checksums.json' in expected:
            raise ValueError('invalid package checksum manifest')
        expected['checksums.json'] = hashlib.sha256(checksum_bytes).hexdigest()
        verify_zip(path, expected)
        manifest = json.loads(archive.read('build-manifest.json'), object_pairs_hook=_unique_object)
        if (manifest.get('commit') != summary.get('candidate_code_commit')
                or manifest.get('version') != summary.get('version')
                or summary.get('files') != len(expected)):
            raise ValueError('package version or commit mismatch')
    return summary


def finalize(source, app, review, output, archive_dirs=()):
    source, app, output = Path(source).resolve(), Path(app).absolute(), Path(output).absolute()
    if (output.exists() or output.is_symlink() or output.resolve().is_relative_to(source)
            or output.resolve().is_relative_to(app.resolve())):
        raise ValueError('output must be new and outside all inputs')
    validate_review(app, review)
    verify_sidecars(source, review)
    verify_archive_contract(source)
    # prepare validates every source byte and archive before creating its output.
    prepared = output / 'materials'
    prepare(source, prepared, archive_dirs)
    verify_sidecars(prepared, review)
    validate_review(app, review)
    policy = prepared / POLICY if review['sidecar_sha256']['binary_dispositions'] is not None else None
    staged = output / 'staging'
    hashes = stage_release(prepared, app, staged, policy)
    for name, expected in review['app_files'].items():
        if file_sha256(staged / name) != expected:
            raise ValueError('staged artifact differs from reviewed artifact')
    # Keep the stager's reviewed bytes authoritative; never re-record a drifted tree.
    expected = dict(hashes)
    checksum_bytes = (json.dumps(hashes, sort_keys=True, indent=2) + '\n').encode('utf-8')
    expected['checksums.json'] = hashlib.sha256(checksum_bytes).hexdigest()
    verify_tree(staged, expected)
    pending = output / 'candidate.zip.incomplete'
    final = output / 'GachaOps-candidate.zip'
    try:
        with zipfile.ZipFile(pending, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(expected):
                archive.write(staged / name, name)
        verify_tree(staged, expected)
        verify_zip(pending, expected)
        pending.rename(final)
        result = {'schema_version': 1, 'status': 'candidate_verified', 'version': review['version'],
                  'candidate_code_commit': review['candidate_code_commit'], 'files': len(expected),
                  'zip_sha256': file_sha256(final), 'public_release_approved': False}
        (output / 'release-summary.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8', newline='')
        return result
    except (OSError, ValueError, zipfile.BadZipFile):
        pending.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture_parser = commands.add_parser('capture')
    capture_parser.add_argument('--app-dir', type=Path, required=True)
    capture_parser.add_argument('--output', type=Path, required=True)
    finish = commands.add_parser('finalize')
    finish.add_argument('--source', type=Path, required=True)
    finish.add_argument('--app-dir', type=Path, required=True)
    finish.add_argument('--review', type=Path, required=True)
    finish.add_argument('--output', type=Path, required=True)
    finish.add_argument('--archive-dir', type=Path, action='append', default=[])
    verify = commands.add_parser('verify')
    verify.add_argument('--zip', type=Path, required=True)
    verify.add_argument('--summary', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            if args.output.resolve().is_relative_to(args.app_dir.resolve()):
                raise ValueError('review record must be outside app')
            record = capture(args.app_dir)
            with args.output.open('x', encoding='utf-8', newline='') as stream:
                stream.write(json.dumps(record, sort_keys=True, indent=2) + '\n')
            result = {'status': 'pending_review', 'files': len(record['app_files']), 'approved': False}
        elif args.command == 'finalize':
            result = finalize(args.source, args.app_dir, read_record(args.review), args.output, args.archive_dir)
        else:
            result = verify_package(args.zip, read_record(args.summary))
        print(json.dumps(result))
        return 0
    except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile):
        print(json.dumps({'status': 'blocked', 'reason': 'unapproved_changed_or_missing_release_input'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
