#!/usr/bin/env python3
"""Host/mock tests only. No QEMU, builds, hardware or shared flock."""
import base64
import http.client
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from menu_control import BASE, Server, VM, terminate_owned
from menu_live import FirmwareFault, FirmwareMonitor, serial_command, snapshot_message


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('condition timed out')


def mock_vm(mode='normal'):
    return VM([sys.executable, str(BASE / 'mock_menu_vm.py'), mode])


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.vm = mock_vm()
        self.server = Server(self.vm)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.vm.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path='/api/state', method='GET', payload=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        base = {'X-Session-Token': self.server.token, 'Origin': self.server.origin,
                'Content-Type': 'application/json'}
        base.update(headers or {})
        base = {key: value for key, value in base.items() if value is not None}
        body = json.dumps(payload) if payload is not None else None
        conn.request(method, path, body, base)
        response = conn.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return result

    def test_auth_and_fixed_routes(self):
        self.assertEqual(self.request()[0], 200)
        for headers in ({'X-Session-Token': 'wrong'}, {'Origin':'http://evil.invalid'},
                        {'Origin':'null'}, {'Host':'evil.invalid'}, {'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.request(headers=headers)[0], 403)
            self.assertEqual(self.request('/api/start', 'POST', {}, headers)[0], 403)
        for path in ('/api/shell', '/../../etc/passwd', '/api/state?file=/etc/passwd'):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request('/api/start', 'POST', {'shell':'true'})[0], 400)
        self.assertEqual(self.request('/api/start', 'POST', {}, {'Content-Length':'257'})[0], 413)
        self.assertEqual(self.request('/api/start', 'POST', {}, {'Content-Type':'text/plain'})[0], 415)
        self.assertEqual(self.request('/api/start', 'POST', {}, {'Origin':None})[0], 403)
        self.assertEqual(self.request(headers={'X-Session-Token':'é'})[0], 403)
        self.assertEqual(self.request('/api/key', 'POST', {'key':'UP'})[0], 400)
        self.assertEqual(self.request('/api/key', 'POST', {'key':[]})[0], 400)
        self.assertEqual(self.request('/api/save')[0], 409)

    def test_state_queue_snapshot_and_stop(self):
        self.assertEqual(self.vm.status()['state'], 'disconnected')
        self.assertEqual(self.request('/api/start', 'POST', {})[0], 202)
        wait_for(lambda: self.vm.status()['snapshot'])
        self.assertEqual(self.vm.status()['state'], 'running')
        self.assertFalse(self.vm.status()['stale'])
        self.assertEqual(self.request('/api/start', 'POST', {})[0], 400)
        self.assertEqual(self.request('/api/key', 'POST', {'key':'MENU'})[0], 202)
        self.assertEqual(self.request('/api/snapshot', 'POST', {})[0], 202)
        wait_for(lambda: self.vm.status()['snapshot']['number'] == 2)
        events = self.vm.status()['events']
        self.assertEqual(events[1]['kind'], 'input')
        self.assertEqual(events[1]['message'], 'MENU：输入已提交')
        status, png, headers = self.request('/api/save')
        self.assertEqual(status, 200)
        self.assertTrue(png.startswith(b'\x89PNG'))
        self.assertEqual(headers['X-Snapshot-Number'], '2')
        self.assertEqual(self.request('/api/stop', 'POST', {})[0], 202)
        wait_for(lambda: self.vm.status()['state'] == 'exited')
        self.assertTrue(self.vm.status()['stale'])
        self.assertEqual(self.vm.png, png)
        self.assertEqual(self.request('/api/key', 'POST', {'key':'MENU'})[0], 400)

    def test_bad_reply_and_exit_do_not_succeed(self):
        for mode in ('bad-reply', 'exit'):
            vm = mock_vm(mode)
            try:
                vm.start()
                wait_for(lambda: vm.status()['state'] == 'error')
                self.assertIsNone(vm.status()['snapshot'])
                self.assertTrue(vm.status()['error'])
            finally:
                vm.close()

    def test_validation_failure_retains_old_frame(self):
        self.vm.start()
        wait_for(lambda: self.vm.status()['snapshot'])
        old = self.vm.png
        self.vm.stop()
        self.vm.thread.join(8)
        self.vm.command[-1] = 'corrupt'
        self.vm.start()
        wait_for(lambda: bool(self.vm.status()['error']))
        self.assertEqual(self.vm.png, old)
        self.assertTrue(self.vm.status()['stale'])
        self.assertIn('校验失败', self.vm.status()['error'])

    def test_stop_cancels_startup_or_inflight(self):
        for mode in ('startup-hang', 'hang'):
            vm = mock_vm(mode)
            vm.start()
            if mode == 'hang':
                wait_for(lambda: vm.status()['busy'] == 'snapshot')
            vm.stop()
            vm.thread.join(8)
            self.assertFalse(vm.thread.is_alive())
            self.assertEqual(vm.status()['state'], 'exited')

    def test_firmware_crash_between_commands_and_inflight_stops_input(self):
        (BASE / 'menu-runs').mkdir(exist_ok=True)
        for mode in ('idle-crash', 'inflight-crash'):
            with tempfile.TemporaryDirectory(dir=BASE / 'menu-runs') as run:
                vm = VM([sys.executable, str(BASE / 'mock_menu_vm.py'), mode, run], native_display='none')
                vm.mock = False  # Exercise the real console monitor, without starting a VM.
                try:
                    vm.start()
                    wait_for(lambda: vm.status()['state'] == 'running')
                    vm.enqueue({'action': 'key', 'key': 'ENTER'})
                    if mode == 'inflight-crash':
                        for _ in range(6):
                            vm.enqueue({'action': 'key', 'key': 'DOWN'})
                    wait_for(lambda: vm.status()['state'] == 'error', timeout=3)
                    vm.thread.join(8)
                    self.assertFalse(vm.thread.is_alive())
                    self.assertIn('固件发生异常', vm.status()['error'])
                    self.assertEqual(vm.status()['pending'], 0)
                    with self.assertRaisesRegex(ValueError, '尚未运行'):
                        vm.enqueue({'action': 'key', 'key': 'LEFT'})
                finally:
                    vm.close()


class TransportTests(unittest.TestCase):
    def test_existing_exception_prevents_any_serial_write(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            console = Path(tmp) / 'console.log'
            console.write_bytes(b'command succeeded\nEntering exception monitor.\nException> ')
            process = Mock()
            with self.assertRaises(FirmwareFault):
                serial_command(process, console, 'true')
            process.stdin.write.assert_not_called()

    def test_monitor_handles_split_marker_and_log_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = Path(tmp) / 'console.log'
            console.write_bytes(b'normal startup\n' * 100)
            monitor = FirmwareMonitor(console)
            monitor.check()
            console.write_bytes(b'new log\r\nEntering except')
            monitor.check()
            with console.open('ab') as output:
                output.write(b'ion monitor.\r\n')
            with self.assertRaises(FirmwareFault):
                monitor.check()
            with self.assertRaises(FirmwareFault):
                monitor.check()

    def test_cleanup_only_owns_created_group(self):
        owned = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        try:
            terminate_owned(owned)
            self.assertIsNotNone(owned.poll())
            self.assertIsNone(unrelated.poll())
        finally:
            terminate_owned(unrelated)

    def test_sentinel_ignores_prompt_echo_and_old_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = Path(tmp) / 'console.log'
            console.write_bytes(b'old data\nMENU_DIAG_COMMAND_DONE_old:0\n')
            class Writer:
                status = b'0'
                def write(self, data):
                    marker = re.search(rb'MENU_DIAG_COMMAND_DONE_[a-f0-9]+', data)[0]
                    with console.open('ab') as output:
                        output.write(b'\n/ # ' + data + b'\n/ # \nMENU_DIAG_COMMAND_DONE_old:0\n')
                    def complete():
                        with console.open('ab') as output:
                            output.write(b'\n' + marker + b':' + self.status + b'\n')
                    threading.Timer(.15, complete).start()
                def flush(self):
                    pass
            class Proc:
                stdin = Writer()
                def poll(self):
                    return None
            start = time.monotonic()
            serial_command(Proc(), console, 'true', timeout=2)
            self.assertGreaterEqual(time.monotonic() - start, .14)
            Writer.status = b'7'
            with self.assertRaisesRegex(RuntimeError, '返回码 7'):
                serial_command(Proc(), console, 'false', timeout=2)
            with patch.object(Writer, 'write', lambda self, data: None):
                with self.assertRaises(TimeoutError):
                    serial_command(Proc(), console, 'true', timeout=.1)

    def test_container_validation_is_required(self):
        valid = (BASE / 'fixtures/headless-osd.b64').read_bytes()
        message = snapshot_message(valid)
        self.assertEqual(message['info']['sequence'], 2)
        with self.assertRaises(ValueError):
            snapshot_message(valid.replace(b'HEADLESS_OSD_END', b'BROKEN'))
        lines = valid.splitlines()
        start, end = lines.index(b'HEADLESS_OSD_BEGIN'), lines.index(b'HEADLESS_OSD_END')
        raw = bytearray(base64.b64decode(b''.join(lines[start + 1:end])))
        raw[-1] ^= 1
        with self.assertRaises(ValueError):
            snapshot_message(b'HEADLESS_OSD_BEGIN\n' + base64.b64encode(raw) + b'\nHEADLESS_OSD_END\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
