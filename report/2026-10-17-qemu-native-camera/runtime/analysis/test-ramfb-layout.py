"""Check the chosen virtual pool against the local configuration; no VM boot."""
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import os

base = Path(__file__).resolve().parent
start, end = 0x41e100000, 0x41e600000
kemco = base / 'unpacked/firmware.tar_unpacked/0800_appli/setting/kemco.txt'
ranges = [(int(addr, 16), int(size, 16)) for size, addr in
          re.findall(r'memrsv=(0x[0-9a-fA-F]+)@(0x[0-9a-fA-F]+)', kemco.read_text())]
assert all(addr + size <= start or addr >= end for addr, size in ranges)
assert 0x41e000000 + 0x100000 == start  # dmm-service.h's two virtual CP slots
assert 0x410268000 + 0x7c000 < start  # monitor, loader and capid workspace
assert 0x405000000 + 0x1000000 < start  # ulogio workspace
cfg = base / 'menu-root/seed-setting/DmmCfg.bin'
raw = cfg.read_bytes()
# Nine packed top-level extents: u32 addr-low,u16 record-size,u16 addr-high,
# u32 id,u32 length,u32 flags. Bound interpretation to this inspected file.
assert len(raw) == 65956
extents = []
for offset in range(0x50, 0x104, 20):
    low, record, high, ident, length, flags = struct.unpack_from('<IHHIII', raw, offset)
    assert record == 20 and high == 4 and flags == 1 and length
    addr = (high << 32) | low
    assert addr >= end or addr + length <= start
    assert any(lo <= addr and addr + length <= lo + size for lo, size in ranges)
    extents.append(dict(offset=hex(offset), start=hex(addr), end=hex(addr + length), id=hex(ident)))
assert extents[0]['start'] == '0x424000000' and extents[-1]['end'] == '0x500000000'
cp_cfg = base / 'menu-root/cp/DmmCfg.bin'
assert hashlib.sha256(cp_cfg.read_bytes()).hexdigest() == 'd3466cee62fd86d927236a2c6c37dd633f76cdc181b4ad07a9863696aab76660'
evidence = dict(pool=[hex(start), hex(end)], kemco=str(kemco),
                kemco_sha256=hashlib.sha256(kemco.read_bytes()).hexdigest(),
                ap_cfg=str(cfg), ap_cfg_sha256=hashlib.sha256(raw).hexdigest(),
                ap_top_level_extents=extents,
                cp_model='dmm-service.h: [0x41e000000,0x41e100000); no real CP execution')
(base / 'ramfb-output/check/memory-layout.json').write_text(json.dumps(evidence, indent=2) + '\n')
# Invalid displays fail before creating output directories or starting QEMU.
env = dict(os.environ, MENU_NATIVE_WINDOW='1', QEMU_NATIVE_DISPLAY='none; false')
result = subprocess.run(['bash', str(base / 'qemu-kernel-probe.sh')], env=env, capture_output=True)
assert result.returncode == 2 and b'QEMU_NATIVE_DISPLAY must be' in result.stderr
result = subprocess.run(['python3', str(base / 'qemu-menu-probe.py'), '--native-display', 'none'],
                        env=dict(os.environ, MENU_HEADLESS_OSD='0', MENU_NATIVE_WINDOW='0'), capture_output=True)
assert result.returncode == 2 and b'--native-display requires' in result.stderr
print('RAMFB_LAYOUT_PASS: kemco, nine AP DmmCfg extents, CP/native workspaces and invalid launch flags')
