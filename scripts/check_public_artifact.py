"""Public-artifact gate. Reports rule IDs and locations, never matched values."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess


RULES = (
    ('PERSONAL_PATH', re.compile(r'(?i)(?:[a-z]:[\\/]+users[\\/]+|/' + r'users/|/' + r'home/)[a-z0-9_.-]+')),
    ('PRIVATE_CHAT', re.compile(r'(?i)(?:chatgpt(?:-conversation)?://|chatgpt\.com/(?:c|share)/|codex://threads/)[a-z0-9_-]{8,}')),
    ('CREDENTIAL', re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)')),
)
PRIVATE_DIRS = {'.git', 'runs', 'state', 'build', 'dist', '__pycache__', '.venv', '.superpowers', 'evidence'}
PRIVATE_FILES = {'secret.json', 'secrets.json', 'gachaops-workspace.json', '.env', 'credentials.json'}


def inspect(path, data):
    parts = PurePosixPath(path).parts
    findings = []
    if (any(p.lower() in PRIVATE_DIRS for p in parts) or parts[-1].lower() in PRIVATE_FILES
            or (parts[-1].lower().startswith('local') and parts[-1].lower().endswith('.json'))):
        findings.append({'path': path, 'rule': 'PRIVATE_FILE'})
    texts = [data.decode('utf-8', errors='replace'), data.decode('utf-16-le', errors='replace')]
    for rule, pattern in RULES:
        if any(pattern.search(text) for text in texts):
            findings.append({'path': path, 'rule': rule})
    return findings


def scan_root(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('invalid artifact root')
    findings = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            findings.append({'path': relative, 'rule': 'SYMLINK'})
        elif path.is_file():
            findings.extend(inspect(relative, path.read_bytes()))
    return findings


def scan_history(root):
    root = Path(root).resolve()
    git = ['git', '-c', 'safe.directory=' + root.as_posix(), '-C', str(root)]
    objects = subprocess.check_output(git + ['rev-list', '--objects', '--all']).decode('utf-8').splitlines()
    findings = []
    for row in objects:
        oid, _, path = row.partition(' ')
        if not path:
            continue
        kind = subprocess.check_output(git + ['cat-file', '-t', oid]).strip()
        if kind != b'blob':
            continue
        data = subprocess.check_output(git + ['cat-file', 'blob', oid])
        findings.extend(dict(finding, object=oid) for finding in inspect(path, data))
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--root', type=Path)
    mode.add_argument('--git-history', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    try:
        findings = scan_root(args.root) if args.root else scan_history(args.git_history)
        report = {'schema_version': 1, 'status': 'blocked' if findings else 'passed', 'findings': findings}
        code = 1 if findings else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        report = {'schema_version': 1, 'status': 'blocked', 'findings': [{'path': '.', 'rule': 'SCAN_FAILED'}]}
        code = 2
    text = json.dumps(report, ensure_ascii=True, indent=2)
    if args.report:
        args.report.write_text(text + '\n', encoding='utf-8')
    print(text)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
