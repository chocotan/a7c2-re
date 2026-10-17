"""Validate a snapshot exported from one immutable guest inode, never live VRAM."""
import base64
import hashlib
import json
import re
import struct

HEADER = 128
METADATA = 80 + 256 * 96 + 16 + 258
PIXELS = 640 * 480 * 2


def decode_export(console):
    lines = console.replace(b'\r\n', b'\n').splitlines()
    start = lines.index(b'HEADLESS_OSD_BEGIN') + 1
    end = lines.index(b'HEADLESS_OSD_END', start)
    transport = b'\n'.join(lines[start:end]) + b'\n'
    # printk may interrupt even one tty write; retain every byte outside known kernel records.
    transport, noise = re.subn(rb'\[\s*\d+\.\d+\] (?:UDIF:|\[K\])[^\n]*\n', b'', transport)
    # Our own bounded progress records share the same console at power-of-two sequences.
    transport, observer_noise = re.subn(
        rb'OBS OSD_HEADLESS_(?:PERSISTED seq=\d+ pixel_source_seq=\d+ id=0x[0-9a-f]+ '
        rb'phys=0x[0-9a-f]+ geometry=640x480 bytes=614400 fnv1a64=[0-9a-f]{16} '
        rb'mode=AP-RGBA4444-experimental path=/tmp/headless-osd-frame\.bin|'
        rb'ACK seq=\d+ persisted AP snapshot consumed; CP composition/panel unimplemented)\n',
        b'', transport)
    raw = b''.join(base64.b64decode(line, validate=True) for line in transport.splitlines() if line)
    if len(raw) != HEADER + METADATA + PIXELS or raw[:8] != b'HOSDAP02':
        raise ValueError('invalid snapshot magic/length')
    seq, ident, width, height, fmt, phys, expected, pixels, metadata, command = struct.unpack_from('<QIIIIQQIII', raw, 8)
    if (width, height, fmt, pixels, metadata) != (640, 480, 1, PIXELS, METADATA):
        raise ValueError('unsupported snapshot geometry/format')
    if not seq or command not in (0x1003, 0x1005) or not 0x410000000 <= phys <= 0x500000000 - PIXELS:
        raise ValueError('invalid snapshot identity')
    pixel_source_seq, = struct.unpack_from('<Q', raw, 120)
    if not 0 < pixel_source_seq <= seq or (command == 0x1003 and pixel_source_seq != seq):
        raise ValueError('invalid retained pixel source sequence')
    value = 14695981039346656037
    for byte in raw[:40] + bytes(8) + raw[48:]:
        value = ((value ^ byte) * 1099511628211) & ((1 << 64) - 1)
    if value != expected:
        raise ValueError('snapshot header/metadata/pixel checksum mismatch')
    desc = raw[64:100]
    if struct.unpack_from('<I', desc, 8)[0] != ident or struct.unpack_from('<HH', desc, 20) != (width, height):
        raise ValueError('request and snapshot identity differ')
    if struct.unpack_from('<H', desc)[0] != 0x1003 or struct.unpack_from('<H', desc, 16)[0] != 0:
        raise ValueError('unsupported retained descriptor')
    if struct.unpack_from('<H', raw, 100)[0] != 0x1002 or struct.unpack_from('<I', raw, 108)[0] != command:
        raise ValueError('invalid frozen/update pairing')
    if command == 0x1005 and struct.unpack_from('<H', raw, 112)[0] != 0x1005:
        raise ValueError('invalid metadata update body')
    return raw, dict(sequence=seq, pixel_source_seq=pixel_source_seq, buffer_id=ident, physical=hex(phys), width=width,
                     height=height, row_pixels=width, bmp_format=fmt, update_command=hex(command),
                     fnv1a64=f'{expected:016x}', sha256=hashlib.sha256(raw).hexdigest(),
                     metadata_bytes=metadata, pixel_bytes=pixels, descriptor_hex=desc.hex(),
                     fix_hex=raw[100:112].hex(), metadata_update_hex=raw[112:120].hex(),
                     mode='AP software consumption; experimental RGBA4444; no CP composition or panel',
                     source='single guest snapshot inode, persisted/read-back before completion send',
                     persistence='current guest run only (/tmp may be tmpfs)',
                     console_kernel_records_removed=noise,
                     console_headless_records_removed=observer_noise,
                     checksum_scope='entire container with checksum field zeroed',
                     ack_delivery_proven=False)


def capture(console, directory):
    raw, info = decode_export(console)
    (directory / 'headless-osd-frame.bin').write_bytes(raw)
    pixels = raw[HEADER + METADATA:]
    (directory / 'headless-osd-pixels.bin').write_bytes(pixels)
    (directory / 'headless-osd-frame.json').write_text(json.dumps(info, indent=2) + '\n')
    return info
