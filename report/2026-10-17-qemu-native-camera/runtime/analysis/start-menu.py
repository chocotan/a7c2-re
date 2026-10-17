#!/usr/bin/env python3
"""Start the verified, bounded A7C II menu capture profile."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from headless_osd_capture import decode_export, HEADER, METADATA

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def profile_environment(current, menu_key=False):
    # Prior research switches must not silently change the one-click baseline.
    env = {key: value for key, value in current.items()
           if not key.startswith(('MENU_', 'QEMU_', 'SONY_'))
           and key not in ('SKIP_REBUILD', 'SEED_BACKUP', 'PRELOAD_ALL', 'CPIO_PAD_BYTES')}
    env.update(MENU_HEADLESS_OSD='1', MENU_SOFTWARE_LZ77='1', MENU_GPE_RGBA4444='1',
               MENU_APPLY_MENU='1', MENU_FRAME_OBSERVE='1', MENU_OSD_OBSERVE='1',
               MENU_CTOR_CENSUS='1', MENU_OPEN_SET='0x308', MENU_OPEN_SET_LOG_ONLY='1',
               MENU_MODE_SENTINELS='1', MENU_SETTLE_SECONDS='8', QEMU_BOOT_TYPE='1', MENU_DATE_SERVICE='1')
    if menu_key:
        env.update(MENU_KEY_INJECT='0x1ab,0,1,0,1,0.05,0', MENU_KEY_POST_WAIT='2')
    return env


def result_directory(output, runs):
    decoder = json.JSONDecoder()
    for match in reversed(list(re.finditer(r'^\{', output, re.MULTILINE))):
        try:
            info, _ = decoder.raw_decode(output[match.start():])
        except ValueError:
            continue
        if isinstance(info, dict) and info.get('probe_collected') is True and isinstance(info.get('run'), str):
            run = Path(info['run']).resolve()
            if run.parent != runs.resolve():
                raise ValueError('运行结果不在本项目 menu-runs 目录中')
            return run
    raise ValueError('未取得本次运行的结果目录；请检查上方启动日志')


def verify_snapshot(run):
    from PIL import Image
    raw, info = decode_export((run / 'console.log').read_bytes())
    if (run / 'headless-osd-frame.bin').read_bytes() != raw:
        raise ValueError('保存的快照与原始导出不一致')
    if (run / 'headless-osd-pixels.bin').read_bytes() != raw[HEADER + METADATA:]:
        raise ValueError('保存的像素与提交容器不一致')
    image = run / 'headless-osd-pixels-rgba4444-software.png'
    with Image.open(image) as picture:
        if picture.size != (640, 480) or picture.convert('RGB').getcolors(maxcolors=2) is not None:
            raise ValueError('本次画面为空或仅为纯色；未将其视为菜单成功')
    return image, info


def main():
    parser = argparse.ArgumentParser(description='一键启动隔离QEMU、采集并打开已验证菜单快照；完成后自动关闭VM。不是常驻交互窗口。')
    parser.add_argument('--menu-key', action='store_true', help='追加已验证的 MENU/返回短按；可能退出当前菜单')
    parser.add_argument('--no-open', action='store_true', help='仅保存快照，不调用桌面图片查看器')
    args = parser.parse_args()
    for command in ('bash', 'flock', 'clang', 'cpio', 'bwrap'):
        if not shutil.which(command):
            parser.error('缺少依赖：' + command)
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        parser.error('需要已安装的 Python Pillow 才能验证和显示快照')
    print('启动 A7C II 菜单实验：重建 → 隔离运行 → 校验快照 → 关闭 VM。', flush=True)
    print('仅 AP 软件输出；常驻原生窗口请使用 start-a7c2-native.sh。', flush=True)
    lines = []
    with subprocess.Popen(['bash', str(BASE / 'run-menu-probe.sh')], cwd=ROOT,
                          env=profile_environment(os.environ, args.menu_key),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
        for line in process.stdout:
            print(line, end='', flush=True)
            lines.append(line)
        code = process.wait()
    if code:
        return code if code > 0 else 1
    try:
        run = result_directory(''.join(lines), BASE / 'menu-runs')
        image, info = verify_snapshot(run)
    except (OSError, ValueError, IndexError) as error:
        print('快照验收失败：' + str(error), file=sys.stderr)
        return 1
    print(f'\n已验证本次提交：事务 {info["sequence"]}，像素来源 {info["pixel_source_seq"]}')
    print(f'图片：{image}\n日志与元数据：{run}')
    print('VM 已退出；此入口用于复现快照，不提供实时相机操作。')
    if not args.no_open and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and shutil.which('xdg-open'):
        subprocess.Popen(['xdg-open', str(image)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
