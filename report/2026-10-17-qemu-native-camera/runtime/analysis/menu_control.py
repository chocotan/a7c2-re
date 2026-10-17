"""Loopback-only UI and one owned VM. Python stdlib HTTP, existing Pillow decoder."""
import argparse
import base64
from collections import deque
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import socket
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from menu_live import KEYS, FirmwareMonitor, receive_message, send_message

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
UI = ROOT / 'analysis/qemu-control'
HISTORY = ROOT / 'analysis/qemu-boot-research/screenshots/07-menu-headless-committed.png'
spec = importlib.util.spec_from_file_location('start_menu', BASE / 'start-menu.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def terminate_owned(process):
    """Only a Popen created with start_new_session=True is passed here."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=6)
    except subprocess.TimeoutExpired:
        pass
    # Descendants can outlive their leader; this group was allocated by our Popen.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=3)


class VM:
    def __init__(self, command=None, native_display=None):
        if native_display not in (None, 'none', 'gtk', 'sdl'):
            raise ValueError('不支持的原生显示后端')
        self.command = command or ['bash', str(BASE / 'run-menu-probe.sh')]
        self.mock = command is not None
        self.native_display = native_display
        self.run_dir = ''
        self.lock = threading.RLock()
        self.state = 'disconnected'
        self.error = ''
        self.snapshot = None
        self.png = None
        self.stale = True
        self.events = deque(maxlen=12)
        self.event_id = 0
        self.thread = None
        self.cancel = threading.Event()
        self.commands = queue.Queue(maxsize=8)
        self.busy = ''
        self.log_path = ''

    def event(self, message, kind='status'):
        self.event_id += 1
        self.events.appendleft({'id': self.event_id, 'time': time.time(), 'message': message, 'kind': kind})

    def status(self):
        with self.lock:
            return dict(state=self.state, error=self.error, snapshot=self.snapshot, stale=self.stale,
                        busy=self.busy, pending=self.commands.qsize(), events=list(self.events),
                        keys=list(KEYS), mock=self.mock, log=self.log_path, run=self.run_dir)

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('已有 VM 正在启动、运行或停止')
            self.state, self.error, self.stale = 'starting', '', True
            self.run_dir = ''
            self.cancel.clear()
            self.commands = queue.Queue(maxsize=8)
            self.event('启动请求已接受；等待构建、锁与串口初始化完成')
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def enqueue(self, command):
        with self.lock:
            if self.state != 'running':
                raise ValueError('VM 尚未运行')
            try:
                self.commands.put_nowait(command)
            except queue.Full:
                raise ValueError('命令队列已满，请稍后重试') from None

    def stop(self):
        with self.lock:
            if self.state not in ('starting', 'running', 'stopping'):
                raise ValueError('没有本控制台正在运行的 VM')
            self.state, self.stale = 'stopping', True
            self.cancel.set()
            self.event('停止请求已接受；正在清理本控制台创建的 VM')

    def close(self):
        with self.lock:
            self.cancel.set()
        if self.thread:
            self.thread.join(timeout=12)

    def _run(self):
        process = None
        monitor = None
        parent, child = socket.socketpair()
        # run-menu-probe.sh reserves fd9 for its build/runtime flock.
        inherited = socket.socket(fileno=fcntl.fcntl(child.fileno(), fcntl.F_DUPFD_CLOEXEC, 20))
        child.close()
        child = inherited
        log = None
        try:
            import tempfile
            logs = BASE / 'ui-runs'
            logs.mkdir(exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='ui-', suffix='.log', dir=logs)
            log = os.fdopen(fd, 'wb')
            with self.lock:
                self.log_path = name
            env = launcher.profile_environment(os.environ)
            if self.native_display is not None:
                env.update(MENU_NATIVE_WINDOW='1', QEMU_NATIVE_DISPLAY=self.native_display,
                           QEMU_NATIVE_INPUT='1', QEMU_SYSTEM_DIR=str(BASE.parent / 'sources/qemu-11.1.2/build-sony-ui'))
            env['MENU_CONTROL_FD'] = str(child.fileno())
            process = subprocess.Popen(self.command, env=env, cwd=BASE.parent,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, pass_fds=(child.fileno(),))
            child.close()
            reply = receive_message(parent, timeout=300, cancelled=self.cancel.is_set)
            if reply.get('type') != 'ready':
                raise RuntimeError('VM 初始化确认无效')
            if not self.mock:
                run = Path(reply.get('run', '')).resolve()
                if run.parent != (BASE / 'menu-runs').resolve():
                    raise RuntimeError('VM 返回的运行目录不合法')
                monitor = FirmwareMonitor(run / 'console.log')

            def cancelled():
                if self.cancel.is_set():
                    return True
                if monitor:
                    monitor.check()
                return False

            cancelled()
            with self.lock:
                if self.cancel.is_set():
                    return
                self.state = 'running'
                self.run_dir = reply.get('run', '')
                self.event('串口初始化已完成；' + ('等待本次已校验快照' if self.native_display is None else '原生画面由 ramfb 更新'))
                if self.native_display is None:
                    self.commands.put_nowait({'action': 'snapshot'})
            while not cancelled():
                if process.poll() is not None:
                    if process.returncode == 0:
                        with self.lock:
                            self.state, self.stale = 'exited', True
                            self.event('QEMU 窗口已关闭，VM 已退出')
                        break
                    raise RuntimeError('VM 进程意外退出，返回码 ' + str(process.returncode))
                try:
                    command = self.commands.get(timeout=.2)
                except queue.Empty:
                    continue
                with self.lock:
                    self.busy = command['action']
                send_message(parent, command)
                reply = receive_message(parent, cancelled=cancelled)
                with self.lock:
                    if reply.get('type') == 'error':
                        self.error = str(reply.get('error', 'VM 操作失败'))
                        self.stale = True
                        self.event(self.error)
                    elif command['action'] == 'snapshot' and reply.get('type') == 'snapshot':
                        from PIL import Image
                        png = base64.b64decode(reply['png'], validate=True)
                        with Image.open(io.BytesIO(png)) as image:
                            if image.format != 'PNG' or image.size != (640, 480):
                                raise ValueError('快照 PNG 格式错误')
                            image.verify()
                        info = reply['info']
                        self.png = png
                        self.snapshot = {'sequence': info['sequence'], 'pixel_source_seq': info['pixel_source_seq'],
                                         'sha256': info['sha256'], 'captured_at': reply['captured_at'],
                                         'number': (self.snapshot or {}).get('number', 0) + 1}
                        self.stale, self.error = False, ''
                        self.event(f'快照 #{self.snapshot["number"]} 已校验；事务 {info["sequence"]}')
                    elif command['action'] == 'key' and reply == {'type': 'key', 'key': command['key']}:
                        self.event(command['key'] + '：输入已提交', kind='input')
                    else:
                        raise RuntimeError('VM 返回不匹配，未将操作视为成功')
                    self.busy = ''
        except Exception as error:
            if monitor and not self.cancel.is_set():
                try:
                    monitor.check()
                except (OSError, RuntimeError) as fault:
                    error = fault
            with self.lock:
                if not self.cancel.is_set():
                    self.error = str(error)
                    self.state = 'error'
                    self.event('VM 错误：' + self.error)
        finally:
            parent.close()
            child.close()
            cleanup_error = None
            if process is not None:
                try:
                    terminate_owned(process)
                except (OSError, subprocess.TimeoutExpired) as error:
                    cleanup_error = str(error)
            if log is not None:
                log.close()
            with self.lock:
                self.busy, self.stale = '', True
                while not self.commands.empty():
                    self.commands.get_nowait()
                if cleanup_error:
                    self.state, self.error = 'error', 'VM 清理失败：' + cleanup_error
                    self.event(self.error)
                elif self.cancel.is_set():
                    self.state = 'exited'
                    self.event('本控制台的 VM 已退出')


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, vm, port=0):
        self.vm, self.token = vm, secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), Handler)
        self.host = '127.0.0.1:' + str(self.server_port)
        self.origin = 'http://' + self.host


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def respond(self, status, body, kind='application/json; charset=utf-8', download=False, number=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'none'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="a7c2-snapshot.png"')
        if number is not None:
            self.send_header('X-Snapshot-Number', str(number))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self, api=False, mutation=False):
        if self.headers.get_all('Host') != [self.server.host]:
            return False
        origins = self.headers.get_all('Origin')
        if origins is not None and origins != [self.server.origin]:
            return False
        if mutation and origins != [self.server.origin]:
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        return not api or secrets.compare_digest(self.headers.get('X-Session-Token', '').encode(), self.server.token.encode())

    def do_GET(self):
        api = self.path.startswith('/api/')
        if not self.authorized(api=api):
            return self.respond(403, {'error': '拒绝无效 Host、Origin 或会话令牌'})
        if self.path == '/api/state':
            return self.respond(200, self.server.vm.status())
        if self.path in ('/api/snapshot', '/api/save'):
            with self.server.vm.lock:
                png = self.server.vm.png
                number = (self.server.vm.snapshot or {}).get('number')
            if png is None:
                return self.respond(409, {'error': '尚无本次控制台已校验快照'})
            return self.respond(200, png, 'image/png', self.path == '/api/save', number)
        files = {'/': (UI / 'index.html', 'text/html; charset=utf-8'),
                 '/app.js': (UI / 'app.js', 'text/javascript; charset=utf-8'),
                 '/style.css': (UI / 'style.css', 'text/css; charset=utf-8'),
                 '/history.png': (HISTORY, 'image/png')}
        if self.path not in files:
            return self.respond(404, {'error': '接口不存在'})
        path, kind = files[self.path]
        try:
            return self.respond(200, path.read_bytes(), kind)
        except OSError:
            return self.respond(404, {'error': '资源不可用'})

    def do_POST(self):
        if not self.authorized(api=True, mutation=True):
            return self.respond(403, {'error': '拒绝无效 Host、Origin 或会话令牌'})
        if self.path not in ('/api/start', '/api/stop', '/api/snapshot', '/api/key'):
            return self.respond(404, {'error': '接口不存在'})
        lengths = self.headers.get_all('Content-Length') or []
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) > 256:
            return self.respond(413, {'error': '请求长度无效或超过 256 字节'})
        if self.headers.get('Content-Type') != 'application/json':
            return self.respond(415, {'error': '仅接受 application/json'})
        try:
            payload = json.loads(self.rfile.read(int(lengths[0])))
            if self.path == '/api/key':
                if not isinstance(payload, dict) or set(payload) != {'key'} or not isinstance(payload['key'], str) or payload['key'] not in KEYS:
                    raise ValueError('按键未验证或参数无效')
                self.server.vm.enqueue({'action': 'key', 'key': payload['key']})
            else:
                if payload != {}:
                    raise ValueError('该接口不接受参数')
                if self.path == '/api/start':
                    self.server.vm.start()
                elif self.path == '/api/stop':
                    self.server.vm.stop()
                else:
                    self.server.vm.enqueue({'action': 'snapshot'})
        except (ValueError, UnicodeError) as error:
            return self.respond(400, {'error': str(error)})
        return self.respond(202, {'accepted': True})


def main():
    parser = argparse.ArgumentParser(description='本机 QEMU 常驻控制台；关闭此进程即停止其 VM')
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    vm = VM()
    server = Server(vm, args.port)
    url = server.origin + '/#' + server.token
    print('控制台地址（含本次会话令牌，请勿分享）：' + url, flush=True)
    print('按 Ctrl+C 退出服务并清理本控制台的 VM。', flush=True)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        if not args.no_open:
            webbrowser.open(url)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        vm.close()


if __name__ == '__main__':
    main()
