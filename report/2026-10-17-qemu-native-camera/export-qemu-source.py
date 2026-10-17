#!/usr/bin/env python3
"""Maintain the source handoff: audited base tar + complete local source patch."""
import argparse
import difflib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

BASE_SHA = '731b5681e4bb18be313231579b8efd0296c5b015fa36dc533874b639ba838016'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    args = parser.parse_args()
    if digest(args.base) != BASE_SHA:
        parser.error('QEMU 11.1.2 source archive identity differs')
    out = Path(__file__).resolve().parent / 'source'
    out.mkdir(exist_ok=True)
    known, changes, before = set(), {}, {}
    with tarfile.open(args.base) as archive:
        for item in archive:
            parts = Path(item.name).parts
            if len(parts) < 2 or '..' in parts or item.name.startswith('/'):
                continue
            name = Path(*parts[1:]).as_posix()
            known.add(name)
            if not item.isfile():
                continue
            path = args.source / name
            original = archive.extractfile(item).read()
            if not path.is_file():
                raise ValueError('Base file missing from source tree: ' + name)
            current = path.read_bytes()
            if current != original:
                before[name], changes[name] = original, current
    for directory, dirs, files in os.walk(args.source):
        dirs[:] = [d for d in dirs if not d.startswith('build') and
                   d not in ('.git', '__pycache__', 'pyvenv') and not d.endswith('.egg-info')
                   and Path(directory)/d != args.source/'subprojects/dtc']
        for leaf in files:
            path = Path(directory) / leaf
            name = path.relative_to(args.source).as_posix()
            if name == 'subprojects/.wraplock':
                continue  # Meson's runtime lock has no corresponding source content.
            if name not in known and (path.suffix in ('.c', '.h', '.S', '.py', '.json', '.md', '.txt', '.build')
                                      or leaf in ('Kconfig', 'Makefile', 'meson.build')):
                before[name], changes[name] = b'', path.read_bytes()
            elif name not in known:
                raise ValueError('Unclassified new source-tree file; review before export: ' + name)
    patch = ''.join(''.join(difflib.unified_diff(
        before[name].decode('utf-8').splitlines(True), data.decode('utf-8').splitlines(True),
        fromfile='a/' + name if before[name] else '/dev/null', tofile='b/' + name))
        for name, data in sorted(changes.items()))
    # Apply to the affected original files, then compare every resulting byte.
    with tempfile.TemporaryDirectory(prefix='a7c2-patch-check-') as temporary:
        root = Path(temporary)
        for name, data in before.items():
            if data:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        subprocess.run(['patch', '--batch', '-p1', '-d', str(root)], input=patch.encode(), check=True,
                       stdout=subprocess.DEVNULL)
        for name, data in changes.items():
            if (root / name).read_bytes() != data:
                raise ValueError('Source patch verification failed: ' + name)
    (out / 'qemu-sony-native.patch').write_text(patch)
    dtc = args.source / 'subprojects/dtc'
    if not (dtc / 'libfdt/libfdt.h').is_file():
        raise ValueError('Offline DTC dependency missing')
    dtc_files = {}
    with (out / 'dtc-sources.tar.gz').open('wb') as target, gzip.GzipFile(fileobj=target, mode='wb', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w') as archive:
            for path in sorted(dtc.rglob('*')):
                if not path.is_file() or path.is_symlink() or any(p in ('.git', '__pycache__') for p in path.parts):
                    continue
                if path.suffix in ('.o', '.a', '.d', '.pyc'):
                    continue
                if path.name in ('version_gen.h', '.meson-subproject-wrap-hash.txt') or path.name.endswith(('.tab.c', '.tab.h', '.lex.c')):
                    continue
                data = path.read_bytes()
                if data.startswith(b'\x7fELF'):
                    continue
                relative = path.relative_to(dtc)
                public = out / 'dtc' / relative
                public.parent.mkdir(parents=True, exist_ok=True)
                if public.exists() and public.read_bytes() != data:
                    raise ValueError('Public DTC source has independent changes: ' + str(relative))
                public.write_bytes(data)
                public.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
                dtc_files[relative.as_posix()] = hashlib.sha256(data).hexdigest()
                item = archive.gettarinfo(str(path), 'dtc/' + path.relative_to(dtc).as_posix())
                item.uid = item.gid = item.mtime = 0
                item.uname = item.gname = ''
                archive.addfile(item, io.BytesIO(data))
    manifest = {'base': 'qemu-11.1.2.tar.xz', 'base_sha256': BASE_SHA,
                'excluded_generated': ['build*/', '**/__pycache__/', '**/*.egg-info/', 'subprojects/.wraplock'],
                'patch_sha256': digest(out / 'qemu-sony-native.patch'),
                'dtc_sha256': digest(out / 'dtc-sources.tar.gz'),
                'dtc_source_files': dtc_files,
                'source_files': {name: {'before': hashlib.sha256(before[name]).hexdigest() if before[name] else None,
                                       'after': hashlib.sha256(data).hexdigest()}
                                 for name, data in sorted(changes.items())}}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('QEMU_SOURCE_PATCH_PASS', len(changes), 'files; offline DTC source included')


if __name__ == '__main__':
    main()
