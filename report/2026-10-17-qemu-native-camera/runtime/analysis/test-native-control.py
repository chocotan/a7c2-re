#!/usr/bin/env python3
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile

from menu_live import KEYS, SINGLE_STATUS, connect_in_run, key_command


with tempfile.TemporaryDirectory(prefix='a7c2-control-') as temporary:
    root = Path(temporary)
    long_run = root / ('nested-' * 18)
    long_run.mkdir()
    assert len(str(long_run / 'input.sock')) > 108
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
        fd = os.open(long_run, os.O_RDONLY | os.O_DIRECTORY)
        try:
            server.bind(f'/proc/self/fd/{fd}/input.sock')
        finally:
            os.close(fd)
        server.listen()
        connect_in_run(client, long_run, 'input.sock')
        with server.accept()[0] as accepted:
            client.sendall(b'M')
            assert accepted.recv(1) == b'M'

    fake = root / 'inject key'
    fake.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$INJECT_TEST_LOG"
case "$INJECT_TEST_FAIL:$5" in press:1|release:0) exit 7;; esac
exit 0
''')
    fake.chmod(0o700)
    for failure in ('none', 'press', 'release'):
        for name, code in KEYS.items():
            if name in SINGLE_STATUS:
                continue
            log = root / 'injections.txt'
            log.write_text('')
            command = key_command(name).replace('/simulation/inject-key', shlex.quote(str(fake)))
            result = subprocess.run(['bash', '-c', command], env=dict(os.environ,
                INJECT_TEST_LOG=str(log), INJECT_TEST_FAIL=failure))
            assert (result.returncode == 0) == (failure == 'none'), (name, failure)
            assert log.read_text().splitlines() == [
                f'0x807a0020 0x92840747 {code} 0 1 0 1',
                f'0x807a0020 0x92840747 {code} 0 0 0 1'], (name, failure)
    for name, status in SINGLE_STATUS.items():
        log.write_text('')
        command = key_command(name).replace('/simulation/inject-key', shlex.quote(str(fake)))
        result = subprocess.run(['bash', '-c', command], env=dict(os.environ,
            INJECT_TEST_LOG=str(log), INJECT_TEST_FAIL='none'))
        assert result.returncode == 0
        assert log.read_text().splitlines() == [f'0x807a0020 0x92840747 {KEYS[name]} 0 {status} 0 1']
    try:
        key_command('MENU; arbitrary shell')
    except KeyError:
        pass
    else:
        raise AssertionError('unknown key accepted')

print('NATIVE_CONTROL_PASS: long Unix paths, verified taps, release on press failure, strict keys')
