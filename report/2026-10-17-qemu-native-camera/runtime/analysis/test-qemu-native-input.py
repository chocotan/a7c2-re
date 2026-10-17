#!/usr/bin/env python3
"""Exercise the compiled QEMU input device via QMP, without Sony firmware."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from menu_live import connect_in_run

BASE = Path(__file__).resolve().parent
binary = Path(os.environ.get('QEMU_TEST_BINARY', str(BASE.parent / 'sources/qemu-11.1.2/build-sony-ui/qemu-system-aarch64')))
with tempfile.TemporaryDirectory(prefix='a7c2-qemu-input-') as temporary:
    out = Path(temporary)
    command = ['bwrap', '--unshare-all', '--die-with-parent', '--ro-bind', '/usr', '/usr',
               '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib', '/lib64',
               '--ro-bind', str(binary), '/qemu', '--bind', str(out), '/out',
               '--dev', '/dev', '--proc', '/proc', '/qemu', '-machine', 'virt',
               '-cpu', 'cortex-a35', '-m', '128M', '-nodefaults', '-nic', 'none',
               '-display', 'none', '-monitor', 'none', '-serial', 'none',
               '-device', 'loader,addr=0x40000000,data=0x14000000,data-len=4,cpu-num=0',
               '-device', 'ramfb,id=a7c2-screen',
               '-chardev', 'socket,id=keys,path=/out/input.sock,server=on,wait=off',
               '-device', 'sony-ui-input,chardev=keys,display=a7c2-screen',
               '-qmp', 'unix:/out/qmp.sock,server=on,wait=off', '-S']
    with (out / 'qemu.log').open('wb') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 5
            while not (out / 'qmp.sock').exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError((out / 'qemu.log').read_text())
                time.sleep(.02)
            with socket.socket(socket.AF_UNIX) as qmp, socket.socket(socket.AF_UNIX) as keys:
                qmp.settimeout(3)
                keys.settimeout(.15)
                connect_in_run(qmp, out, 'qmp.sock')
                connect_in_run(keys, out, 'input.sock')
                with qmp.makefile('rwb') as stream:
                    assert 'QMP' in json.loads(stream.readline())

                    def execute(name, arguments=None):
                        message = {'execute': name}
                        if arguments:
                            message['arguments'] = arguments
                        stream.write(json.dumps(message).encode() + b'\n')
                        stream.flush()
                        while True:
                            reply = json.loads(stream.readline())
                            if 'event' in reply:
                                continue
                            assert 'return' in reply, reply
                            return reply['return']

                    def key(code, down):
                        execute('input-send-event', {'device': 'a7c2-screen', 'events': [
                            {'type': 'key', 'data': {'down': down, 'key': {'type': 'qcode', 'data': code}}}]})

                    execute('qmp_capabilities')
                    execute('cont')
                    for code, wire in [('m', b'M'), ('esc', b'M'), ('ret', b'E'),
                                       ('up', b'U'), ('down', b'D'), ('left', b'L'),
                                       ('right', b'R'), ('delete', b'X'),
                                       ('pgup', b'<'), ('pgdn', b'>'), ('f12', b'S')]:
                        key(code, True)
                        assert keys.recv(1) == wire, code
                        key(code, True)
                        try:
                            data = keys.recv(1)
                        except socket.timeout:
                            pass
                        else:
                            raise AssertionError(('repeated hold produced action', code, data))
                        key(code, False)
                    key('a', True)
                    key('a', False)
                    try:
                        data = keys.recv(1)
                    except socket.timeout:
                        pass
                    else:
                        raise AssertionError(('unknown key produced action', data))
                    execute('quit')
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
print('QEMU_NATIVE_INPUT_PASS: actual QMP/device/socket route, held suppression, unknown keys, clean quit')
