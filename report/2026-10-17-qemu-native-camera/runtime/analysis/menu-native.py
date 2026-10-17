#!/usr/bin/env python3
"""Native QEMU GTK window; reuse the isolated VM and private control transport."""
import argparse
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import time

from menu_control import BASE, VM
from menu_live import connect_in_run

BUTTONS = {ord('M'): 'MENU', ord('E'): 'ENTER', ord('U'): 'UP',
           ord('D'): 'DOWN', ord('L'): 'LEFT', ord('R'): 'RIGHT', ord('X'): 'DELETE',
           ord('<'): 'REAR_LEFT', ord('>'): 'REAR_RIGHT'}


def main():
    parser = argparse.ArgumentParser(description='启动 Sony A7C II 原生 QEMU 菜单窗口；关闭窗口或 Ctrl+C 停止 VM。')
    parser.add_argument('--display', choices=('gtk', 'sdl', 'none'), default='gtk',
                        help='默认 GTK；none 仅用于自动化验收，不打开窗口')
    args = parser.parse_args()
    for command in ('bash', 'flock', 'clang', 'cpio', 'bwrap'):
        if not shutil.which(command):
            parser.error('缺少依赖：' + command)
    binary = BASE.parent / 'sources/qemu-11.1.2/build-sony-ui/qemu-system-aarch64'
    if not binary.is_file():
        parser.error('缺少原生窗口版 QEMU，请先按 README 构建或安装交付的程序：' + str(binary))
    try:
        backends = subprocess.check_output([str(binary), '-display', 'help'], text=True)
        devices = subprocess.check_output([str(binary), '-device', 'help'], text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        parser.error('QEMU 或其运行库不可用：' + str(error))
    if args.display not in backends.split() or 'name "sony-ui-input"' not in devices:
        parser.error('QEMU 缺少所选图形后端或 Sony 按键桥，请核对构建版本')
    if args.display != 'none' and not os.environ.get('DISPLAY'):
        parser.error('未检测到本地图形桌面 DISPLAY；请在 X11/Xwayland 桌面终端启动')

    vm = VM(native_display=args.display)
    input_socket = None
    input_closed_at = None
    last_event = 0
    saved = 0

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    print('启动原生 QEMU 窗口。方向键导航，Enter 确定，M/Esc 返回，Delete 删除/帮助，F12 保存快照。', flush=True)
    print('这是原固件菜单的 AP 软件输出实验；拍摄、完整 CP 合成及正常冷启动尚未恢复。', flush=True)
    vm.start()
    try:
        while True:
            state = vm.status()
            for event in reversed(state['events']):
                if event['id'] > last_event:
                    if event.get('kind') != 'input':
                        print(event['message'], flush=True)
                    last_event = event['id']
            if state['state'] == 'error':
                print('运行失败，日志：' + state['log'], file=sys.stderr)
                return 1
            if state['state'] == 'exited':
                print('日志与截图：' + state['run'], flush=True)
                return 0
            if input_closed_at is not None and time.monotonic() - input_closed_at > 3:
                raise RuntimeError('原生按键通道关闭，但 VM 尚未正常退出')
            if state['state'] == 'running' and input_socket is None and input_closed_at is None:
                run = Path(state['run']).resolve()
                if run.parent != (BASE / 'menu-runs').resolve():
                    raise RuntimeError('VM 返回的运行目录不合法')
                input_socket = socket.socket(socket.AF_UNIX)
                input_socket.settimeout(2)
                connect_in_run(input_socket, run, 'native-input.sock')
                input_socket.settimeout(None)
                print('原生按键通道就绪；运行目录：' + str(run), flush=True)
            if input_socket is not None and select.select([input_socket], [], [], .1)[0]:
                data = input_socket.recv(64)
                if not data:
                    input_socket.close()
                    input_socket = None
                    # QEMU closes this socket before its process exits.
                    input_closed_at = time.monotonic()
                    continue
                for key in data:
                    command = {'action': 'snapshot'} if key == ord('S') else (
                        {'action': 'key', 'key': BUTTONS[key]} if key in BUTTONS else None)
                    if command is None:
                        raise RuntimeError('原生按键桥返回未知命令')
                    try:
                        vm.enqueue(command)
                    except ValueError as error:
                        print('按键未发送：' + str(error), flush=True)
                        if vm.status()['state'] != 'running':
                            break
            elif input_socket is None:
                time.sleep(.1)
            with vm.lock:
                snapshot = vm.snapshot
                if snapshot and snapshot['number'] > saved:
                    saved = snapshot['number']
                    path = Path(vm.run_dir) / f'user-snapshot-{saved:03d}.png'
                    with path.open('xb') as output:
                        output.write(vm.png)
                    print('已保存校验快照：' + str(path), flush=True)
    except KeyboardInterrupt:
        print('正在停止本次 VM。', flush=True)
        return 0
    except (OSError, RuntimeError) as error:
        print('原生窗口错误：' + str(error) + '\n日志：' + vm.log_path, file=sys.stderr)
        return 1
    finally:
        if input_socket is not None:
            input_socket.close()
        vm.close()


if __name__ == '__main__':
    raise SystemExit(main())
