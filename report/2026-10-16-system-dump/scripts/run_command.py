#!/usr/bin/env python3
"""Run arbitrary commands inside a Sony ILCE-7CM2 over one service session.

    sudo ./.venv/bin/python run_command.py 'id; uname -a'
    sudo ./.venv/bin/python run_command.py --file commands.txt --json result.json
    sudo ./.venv/bin/python run_command.py --interactive

A session is expensive: the payload is armed at the start and the camera needs a
physical USB reconnect at the end. Every command therefore goes through **one**
session — pass several, a file, or `--interactive` to type them while it stays open.

Prerequisites match the other tools in this directory: the camera at the mass-storage
identity, no camera volume mounted, `sudo` (USB device nodes are root-only), and the
vendored Sony-PMCA-RE next to `usb_service_probe.py`. Command results are printed to
stderr; the service CLI's JSON event stream stays on stdout.
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


def interpreter():
    """Prefer the directory's venv (it carries pyusb); fall back to the caller's."""
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def build_command(commands, files, interactive, json_path, deadline, interpreter_path=None,
                  shell=False, shell_script=None, unmount=True):
    """Assemble the service CLI invocation that runs these commands in one session."""
    if not commands and not files and not interactive and not shell and not shell_script:
        raise ValueError("give at least one command, a --file, --interactive, or --shell")
    command = [interpreter_path or interpreter(), str(PROBE),
               "--approve-preload", APPROVAL, "--deadline", str(deadline)]
    for item in commands:
        command += ["--run-command", item]
    for path in files or ():
        command += ["--run-command-file", str(path)]
    if interactive:
        command += ["--run-command-interactive"]
    if shell:
        command += ["--shell"]
    if shell_script:
        command += ["--shell-script", str(shell_script)]
    if json_path:
        command += ["--run-command-json", str(json_path)]
    if unmount:
        # The desktop mounts the camera's own FAT partitions on insert; the service
        # session refuses to start while they are mounted, so unmount them first.
        command += ["--unmount-camera-volumes"]
    return command



MSC_PID = "0e8a"
STICKY_MODES = {"096f": "上一次会话结束后的残留模式", "0336": "服务模式", "02a9": "服务模式"}


def camera_state_hint():
    """Return True when the camera is at the mass-storage identity, False otherwise.

    The camera keeps the mode a finished session left it in, so a plain replug often
    brings it back as 096f/0336 and the session cannot start. Saying which mode it is
    in — and that a power cycle is what clears it — is the difference between a
    usable tool and a cryptic "no matching camera connected". How it gets cleared is
    host-dependent: Windows needs a power cycle, Linux a replug (2026-10-18 note).
    """
    listing = subprocess.run(["lsusb", "-d", "054c:"], capture_output=True, text=True).stdout
    if not listing.strip():
        return None
    return f":{MSC_PID}" in listing


def hint_message():
    listing = subprocess.run(["lsusb", "-d", "054c:"], capture_output=True, text=True).stdout
    if not listing.strip():
        return ("未检测到相机：请连接 USB，并在相机上选择「海量存储 / Mass Storage」"
                "（相机需开机）")
    for pid, what in STICKY_MODES.items():
        if f":{pid}" in listing:
            return (f"检测到 054c:{pid}（{what}）✗，服务会话需要 054c:{MSC_PID}。"
                    "恢复办法取决于主机：Windows 上请**断电重启**（关机再开机）后重新连接 USB；"
                    "Linux 上**重新插拔 USB 即可**（偶尔要先插一次回到 0336，再插一次）")
    return f"检测到相机，但身份不是 054c:{MSC_PID}（见 lsusb -d 054c: 输出）"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="*", help="One or more commands, each run with /bin/sh -c")
    parser.add_argument("--file", action="append", default=[], metavar="FILE",
                        help="Read commands from a file (one per line, '#' comments skipped)")
    parser.add_argument("--interactive", action="store_true",
                        help="Type commands while the session is open; 'exit' ends it")
    parser.add_argument("--shell", action="store_true",
                        help="Persistent in-camera shell: keeps cd/export and streams output")
    parser.add_argument("--shell-script", metavar="FILE",
                        help="Feed a script to the persistent shell (one command per line)")
    parser.add_argument("--json", metavar="FILE",
                        help="Write results (command, exit status, output) as JSON")
    parser.add_argument("--deadline", type=int, default=300, metavar="SECONDS",
                        help="Wall-clock cap for the session (default 300)")
    parser.add_argument("--keep-mounts", action="store_true",
                        help="Do not unmount volumes the desktop auto-mounted for the camera")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the service CLI invocation instead of running it")
    args = parser.parse_args(argv)

    try:
        command = build_command(args.command, args.file, args.interactive, args.json,
                                args.deadline,
                                interpreter_path=sys.executable if args.dry_run else None,
                                shell=args.shell, shell_script=args.shell_script,
                                unmount=not args.keep_mounts)
    except ValueError as error:
        parser.error(str(error))
    if args.dry_run:
        print(" ".join(command))
        return 0

    # Order matters: a missing or wrong-identity camera is reported before the
    # privilege check so the message names what to fix, not just "run as root".
    if not camera_state_hint():
        print(hint_message(), file=sys.stderr)
        return 3

    if os.geteuid() != 0:
        print("需要 root：USB 设备节点为 root 专属，请用 sudo 运行（见文件头用法）", file=sys.stderr)
        return 2

    count = len(args.command) + len(args.file)
    detail = f"{count} 条命令参数/文件" if count else "0 条预置命令"
    if args.interactive:
        detail += "，随后进入交互模式"
    print(f"在一次会话内执行：{detail}", file=sys.stderr)
    result = subprocess.run(command, cwd=HERE)
    if result.returncode != 0:
        print(f"服务会话返回 {result.returncode}；若相机停在 054c:096f，需手动重连一次 USB",
              file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
