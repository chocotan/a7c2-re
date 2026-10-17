"""Private live-control transport and serial commands; no caller-supplied shell."""
import base64
import io
import json
import os
import re
import secrets
import select
import socket
import time

from headless_osd_capture import decode_export, HEADER, METADATA

MAX_MESSAGE = 2 * 1024 * 1024
# Add only independently verified mappings here; HTTP accepts symbolic names only.
KEYS = {'MENU': '0x1ab', 'ENTER': '0x1ac', 'UP': '0x3', 'DOWN': '0x30',
        'LEFT': '0x15', 'RIGHT': '0x1af', 'DELETE': '0x1f8',
        'REAR_LEFT': '0x10000013', 'REAR_RIGHT': '0x10000013'}
SINGLE_STATUS = {'REAR_LEFT': '0xfffffc00', 'REAR_RIGHT': '0x400'}


class FirmwareFault(RuntimeError):
    pass


class FirmwareMonitor:
    """Follow the complete console, including faults between two commands."""
    def __init__(self, console):
        self.console = console
        self.offset = 0
        self.tail = b''
        self.failed = False

    def check(self):
        if self.failed:
            raise FirmwareFault('相机固件发生异常，已停止接收按键；详情见 console.log')
        with self.console.open('rb') as reader:
            if os.fstat(reader.fileno()).st_size < self.offset:
                self.offset, self.tail = 0, b''
            reader.seek(self.offset)
            while chunk := reader.read(65536):
                self.offset += len(chunk)
                data = self.tail + chunk.replace(b'\r', b'')
                if re.search(rb'(?:^|\n)(?:Entering exception monitor\.|Exception>)', data):
                    self.failed = True
                    raise FirmwareFault('相机固件发生异常，已停止接收按键；详情见 console.log')
                self.tail = data[-128:]


def connect_in_run(sock, directory, name):
    # Linux AF_UNIX paths are short; the run directory may live in a long checkout.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        sock.connect(f'/proc/self/fd/{fd}/{name}')
    finally:
        os.close(fd)


def key_command(key):
    code = KEYS[key]
    prefix = f'/simulation/inject-key 0x807a0020 0x92840747 {code} 0'
    if key in SINGLE_STATUS:
        return f'{prefix} {SINGLE_STATUS[key]} 0 1'
    # The native UI expects 1/0. A 50 ms guest tap avoids firmware auto-repeat.
    return (f'{prefix} 1 0 1; pressed=$?; sleep 0.05; '
            f'{prefix} 0 0 1; released=$?; test "$pressed" = 0 && test "$released" = 0')


def send_message(sock, value):
    sock.sendall(json.dumps(value, ensure_ascii=True).encode() + b'\n')


def receive_message(sock, timeout=120, cancelled=lambda: False):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        if cancelled():
            raise RuntimeError('操作已取消')
        if not select.select([sock], [], [], min(.2, max(0, deadline - time.monotonic())))[0]:
            continue
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError('VM 控制通道已关闭')
        data.extend(chunk)
        if len(data) > MAX_MESSAGE:
            raise ValueError('控制消息过大')
        if b'\n' in data:
            if not data.endswith(b'\n') or data.count(b'\n') != 1:
                raise ValueError('控制消息边界错误')
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError('控制消息类型错误')
            return value
    raise TimeoutError('等待 VM 命令完成超时')


def clean_console(data):
    return re.sub(rb'\[\s*\d+\.\d+\] (?:UDIF:|\[K\])[^\n]*\n', b'', data.replace(b'\r\n', b'\n'))


def serial_command(proc, console, command, timeout=90, monitor=None):
    """Ignore echoed commands/prompts and require this command's exact RC sentinel."""
    monitor = monitor or FirmwareMonitor(console)
    monitor.check()
    marker = 'MENU_DIAG_COMMAND_DONE_' + secrets.token_hex(12)
    # Live console writers use O_APPEND; discard old exports only between commands.
    if console.stat().st_size > 16 * 1024 * 1024:
        with console.open('r+b') as old:
            old.truncate(0)
    with console.open('rb') as reader:
        reader.seek(0, 2)
        proc.stdin.write((command + "; rc=$?; printf '\\n" + marker + ":%s\\n' \"$rc\"\n").encode())
        proc.stdin.flush()
        data = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            monitor.check()
            data.extend(reader.read(65536))
            if len(data) > 8 * 1024 * 1024:
                raise RuntimeError('单次串口输出超过限制')
            clean = clean_console(bytes(data))
            if b'Exception>' in clean:
                raise RuntimeError('固件进入异常监视器')
            match = re.search(rb'\n' + marker.encode() + rb':([0-9]+)\n', clean)
            if match:
                if int(match[1]):
                    raise RuntimeError('guest 命令失败，返回码 ' + match[1].decode())
                return bytes(data)
            if proc.poll() is not None:
                raise RuntimeError('VM 进程提前退出')
            time.sleep(.05)
    raise TimeoutError('串口命令缺少完成标记；停止 VM 以避免后续命令交错')


def snapshot_message(console):
    from PIL import Image
    raw, info = decode_export(console)
    pixels = raw[HEADER + METADATA:]
    rgba = bytearray(640 * 480 * 4)
    for i in range(640 * 480):
        value = pixels[i * 2] | pixels[i * 2 + 1] << 8
        rgba[i * 4:i * 4 + 4] = bytes((value >> 12, value >> 8 & 15, value >> 4 & 15, value & 15))
    rgba = bytes(channel * 17 for channel in rgba)
    image = Image.frombytes('RGBA', (640, 480), rgba)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return {'type': 'snapshot', 'info': info, 'captured_at': time.time(),
            'png': base64.b64encode(buffer.getvalue()).decode()}


def run_live(proc, out, descriptor):
    monitor = FirmwareMonitor(out / 'console.log')
    with socket.socket(fileno=descriptor) as control:
        # Barrier follows the entire queued setup, not a shell prompt or stdout "ready".
        serial_command(proc, out / 'console.log', 'true', monitor=monitor)
        send_message(control, {'type': 'ready', 'run': str(out)})
        while True:
            monitor.check()
            if proc.poll() is not None:
                if proc.returncode == 0:
                    return
                raise RuntimeError('VM 已退出')
            if not select.select([control], [], [], .5)[0]:
                continue
            request = receive_message(control, timeout=5)
            action = request.get('action')
            if action == 'snapshot' and set(request) == {'action'}:
                data = serial_command(proc, out / 'console.log', '/simulation/headless-osd-export', monitor=monitor)
                try:
                    result = snapshot_message(data)
                except (ValueError, IndexError) as error:
                    result = {'type': 'error', 'error': '快照校验失败：' + str(error)}
            elif action == 'key' and set(request) == {'action', 'key'} and request['key'] in KEYS:
                # Release is always attempted, even if press fails. The result covers both sends.
                serial_command(proc, out / 'console.log', key_command(request['key']), monitor=monitor)
                result = {'type': 'key', 'key': request['key']}
            else:
                raise ValueError('非法私有控制命令')
            send_message(control, result)
