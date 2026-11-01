"""Create a separate public-source candidate; never edit or rewrite the source."""
import argparse
import json
from pathlib import Path
import re
import shutil

from check_public_artifact import PRIVATE_DIRS, PRIVATE_FILES, RULES, scan_root

TOP_FILES = {'README.md', 'PACKAGE_README.md', '.gitignore', '.gitattributes', 'pyproject.toml', 'requirements-desktop.txt', 'GachaOps.spec',
             'LICENSE', 'CONTRIBUTING.md', 'CODE_OF_CONDUCT.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md'}
TOP_DIRS = {'gachaops', 'tests', 'scripts', 'config', 'docs', '.github'}
INTERNAL_DOCS = {'PROJECT_STATE.md', 'BASELINE.md', 'BASELINE_FILES.json', 'UI_ACCEPTANCE_20261003.md'}
# Default deny: new development/audit documents never enter a public root implicitly.
PUBLIC_DOCS = {
    'QUICKSTART.md', 'USAGE_NOTICE.md', 'SUPPORT_MATRIX.md', 'DATA_DIRECTORIES.md',
    'ADDING_ADAPTER.md', 'PUBLIC_SOURCE.md', 'VALIDATION.md', 'THIRD_PARTY_ASSISTANTS.md',
    'index.html', 'images/program.svg', 'images/first-start.png',
    'release/RELEASE_RUNBOOK.md', 'release/BINARY_DISPOSITIONS.json',
    'release/DISTRIBUTION_APPROVAL.json', 'release/DISTRIBUTION_REVIEW_PROPOSAL.json',
}
MATERIAL_DIRS = ('release/dependency-licenses/', 'release/source-delivery/')
# Runtime compatibility references keep their stable paths. Public summaries are
# separately authored; the complete historical evidence stays private and intact.
PUBLIC_DOC_REPLACEMENTS = {name: 'public/' + name for name in (
    'TEST_ASSISTANTS.md', 'ZZZ_ONEDRAGON.md', 'MAAEND.md', 'MAAGF2.md',
    'OK_NTE.md', 'BETTERGI.md', 'MARCH7.md')}


def stage(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_dir() or output.exists() or output == source:
        raise ValueError('output must be a new directory')
    # A nested output would be traversed while building the candidate.
    if output.is_relative_to(source):
        raise ValueError('output must be outside the source tree')
    output.mkdir(parents=True)
    copied = []
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        destination = relative
        parts = relative.parts
        if parts[0] not in TOP_FILES | TOP_DIRS:
            continue
        if parts[0] == 'docs':
            document = '/'.join(parts[1:])
            replacements = {value: key for key, value in PUBLIC_DOC_REPLACEMENTS.items()}
            if document in replacements:
                destination = Path('docs') / replacements[document]
            elif document in PUBLIC_DOC_REPLACEMENTS:
                # A clean exported repository must remain exportable. Only the
                # explicitly marked public summary may use the stable path when
                # no private-worktree replacement source is present.
                if ((source / 'docs' / PUBLIC_DOC_REPLACEMENTS[document]).exists()
                        or not path.is_file()
                        or path.read_bytes().splitlines()[:1] != [b'<!-- gachaops-public-evidence-v1 -->']):
                    continue
            elif document not in PUBLIC_DOCS and not document.startswith(MATERIAL_DIRS):
                continue
        if any(p.lower() in PRIVATE_DIRS for p in parts) or 'superpowers' in parts:
            continue
        if parts[:3] == ('gachaops', 'assets', 'icons'):
            continue  # Unknown game-art redistribution basis; text badges remain.
        if path.name in INTERNAL_DOCS or path.name.lower() in PRIVATE_FILES:
            continue
        if path.name.lower().startswith('local') and path.suffix.lower() == '.json':
            continue
        if path.is_symlink():
            raise ValueError('source contains symlink')
        if not path.is_file():
            continue
        target = output / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        # Human documentation may contain legacy user paths and discussion URLs.
        # Executable sources are never rewritten; a match there blocks export.
        material = parts[0] == 'docs' and '/'.join(parts[1:]).startswith(MATERIAL_DIRS)
        if path.suffix.lower() in {'.md', '.html'} and not material:
            text = data.decode('utf-8-sig')
            text = re.sub(r'(?i)[a-z]:[\\/]+users[\\/]+[^\s`"<>\)]+', '<private-path>', text)
            text = re.sub(r'(?i)/(?:users|home)/[^\s`"<>\)]+', '<private-path>', text)
            text = re.sub(r'(?i)(?:chatgpt(?:-conversation)?://|https://chatgpt\.com/(?:c|share)/|codex://threads/)[^\s\)<>]+',
                          'private-discussion-omitted', text)
            data = text.encode('utf-8')
        target.write_bytes(data)
        copied.append(destination.as_posix())
    findings = scan_root(output)
    matrix = output / 'gachaops/assets/support_matrix.json'
    if matrix.is_file():
        records = json.loads(matrix.read_text(encoding='utf-8-sig'))
        for row in records.get('adapters', []):
            for reference in row.get('evidence', []):
                if (not isinstance(reference, str) or not reference.startswith('docs/')
                        or '..' in Path(reference).parts or not (output / reference).is_file()):
                    findings.append({'path': 'gachaops/assets/support_matrix.json',
                                     'rule': 'MISSING_COMPATIBILITY_EVIDENCE'})
    return {'status': 'blocked' if findings else 'passed', 'files': copied, 'findings': findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = stage(args.source, args.output)
    except (ValueError, OSError, UnicodeError):
        report = {'status': 'blocked', 'findings': [{'path': '.', 'rule': 'EXPORT_FAILED'}]}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
