#!/usr/bin/env python3
"""Read camera memory to this computer — one command, read-only.

    sudo ./.venv/bin/python memread.py --phys 0xffff0000 --size 0x6000 --save brom.bin
    sudo ./.venv/bin/python memread.py --pid 1 --addr 0x400000 --size 0x1000

`/dev/mem` is unrestricted on this camera, so the root the preload gives us reaches
the BootROM, SoC registers and any process's memory. Reads only: this never writes to
the device. The region is written on the camera and pulled back in 64 KiB chunks
through the mailbox, so no card swap is needed.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / "usb_service_probe.py"
VENV_PYTHON = HERE / ".venv" / "bin" / "python"
APPROVAL = "arm-preload-once"


def build_scrapers(ns):
    """Build the DEVICE:ID:OFFSET:SIZE:NAME specs this run should read."""
    if ns.phys is None and ns.pid is None:
        raise ValueError("give --phys ADDRESS or --pid PID")
    source, ident = ("phys", "0") if ns.phys is not None else ("pid", str(ns.pid))
    offset = ns.phys if ns.phys is not None else ns.addr
    region = f"{source}:{ident}:{offset}:{ns.size}"
    if ns.name:
        region += f":{ns.name}"
    return [region]


def build_command(regions, save, deadline, interpreter_path=None, unmount=True):
    command = [interpreter_path or (str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable),
               str(PROBE), "--approve-preload", APPROVAL, "--deadline", str(deadline)]
    for region in regions:
        command += ["--mem-read", region]
    if save:
        command += ["--mem-read-save", str(save)]
    if unmount:
        command += ["--unmount-camera-volumes"]
    return command


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phys", metavar="ADDRESS", type=lambda t: int(t, 0),
                        help="Physical address to read through /dev/mem (hex or decimal)")
    parser.add_argument("--pid", type=int, help="Read inside this process instead")
    parser.add_argument("--addr", type=lambda t: int(t, 0), default=0,
                        help="Virtual address for --pid (default 0)")
    parser.add_argument("--size", type=lambda t: int(t, 0), required=True,
                        help="How many bytes to read")
    parser.add_argument("--name", help="File name used on the camera side")
    parser.add_argument("--save", metavar="FILE", help="Write the bytes here on this computer")
    parser.add_argument("--deadline", type=int, default=600, help="Session cap in seconds")
    parser.add_argument("--keep-mounts", action="store_true", help="Do not unmount camera volumes")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        regions = build_scrapers(args)
    except ValueError as error:
        parser.error(str(error))
    command = build_command(regions, args.save, args.deadline,
                            interpreter_path=sys.executable if args.dry_run else None,
                            unmount=not args.keep_mounts)
    if args.dry_run:
        print(" ".join(command))
        return 0
    if os.geteuid() != 0:
        print("需要 root：请用 sudo 运行", file=sys.stderr)
        return 2
    for region in regions:
        print(f"读取 {region}", file=sys.stderr)
    result = subprocess.run(command, cwd=HERE)
    if result.returncode == 0 and args.save:
        path = Path(args.save)
        print(f"已保存 {path}（{path.stat().st_size if path.exists() else 0} 字节）", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
