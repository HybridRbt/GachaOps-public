"""Bind delivered application source bytes to the exact candidate build identity."""
import hashlib
import io
import json
from pathlib import PurePosixPath
import zipfile

from check_release_binary import _unique_object, canonical_path, valid_hash

DELIVERY = 'SOURCE_DELIVERY/APPLICATION_SOURCE_DELIVERY.json'
BUNDLE = 'SOURCE_DELIVERY/SOURCE_BUNDLE_MANIFEST.json'
REQUIRED_INPUTS = {'GachaOps.spec', 'pyproject.toml', 'requirements-desktop.txt',
                   'scripts/desktop_entry.py', 'gachaops/__init__.py',
                   'gachaops/__main__.py', 'gachaops/desktop.py', 'gachaops/maagf2.py',
                   'gachaops/assets/support_matrix.json'}


def decode(data):
    value = json.loads(data, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError('invalid application source metadata')
    return value


def verify_application_source(files, candidate):
    try:
        delivery = decode(files[DELIVERY])
        commit = candidate['candidate_code_commit']
        executable = candidate['candidate_executable_sha256']
        if (not valid_hash(commit, 40) or not valid_hash(executable)
                or delivery.get('schema_version') != 1
                or delivery.get('candidate_code_commit') != commit
                or delivery.get('candidate_executable_sha256') != executable):
            raise ValueError('application source candidate identity mismatch')
        row = delivery['archive']
        bundle = decode(files[BUNDLE])
        applications = [r for r in bundle.get('archives', []) if r.get('scope') == 'application_source_and_build_inputs']
        if len(applications) != 1 or any(applications[0].get(k) != row.get(k) for k in ('source', 'file', 'sha256', 'bytes', 'scope')):
            raise ValueError('application source missing from archive manifest')
        name = canonical_path(row['file']).as_posix()
        if (len(PurePosixPath(name).parts) != 1 or not name.endswith('.zip')
                or row.get('scope') != 'application_source_and_build_inputs'
                or row.get('source') != 'docs/release/source-delivery/archives/' + name):
            raise ValueError('invalid application source archive declaration')
        data = files['SOURCE_DELIVERY/archives/' + name]
        if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise ValueError('application source archive changed')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if (len(names) != len(set(names)) or not 0 < len(names) <= 5000
                    or sum(r.file_size for r in archive.infolist()) > 50_000_000
                    or archive.testzip() is not None):
                raise ValueError('invalid application source ZIP')
            for path in names:
                canonical_path(path)
            manifest = decode(archive.read('SOURCE_MANIFEST.json'))
            if manifest.get('schema_version') != 1 or manifest.get('candidate_code_commit') != commit:
                raise ValueError('embedded application source commit mismatch')
            rows = manifest.get('git_input_files')
            if not isinstance(rows, list) or not rows:
                raise ValueError('missing application build inputs')
            seen = set()
            for entry in rows:
                path = canonical_path(entry['path']).as_posix()
                if path in seen or not valid_hash(entry['sha256']):
                    raise ValueError('invalid application source input')
                seen.add(path)
                content = archive.read(path)
                if len(content) != entry['bytes'] or hashlib.sha256(content).hexdigest() != entry['sha256']:
                    raise ValueError('application source input hash mismatch')
                blob = entry.get('git_blob')
                if (not valid_hash(blob, 40) or hashlib.sha1(
                        b'blob ' + str(len(content)).encode('ascii') + b'\0' + content).hexdigest() != blob):
                    raise ValueError('application source Git blob mismatch')
            supplements = manifest.get('supplements', [])
            if (not isinstance(supplements, list) or not all(isinstance(n, str) for n in supplements)
                    or not set(supplements) <= {'README.md', 'SOURCE_MANIFEST.json'}
                    or not REQUIRED_INPUTS <= seen
                    or set(names) != seen | set(supplements) | {'SOURCE_MANIFEST.json'}):
                raise ValueError('application source inventory mismatch')
            if (delivery.get('original_git_inputs') != len(rows)
                    or delivery.get('archive_entries') != len(names)):
                raise ValueError('application source count mismatch')
            matrix = '_internal/gachaops/assets/support_matrix.json'
            if matrix in files and files[matrix] != archive.read('gachaops/assets/support_matrix.json'):
                raise ValueError('application source runtime resource mismatch')
        return {'candidate_code_commit': commit, 'source_inputs': len(rows), 'source_entries': len(names)}
    except (KeyError, TypeError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ValueError('missing or invalid application source delivery') from exc
