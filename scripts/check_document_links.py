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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    try:
        from check_release_binary import check_tree
        check_tree(args.root)
        files = {p.relative_to(args.root).as_posix(): p.read_bytes()
                 for p in args.root.rglob('*') if p.is_file()}
        # Supplier documents have their own upstream layout; only validate product docs.
        documents = [name for name in files if not name.startswith((
            'docs/release/dependency-licenses/', 'docs/release/source-delivery/',
            'THIRD_PARTY_LICENSES/', 'SOURCE_DELIVERY/'))]
        findings = check_links(files, documents)
    except (ValueError, OSError, UnicodeError):
        findings = [{'path': '.', 'rule': 'LINK_CHECK_FAILED'}]
    print(json.dumps({'status': 'blocked' if findings else 'passed', 'findings': findings}))
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
