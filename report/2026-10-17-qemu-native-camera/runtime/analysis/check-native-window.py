#!/usr/bin/env python3
"""Bounded X11 integration check against our own native QEMU window."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

from PIL import Image

BASE = Path(__file__).resolve().parent
checks = BASE / 'native-window-check'
checks.mkdir(exist_ok=True)
out = Path(tempfile.mkdtemp(prefix='check-', dir=checks))
log_path = out / 'launcher.log'


def wait_for(predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        if process.poll() is not None:
            raise RuntimeError(f'launcher exited {process.returncode}; {log_path}')
        time.sleep(.1)
    raise TimeoutError(f'check timed out; {log_path}')


def frame_position(window, firmware):
    """Find the exact unscaled AP image inside the native GTK client area."""
    actual = window.convert('RGB')
    expected = Image.alpha_composite(Image.new('RGBA', firmware.size, (0, 0, 0, 255)),
                                    firmware.convert('RGBA')).convert('RGB')
    data, first_row = actual.tobytes(), expected.tobytes()[:640*3]
    index = data.find(first_row)
    while index >= 0:
        pixel, channel = divmod(index, 3)
        y, x = divmod(pixel, actual.width)
        if channel == 0 and x + 640 <= actual.width and y + 480 <= actual.height:
            if actual.crop((x, y, x+640, y+480)).tobytes() == expected.tobytes():
                return [x, y]
        index = data.find(first_row, index + 1)
    return None


with log_path.open('wb') as log:
    window = None
    process = subprocess.Popen([sys.executable, str(BASE / 'menu-native.py')],
                               stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        match = wait_for(lambda: re.search(r'原生按键通道就绪；运行目录：(.*)', log_path.read_text()), 180)
        run = Path(match[1].strip())
        windows = subprocess.check_output(['xdotool', 'search', '--onlyvisible', '--name',
                                          'Sony A7C II - experimental menu'], text=True).split()
        if len(windows) != 1:
            raise RuntimeError('expected exactly our one native window: ' + repr(windows))
        window = windows[0]
        subprocess.run(['xdotool', 'windowfocus', '--sync', window], check=True)
        subprocess.run(['xdotool', 'mousemove', '--window', window, '320', '280', 'click', '1'], check=True)

        def press(key):
            if subprocess.check_output(['xdotool', 'getwindowfocus'], text=True).strip() != window:
                raise RuntimeError('test window lost focus; refusing global key injection')
            # XTEST follows GTK focus; XSendEvent to the toplevel skips DrawingArea.
            subprocess.run(['xdotool', 'key', '--clearmodifiers', key], check=True)

        results = []
        for number, key in enumerate((None, 'Return', 'Down', 'Up', 'Left', 'Right', 'Next', 'Prior', 'm'), 1):
            if key:
                press(key)
                time.sleep(.3)
                subprocess.run(['import', '-window', window,
                                str(out / f'{number:02d}-{key}-after-key.png')], check=True)
            press('F12')
            saved = run / f'user-snapshot-{number:03d}.png'
            wait_for(lambda: saved.exists() and f'已保存校验快照：{saved}' in log_path.read_text(), 15)
            capture = out / f'{number:02d}-{key or "menu"}.png'
            subprocess.run(['import', '-window', window, str(capture)], check=True)
            with Image.open(capture) as image, Image.open(saved) as firmware:
                position = frame_position(image, firmware)
                results.append({'key': key, 'window_capture': capture.name,
                                'ap_capture': str(saved), 'canvas_origin': position})
                if position is None:
                    raise AssertionError('native window pixels differ from validated AP snapshot: ' + str(capture))
        # QEMU's normal Quit action also works without an Xvfb window manager.
        press('ctrl+alt+q')
        code = process.wait(timeout=20)
        if code:
            raise AssertionError(f'normal window close returned {code}; {log_path}')
        (out / 'result.json').write_text(json.dumps({'run': str(run), 'checks': results,
                                                   'normal_close_exit': code}, indent=2) + '\n')
        print('NATIVE_WINDOW_PASS', out)
    except Exception:
        if window:
            subprocess.run(['import', '-window', window, str(out / 'failure-window.png')],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        raise
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
