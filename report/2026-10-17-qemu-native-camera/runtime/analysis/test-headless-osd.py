#!/usr/bin/env python3
"""Check exported bytes, integrity rejection and default-off CLI guard without a VM."""
from pathlib import Path
import base64
import ast
import os
import subprocess
import sys
from headless_osd_capture import capture, decode_export, HEADER, METADATA, PIXELS

directory = Path(sys.argv[1])
console = (directory / 'export.log').read_bytes()
raw, info = decode_export(console)
assert raw == (directory / 'frame.bin').read_bytes()
assert raw[HEADER + METADATA:] == bytes((i * 37 + 9) & 255 for i in range(PIXELS))
assert info['buffer_id'] == 0x402 and info['sequence'] == 2 and info['pixel_source_seq'] == 1
assert info['update_command'] == '0x1005'
assert info['console_kernel_records_removed'] == 0
start = console.index(b'HEADLESS_OSD_BEGIN\n') + len(b'HEADLESS_OSD_BEGIN\n')
for offset in (start + 17, console.index(b'\n', start)):
    noisy = console[:offset] + b'[  487.287849] UDIF: cdev_ioctl warning ret = -1020\n[  487.301211] [K]**UIPC ERROR** test\n' + console[offset:]
    restored, noisy_info = decode_export(noisy)
    assert restored == raw and noisy_info['console_kernel_records_removed'] == 2
    observer = (b'OBS OSD_HEADLESS_PERSISTED seq=512 pixel_source_seq=479 id=0x401 '
                b'phys=0x438a16100 geometry=640x480 bytes=614400 fnv1a64=0b96bffb38081455 '
                b'mode=AP-RGBA4444-experimental path=/tmp/headless-osd-frame.bin\n'
                b'OBS OSD_HEADLESS_ACK seq=512 persisted AP snapshot consumed; CP composition/panel unimplemented\n')
    restored, noisy_info = decode_export(console[:offset] + observer + console[offset:])
    assert restored == raw and noisy_info['console_headless_records_removed'] == 2
try:
    decode_export(console[:start + 17] + b'UNKNOWN!\n' + console[start + 17:])
except ValueError:
    pass
else:
    raise AssertionError('unknown transport corruption accepted')
capture(console, directory)
assert (directory / 'headless-osd-pixels.bin').stat().st_size == PIXELS
corrupt = bytearray(raw)
corrupt[-1] ^= 1
bad_checksum = b'HEADLESS_OSD_BEGIN\n' + base64.b64encode(corrupt) + b'\nHEADLESS_OSD_END\n'
for broken in (console.replace(b'HEADLESS_OSD_END', b'MISSING_END'), bad_checksum):
    try:
        decode_export(broken)
    except (ValueError, IndexError):
        pass
    else:
        raise AssertionError('corrupt export accepted')
for offset in (8, 16, 32, 64, 120):
    corrupt = bytearray(raw); corrupt[offset] ^= 1
    broken = b'HEADLESS_OSD_BEGIN\n' + base64.b64encode(corrupt) + b'\nHEADLESS_OSD_END\n'
    try:
        decode_export(broken)
    except ValueError:
        pass
    else:
        raise AssertionError('corrupt header accepted')
probe = Path(__file__).with_name('qemu-menu-probe.py')
# Execute only parser construction/parse_args, stopping before filesystem or VM setup.
tree = ast.parse(probe.read_text())
done_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'diagnostic_done')
import re
done_namespace = {'re': re}
exec(compile(ast.Module(body=[done_node], type_ignores=[]), str(probe), 'exec'), done_namespace)
done = done_namespace['diagnostic_done']
marker = 'MENU_DIAG_COMMAND_DONE_1'
assert not done(b"\n/ # \nprintf '\\nMENU_DIAG_COMMAND_DONE_1\\n'\n/ # \n", marker)
assert not done(b'\nMENU_DIAG_COMMAND_DONE_0\n', marker)
assert done(b'\r\nMENU_DIAG_COMMAND_DONE_1\r\n', marker)
assert done(b'\nMENU_DIAG_COMMAND_[  3.14] UDIF: noise\nDONE_1\n', marker)
end = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign)
           and any(isinstance(t, ast.Name) and t.id == 'args' for t in node.targets))
parser_code = compile(ast.Module(body=tree.body[:end + 1], type_ignores=[]), str(probe), 'exec')
original_argv, original_env = sys.argv, os.environ.get('MENU_HEADLESS_OSD')
try:
    for value, enabled in ((None, False), ('0', False), ('yes', False), ('1', True)):
        if value is None:
            os.environ.pop('MENU_HEADLESS_OSD', None)
        else:
            os.environ['MENU_HEADLESS_OSD'] = value
        sys.argv = [str(probe)]
        namespace = {}; exec(parser_code, namespace)
        assert namespace['args'].headless_osd is enabled
finally:
    sys.argv = original_argv
    if original_env is None:
        os.environ.pop('MENU_HEADLESS_OSD', None)
    else:
        os.environ['MENU_HEADLESS_OSD'] = original_env
result = subprocess.run([sys.executable, str(probe), '--headless-osd'], capture_output=True)
assert result.returncode == 2 and b'--headless-osd requires' in result.stderr
print('HEADLESS_OSD_HOST_CAPTURE_PASS')
