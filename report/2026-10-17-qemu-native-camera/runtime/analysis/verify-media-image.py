#!/usr/bin/env python3
"""Verify a disk image actually written/read by original Fsys in the isolated test."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / 'vendor/fwtool'))
from fwtool.archive.fat import readFat

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('run', type=Path)
args = parser.parse_args()
log = (args.run / 'console.log').read_text(errors='replace')
if not all(f'FSYS_FILE_TEST_DONE result=0 verify_only={i}' in log for i in (0, 1)):
    parser.error('both original-Fsys processes must complete successfully')
if not re.search(r'FILE_MEDIA_CONFIG call=1 EXISTMEDIADRIVER=\d+->0 OMIT_WRITE=0', log):
    parser.error('file backend / actual writes were not verified')
expected = re.findall(r'FSYS_IMAGE_EXPORTED physical=0x41e600000 bytes=4194304 fnv1a64=([0-9a-f]{16})', log)
image = args.run / 'media-disk.img'
raw = image.read_bytes()
if len(raw) != 0x400000 or len(expected) != 1:
    parser.error('image export size/identity differs')
value = 14695981039346656037
for byte in raw:
    value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
if value != int(expected[0], 16):
    parser.error('host disk image differs from the guest read-back export')
if not shutil.which('fsck.fat'):
    parser.error('fsck.fat is required for independent filesystem validation')
checked = subprocess.run(['fsck.fat', '-n', str(image)], capture_output=True, text=True)
(args.run / 'media-fsck.txt').write_text(checked.stdout + checked.stderr)
if checked.returncode:
    parser.error('filesystem validation failed; see media-fsck.txt')
with image.open('rb') as stream:
    files = {entry.path: entry.contents.read() for entry in readFat(stream) if stat.S_ISREG(entry.mode)}
payload = bytes((i * 37 + 9) & 255 for i in range(4096))
if files.get('/QTEST/PROBE.BIN') != payload or files.get('/SEED.TXT') != b'A7C2 virtual file-media fixture\n':
    parser.error('original-Fsys file content differs')
report = {'check': 'ORIGINAL_FSYS_DISK_PASS', 'disk_sha256': hashlib.sha256(raw).hexdigest(),
          'file_sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload),
          'files': {name: len(data) for name, data in files.items()},
          'scope': 'original Fsys write/flush/remount/read and host disk export; not a photograph or video'}
(args.run / 'media-result.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
