"""Check local Markdown/HTML document targets against an exact artifact file set."""
import argparse
import html
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
from urllib.parse import unquote, urlsplit

MARKDOWN = re.compile(r'!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+[^)\n]*)?\)')
REFERENCES = re.compile(r'(?m)^ {0,3}\[[^\]\n]+\]:[ \t]*(?:\r?\n[ \t]*)?(?:<([^>\n]+)>|([^\s]+))')

PACKAGE_DOCUMENTS = {'PACKAGE_README.md': 'README.md',
                     'PACKAGE_THIRD_PARTY_NOTICES.md': 'THIRD_PARTY_NOTICES.md',
                     'PACKAGE_MICROSOFT_CRT_TERMS.md': 'MICROSOFT_CRT_TERMS.md'}
DOCUMENT_DELIVERY = {name: name for name in (
    'LICENSE', 'SECURITY.md', 'docs/QUICKSTART.md', 'docs/SUPPORT_MATRIX.md',
    'docs/USAGE_NOTICE.md', 'docs/DATA_DIRECTORIES.md', 'docs/ADDING_ADAPTER.md',
    'docs/VALIDATION.md', 'docs/THIRD_PARTY_ASSISTANTS.md')}
DOCUMENT_DELIVERY.update(PACKAGE_DOCUMENTS)
SUPPLIER_PREFIXES = ('docs/release/dependency-licenses/', 'docs/release/source-delivery/',
                     'THIRD_PARTY_LICENSES/', 'SOURCE_DELIVERY/')


class HtmlLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.targets = []

    def handle_starttag(self, tag, attrs):
        self.targets.extend(value for name, value in attrs if name in {'href', 'src'} and value is not None)


def document_targets(text):
    # Code examples are not rendered links. Preserve newlines for reference definitions.
    lines, fence = [], None
    for line in text.splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append('')
        else:
            lines.append('' if fence else line)
    text = re.sub(r'(`+).*?\1', '', '\n'.join(lines), flags=re.S)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)
    parser = HtmlLinks()
    parser.feed(text)
    targets = [a or b for a, b in MARKDOWN.findall(text)]
    # Definitions cover full, collapsed and shortcut Markdown references.
    targets += [a or b for a, b in REFERENCES.findall(text)]
    return targets + parser.targets


def check_links(files, documents=None):
    """files maps canonical POSIX relative names to bytes; never fetch network URLs."""
    findings = []
    for name in sorted(documents if documents is not None else files):
        if PurePosixPath(name).suffix.lower() not in {'.md', '.html'}:
            continue
        text = files[name].decode('utf-8-sig')
        targets = document_targets(text)
        for raw in targets:
            target = html.unescape(raw)
            parts = urlsplit(target)
            if parts.scheme in {'http', 'https', 'mailto'} or target.startswith('//'):
                continue
            if not parts.path and not parts.scheme:
                continue
            path = unquote(parts.path)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), path))
            if (parts.scheme or path.startswith('/') or '\\' in path or ':' in path
                    or resolved == '..' or resolved.startswith('../') or resolved not in files):
                findings.append({'path': name, 'rule': 'LOCAL_LINK_MISSING_OR_EXTERNAL'})
    return findings


def check_product_links(files):
    """Check source pages in source layout and explicit templates after delivery mapping."""
    documents = [name for name in files if name not in PACKAGE_DOCUMENTS
                 and not name.startswith(SUPPLIER_PREFIXES)]
    findings = check_links(files, documents)
    templates = {source: target for source, target in PACKAGE_DOCUMENTS.items() if source in files}
    if templates:
        delivered = {target: files[source] for source, target in DOCUMENT_DELIVERY.items() if source in files}
        for source, target in (('docs/release/dependency-licenses/', 'THIRD_PARTY_LICENSES/'),
                               ('docs/release/source-delivery/', 'SOURCE_DELIVERY/')):
            delivered.update({target + name[len(source):]: data for name, data in files.items()
                              if name.startswith(source)})
        delivered.update({target: files[source] for source, target in templates.items()})
        for source, target in templates.items():
            findings.extend(dict(finding, path=source) for finding in check_links(delivered, [target]))
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    try:
        from check_release_binary import check_tree
        check_tree(args.root)
        files = {p.relative_to(args.root).as_posix(): p.read_bytes()
                 for p in args.root.rglob('*') if p.is_file()}
        findings = check_product_links(files)
    except (ValueError, OSError, UnicodeError):
        findings = [{'path': '.', 'rule': 'LINK_CHECK_FAILED'}]
    print(json.dumps({'status': 'blocked' if findings else 'passed', 'findings': findings}))
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
