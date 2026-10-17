#!/usr/bin/env python3
"""Compare a QMP ramfb screendump with the same run's verified AP container."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from PIL import Image
from headless_osd_capture import decode_export, HEADER, METADATA

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('run', type=Path)
args = parser.parse_args()
raw, info = decode_export((args.run / 'console.log').read_bytes())
if raw != (args.run / 'headless-osd-frame.bin').read_bytes():
    raise SystemExit('Saved container differs from the validated serial export')
expected = bytearray()
for (value,) in struct.iter_unpack('<H', raw[HEADER + METADATA:]):
    alpha = value & 15
    expected.extend((((value >> 12) * 17 * alpha + 7) // 15,
                     (((value >> 8) & 15) * 17 * alpha + 7) // 15,
                     (((value >> 4) & 15) * 17 * alpha + 7) // 15))
with Image.open(args.run / 'ramfb-screen.ppm') as screen:
    actual = screen.convert('RGB')
    if actual.size != (640, 480) or actual.tobytes() != expected:
        raise SystemExit('QMP/AP pixels differ; check source sequence and capture timing')
    actual.save(args.run / 'ramfb-screen.png')
    colors = len(actual.getcolors(640 * 480))
print(json.dumps({'check': 'RAMFB_PIXEL_MATCH_PASS', 'run': str(args.run),
                  'sequence': info['sequence'], 'pixel_source_seq': info['pixel_source_seq'],
                  'distinct_colors': colors, 'rgb_sha256': hashlib.sha256(expected).hexdigest()},
                 ensure_ascii=False))
