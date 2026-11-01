"""Audit a candidate with explicit, exact reviewed binary dispositions.

Raw source/history scanning never consumes these dispositions. Approval records
are reviewed repository data, not cryptographic identity or distribution approval.
"""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from check_public_artifact import scan_root


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def is_reparse_point(path):
    """Reject links/junctions on supported Python 3.11 as well as newer versions."""
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(info, 'st_file_attributes', None)
    if os.name == 'nt' and attributes is None:
        raise ValueError('Windows reparse detection unavailable')
    return stat.S_ISLNK(info.st_mode) or bool((attributes or 0) & 0x400)


def read_record(path):
    path = Path(path)
    if is_reparse_point(path):
        raise ValueError('external record')
    if not path.is_file() or path.stat().st_size > 1_000_000:
        raise ValueError('invalid record')
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique_object)
    except RecursionError:
        raise ValueError('record nesting too deep') from None
    if not isinstance(value, dict):
        raise ValueError('invalid record')
    return value


def file_sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def valid_hash(value, length=64):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{' + str(length) + '}', value) is not None


def canonical_path(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('invalid relative path')
    if any(ord(c) < 32 for c in value) or any(c in value for c in '*?[]<>"|'):
        raise ValueError('invalid relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise ValueError('invalid relative path')
    if any(p in {'.', '..'} or p.endswith((' ', '.')) for p in path.parts):
        raise ValueError('invalid relative path')
    for part in path.parts:
        stem = part.split('.')[0].upper()
        if stem in {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'} or re.fullmatch(r'(?:COM|LPT)[1-9¹²³]', stem):
            raise ValueError('reserved Windows path')
    return path


def candidate_identity(root, record):
    expected = record.get('candidate_executable_sha256')
    commit = record.get('candidate_code_commit')
    if not valid_hash(expected) or not valid_hash(commit, 40):
        raise ValueError('invalid candidate identity')
    manifest = read_record(root / 'build-manifest.json')
    manifest_hash = manifest.get('executable_sha256')
    if (type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or manifest.get('dirty') is not False or manifest.get('commit') != commit
            or not isinstance(manifest_hash, str) or manifest_hash.lower() != expected
            or file_sha256(root / 'GachaOps.exe') != expected):
        raise ValueError('candidate identity mismatch')


def validate_policy(policy):
    if (type(policy.get('schema_version')) is not int or policy['schema_version'] != 1
            or policy.get('approved') is not True
            or policy.get('status') != 'approved_exact_binary_findings'):
        raise ValueError('unapproved policy')
    approval = policy.get('approval')
    if (not isinstance(approval, dict) or approval.get('by') != 'maintainer'
            or approval.get('scope') != 'binary_privacy_only'):
        raise ValueError('invalid approval')
    date.fromisoformat(approval.get('date', ''))
    rows = policy.get('findings')
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise ValueError('invalid dispositions')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', 'rule', 'sha256', 'classification'}:
            raise ValueError('invalid disposition')
        path = canonical_path(row['path'])
        if (len(path.parts) < 2 or path.parts[0] != '_internal'
                or (path.suffix.lower() not in {'.dll', '.pyd'}
                    and row['path'] != '_internal/base_library.zip')):
            raise ValueError('disposition outside binary scope')
        classification = {'PERSONAL_PATH': 'dependency_build_path',
                          'CREDENTIAL': 'dependency_pem_format_literal'}.get(row['rule'])
        if classification is None or row['classification'] != classification or not valid_hash(row['sha256']):
            raise ValueError('invalid disposition rule or hash')
        key = (row['path'].casefold(), row['rule'])
        if key in seen:
            raise ValueError('duplicate disposition')
        seen.add(key)
    return rows


def check_tree(root):
    if not root.is_dir() or is_reparse_point(root):
        raise ValueError('invalid candidate root')
    for path in root.rglob('*'):
        if is_reparse_point(path):
            raise ValueError('external candidate file')


def audit(root, dispositions=None):
    root = Path(root).absolute()
    report = {'schema_version': 1, 'status': 'blocked', 'distribution_approved': False,
              'raw_findings': [], 'accepted_findings': [], 'unresolved_findings': [], 'errors': []}
    try:
        check_tree(root)
        report['raw_findings'] = scan_root(root)
    except (OSError, ValueError):
        report['errors'].append('SCAN_FAILED')
        return report
    report['unresolved_findings'] = list(report['raw_findings'])
    if dispositions is None:
        try:
            manifest = read_record(root / 'build-manifest.json')
            value = manifest.get('executable_sha256')
            candidate_identity(root, {'candidate_code_commit': manifest.get('commit'),
                                     'candidate_executable_sha256': value.lower() if isinstance(value, str) else None})
        except (OSError, ValueError, TypeError, KeyError):
            report['errors'].append('INVALID_CANDIDATE_IDENTITY')
            return report
        report['status'] = 'blocked' if report['raw_findings'] else 'passed'
        return report
    try:
        policy = dispositions if isinstance(dispositions, dict) else read_record(dispositions)
        rows = validate_policy(policy)
        candidate_identity(root, policy)
    except (OSError, ValueError, TypeError, KeyError):
        report['errors'].append('INVALID_DISPOSITION_POLICY_OR_CANDIDATE')
        return report
    raw_keys = {(r['path'], r['rule']) for r in report['raw_findings']}
    allowed = set()
    for row in rows:
        key = (row['path'], row['rule'])
        try:
            if key not in raw_keys or file_sha256(root / row['path']) != row['sha256']:
                raise ValueError('stale or changed disposition')
        except (OSError, ValueError):
            report['errors'].append('STALE_OR_CHANGED_DISPOSITION')
            continue
        allowed.add(key)
        report['accepted_findings'].append(dict(row))
    report['unresolved_findings'] = [r for r in report['raw_findings'] if (r['path'], r['rule']) not in allowed]
    report['status'] = 'blocked' if report['errors'] or report['unresolved_findings'] else 'passed'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, required=True)
    parser.add_argument('--dispositions', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if args.report:
        target = args.report.resolve()
        if target.is_relative_to(args.app_dir.resolve()) or target.exists():
            print(json.dumps({'status': 'blocked', 'errors': ['REPORT_MUST_BE_NEW_AND_OUTSIDE_CANDIDATE']}))
            return 1
    report = audit(args.app_dir, args.dispositions)
    text = json.dumps(report, ensure_ascii=True, indent=2) + '\n'
    if args.report:
        try:
            with args.report.open('x', encoding='utf-8') as stream:
                stream.write(text)
        except OSError:
            print(json.dumps({'status': 'blocked', 'errors': ['REPORT_WRITE_FAILED']}))
            return 1
    print(text, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
