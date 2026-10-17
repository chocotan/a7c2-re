#!/usr/bin/env python3
"""Create a small isolated FAT fixture; never format or open a physical device."""
import io
import os
from pathlib import Path
import stat
import sys
import time

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / 'vendor/fwtool'))
from fwtool.archive import UnixFile
from fwtool.archive.fat import readFat, writeFat

os.environ['TZ'] = 'CST-8'
time.tzset()
marker = b'A7C2 virtual file-media fixture\n'
image = io.BytesIO()
writeFat([UnixFile('/SEED.TXT', len(marker), 315532800, stat.S_IFREG | 0o644,
                   0, 0, io.BytesIO(marker))], 4 * 1024 * 1024, image)
image.seek(43)
image.write(b'NO NAME    ')  # Standard unlabelled FAT volume; fwtool leaves an invalid blank label.
image.seek(0)
assert {entry.path: entry.contents.read() for entry in readFat(image)} == {'/SEED.TXT': marker}
target = BASE / 'menu-root/simulation/media-template.fat'
target.parent.mkdir(exist_ok=True)
temporary = target.with_suffix('.new')
with temporary.open('xb') as output:
    output.write(image.getvalue())
temporary.replace(target)
print('MEDIA_FIXTURE_PASS', target, len(image.getvalue()))
