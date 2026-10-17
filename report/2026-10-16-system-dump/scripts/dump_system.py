#!/usr/bin/env python3
"""One-shot dump of every flash partition on a Sony ILCE-7CM2 through its service interface.

The whole route is four stages, and the script keeps the two physical actions
explicit because they cannot be automated:

    1. check   the camera is at the mass-storage identity, no volume is mounted, the
               SD card carries the two-partition layout (exFAT for the camera, ext2
               for us) — or is rebuilt here with --format-card
    2. dump    the payload runs as root inside the camera, mounts the card's ext2
               partition and writes every plaintext partition to it (one session)
    3. fetch   the card moves to this computer; images are copied here and each copy
               is checked against the card
    4. verify  e2fsck on the ext2 images, digest comparison against files read over
               the service channel, and a content inventory against a reference tree

Usage:
    analysis/dump_system.py --card /dev/sda                 # prompts for the two swaps
    analysis/dump_system.py --card /dev/sda --format-card   # rebuild the card first
    analysis/dump_system.py --stage fetch --yes             # resume after the dump
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VENV_PYTHON = HERE / ".venv" / "bin" / "python"
APPROVAL = "arm-preload-once"
PROBE = "usb_service_probe.py"
CAMERA_ID = "054c:0e8a"
EXFAT_LABEL = "A7C2CAM"
EXT2_LABEL = "A7C2DUMP"
# Partitions whose images the reference tree can vouch for; the rest are inventoried
# by content only, because no reference copy of them exists.
REFERENCE_CHECKS = (("a7c2-rootfs.img", 16_777_216, (), ("/usr",)),
                    ("a7c2-usr.img", 807_403_520, ("/usr",), ()))
SUDO = ["sudo", "-n"]
# Differences that are already explained and belong to the reference tree, not to the
# dump: the local tree's copy was replaced with the instrumented build this project
# uses for QEMU runs, while the image holds the vendor original.
ALLOWED_DIFFERENCES = ("/usr/lib/libIMDB.so",)
NEXT_STAGE = {"check": "把卡插进相机并连接 USB，然后 --stage dump",
              "dump": "把卡插到电脑上，然后 --stage fetch",
              "fetch": "--stage verify"}


def announce(text):
    print(f"\n\033[1m== {text}\033[0m", flush=True)


def run(command, **kwargs):
    """Run a command, streaming its output, and fail loudly."""
    print(f"  $ {' '.join(str(part) for part in command)}", flush=True)
    result = subprocess.run([str(part) for part in command], **kwargs)
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(map(str, command))}")
    return result


def capture(command):
    result = subprocess.run([str(part) for part in command], capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout


def confirm(prompt, assume_yes):
    if assume_yes:
        print(f"  (--yes) {prompt}")
        return
    reply = input(f"  {prompt} [回车继续 / q 中止] ").strip().lower()
    if reply == "q":
        raise SystemExit("operator aborted")


# --- stage 1: preconditions -------------------------------------------------

def check_camera():
    listing = capture(["lsusb", "-d", CAMERA_ID])
    if CAMERA_ID not in listing:
        raise SystemExit(f"camera is not enumerated as {CAMERA_ID}; "
                         "reconnect the USB cable (a finished session leaves it at 054c:096f)")
    mounted = capture(["findmnt", "-rn", "-o", "TARGET,SOURCE"]).splitlines()
    busy = [line for line in mounted if re.search(r"/dev/sd[ef]\d", line)]
    if busy:
        raise SystemExit("camera volumes are mounted; unmount them first:\n  " + "\n  ".join(busy))
    print(f"  ✓ 相机已按 {CAMERA_ID} 枚举，且没有卷被挂载")


def card_partitions(device):
    """Read the card's partition table, without assuming blkid is installed."""
    listing = capture(["lsblk", "-o", "NAME,FSTYPE,LABEL", device])
    rows = [line.split() for line in listing.splitlines()[1:] if line.strip()]
    return {row[0]: row[1:] for row in rows if len(row) >= 2}


def mounts_of(device):
    """Mounted partitions of a card, as (mountpoint, source) pairs."""
    found = []
    for line in capture(["findmnt", "-rn", "-o", "TARGET,SOURCE"]).splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].startswith(device):
            found.append((fields[0], fields[1]))
    return found


def check_card(device, want_layout=True, read_partition=None):
    if not Path(device).exists():
        raise SystemExit(f"{device} does not exist; insert the SD card")
    size = capture(["lsblk", "-ndo", "SIZE", device]).strip()
    if size in ("", "0B"):
        raise SystemExit(f"no card in the reader at {device}; insert the card once it holds "
                         f"{EXFAT_LABEL} + {EXT2_LABEL}")
    rows = card_partitions(device)
    if want_layout:
        labels = {row[1] if len(row) > 1 else "" for row in rows.values()}
        if EXFAT_LABEL not in labels or EXT2_LABEL not in labels:
            raise SystemExit(f"{device} carries {sorted(label for label in labels if label)} "
                             f"instead of {EXFAT_LABEL} + {EXT2_LABEL}; "
                             "run with --format-card to rebuild it")
    # Only the partition this run reads has to be free. A desktop may mount the
    # camera-facing exFAT partition on insert, which is irrelevant to the dump and
    # was previously reported as "the card is mounted".
    source = f"{device}{read_partition}" if read_partition else None
    mounts = mounts_of(device)
    blocking = [(mount, mounted) for mount, mounted in mounts if mounted == source]
    if blocking:
        where = ", ".join(mount for mount, _ in blocking)
        raise SystemExit(f"{source} is mounted at {where}; unmount it first")
    if mounts:
        note = ", ".join(f"{mounted}→{mount}" for mount, mounted in mounts)
        print(f"  注：卡上其它分区被挂载（本次不读它们）：{note}")
    print(f"  ✓ {device} 布局就绪（待读 {source or '按需选择'} 未挂载）")


def format_card(device):
    announce(f"重建 {device} 上的 SD 卡布局（会清空整张卡）")
    if not capture(["lsblk", "-ndo", "TRAN", device]).strip().startswith("usb"):
        raise SystemExit(f"{device} is not on a USB transport; refusing to reformat")
    confirm("将清空这张卡，继续？", assume_yes=True)
    run(SUDO + ["parted", "-s", "-a", "optimal", device, "mklabel", "msdos",
                "mkpart primary 1MiB 440GiB", "mkpart primary 440GiB 100%"])
    subprocess.run(SUDO + ["partprobe", device], check=False)
    time.sleep(2)
    run(SUDO + ["mkfs.exfat", "-L", EXFAT_LABEL, f"{device}1"])
    # ext2, not ext4: the camera kernel only has ext2 among the journaling-free and
    # makes no ext4 driver available, and exfat is missing as well.
    run(SUDO + ["mkfs.ext2", "-F", "-m", "0", "-L", EXT2_LABEL, f"{device}2"])


# --- stage 2: dump -----------------------------------------------------------

def dump_command(timeout=1800, formatterboarding=None):
    command = SUDO + [str(VENV_PYTHON), PROBE,
                      "--deploy-shell", "--approve-preload", APPROVAL,
                      "--dump-to-card", "--deadline", str(timeout)]
    if formatterboarding:
        for spec in formatterboarding:
            command += ["--card-source", spec]
    return command


def summarize_session(events_path):
    """Pull the facts a reader needs out of a session's event log.

    The log is a full USB transcript; without this the operator has to read hundreds
    of kilobytes of frames to learn whether the dump finished, which the camera
    reported, and whether the session cleaned up after itself.
    """
    summary = {}
    for line in Path(events_path).read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = event.get("event")
        if name == "deployment_record":
            dump_record = event.get("card_dump") or {}
            images = dump_record.get("images") or []
            listing = dump_record.get("listing") or ""
            summary.update({
                "images": len(images),
                "names": images,
                "completed_marker": "done" in listing,
                "bytes": sorted({int(size) for size in re.findall(r"\s(\d{6,})\s", listing)}),
                "worker_stopped": event.get("worker_stopped"),
                "disarmed": event.get("disarmed"),
                "preload_neutralised_bytes": event.get("preload_state_after", "").count("\n"),
            })
        elif name == "error":
            summary["error"] = event.get("message")
        elif name == "restore_not_verified":
            summary["camera_returned_to_msc"] = False
    summary.setdefault("camera_returned_to_msc", True)
    return summary


def report_session(summary):
    if not summary:
        print("  （未找到会话记录）")
        return
    print(f"  相机侧镜像数   : {summary.get('images', 0)}")
    names = summary.get("names") or []
    if names:
        shown = ", ".join(names[:6]) + (f" … 共 {len(names)} 个" if len(names) > 6 else "")
        print(f"  分区           : {shown}")
    print(f"  完成标记       : {'✓ 已写 DONE' if summary.get('completed_marker') else '✗ 未见 DONE'}")
    print(f"  worker 已停止  : {summary.get('worker_stopped')}")
    print(f"  预加载已中和   : {summary.get('disarmed')}")
    if summary.get("error"):
        print(f"  错误           : {summary['error']}")
    if not summary.get("camera_returned_to_msc", True):
        print("  提示           : 相机未回到 054c:0e8a；重跑下个阶段前需手动重连一次 USB")


def stage_dump(args):
    announce("把 SD 卡插进相机，并用 USB 连接相机")
    confirm("卡片已在相机中、相机已连好？", args.yes)
    check_camera()
    announce("在相机内写入所有分区（约 2 GiB，需要几分钟）")
    run(dump_command(args.deadline), cwd=HERE)
    sessions = sorted((HERE / "evidence" / "usb-service").glob("run-*"))
    if sessions:
        announce("转储会话结果")
        report_session(summarize_session(sessions[-1] / "events.jsonl"))


# --- stage 3: fetch ----------------------------------------------------------

def fetch_commands(device, destination, partition=2):
    source = f"{device}1" if partition == 1 else f"{device}2"
    mountpoint = "/mnt/a7c2-fetch"
    return (SUDO + ["mkdir", "-p", mountpoint],
            SUDO + ["mount", "-o", "ro", source, mountpoint],
            ["python3", str(HERE / "copy_card_images.py"), mountpoint, str(destination)],
            SUDO + ["umount", mountpoint])


def stage_fetch(args):
    announce("把 SD 卡从相机取出，插到这台电脑上")
    for _ in range(args.card_wait):
        if Path(args.card).exists():
            break
        time.sleep(1)
    confirm("卡片已插到电脑上？", args.yes)
    destination = Path(args.out)
    destination.mkdir(parents=True, exist_ok=True)
    announce(f"复制到 {destination}")
    for command in fetch_commands(args.card, destination, args.card_partition):
        run(command)


# --- stage 4: verify ---------------------------------------------------------

def verify_commands(destination, reference=None):
    commands = []
    images = sorted(Path(destination).glob("*.img"))
    for image in images:
        if capture(["blkid", "-p", "-o", "value", "-s", "TYPE", str(image)]).strip() != "ext2":
            continue
        commands.append(["e2fsck", "-fn", str(image)])
    for name, size, include, exclude in REFERENCE_CHECKS:
        if not (Path(destination) / name).exists():
            continue
        command = ["python3", str(HERE / "verify_card_dump.py"), str(Path(destination) / name),
                   "--expect-bytes", str(size)]
        command += [part for prefix in include for part in ("--include-prefix", prefix)]
        command += [part for prefix in exclude for part in ("--exclude-prefix", prefix)]
        if name == "a7c2-usr.img":
            command += ["--strip-prefix", "/usr"]
        commands.append(command)
    if reference and Path(reference).exists():
        comparison = SUDO + ["python3", str(HERE / "verify_system_dump.py")]
        for name, prefix in (("a7c2-rootfs.img", ""), ("a7c2-usr.img", "/usr")):
            image = Path(destination) / name
            if image.exists():
                comparison += ["--image", f"{image}:{prefix}" if prefix else str(image)]
        if len(comparison) > 4:
            for path in ALLOWED_DIFFERENCES:
                comparison += ["--allow-difference", path]
            comparison += ["--reference", str(reference),
                           "--out", str(Path(destination) / "verification.json")]
            commands.append(comparison)
        command = SUDO + ["python3", str(HERE / "identify_partition_images.py")]
        command += [str(path) for path in images]
        command += ["--reference", str(reference),
                    "--out", str(Path(destination) / "partition-inventory.json")]
        commands.append(command)
    return commands


def stage_verify(args):
    announce("验证产物")
    for command in verify_commands(args.out, args.reference):
        run(command)
    images = sorted(Path(args.out).glob("*.img"))
    manifest = {"generated_by": "dump_system.py",
                "images": [{"name": image.name, "bytes": image.stat().st_size,
                            "sha256": capture(["sha256sum", str(image)]).split()[0]}
                           for image in images]}
    (Path(args.out) / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\n  共 {len(images)} 个镜像，清单写入 {Path(args.out) / 'manifest.json'}")


# --- driver ------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--card", default="/dev/sda", help="SD card block device (default /dev/sda)")
    parser.add_argument("--card-partition", type=int, default=2,
                        help="Card partition holding the images (2 = ext2, default)")
    parser.add_argument("--out", default=str(HERE / "output" / "system-dump"),
                        help="Where images and manifest are written")
    parser.add_argument("--reference", default=str(HERE / "qemu-runtime/analysis/menu-root"),
                        help="Local reference tree used for the content inventory")
    parser.add_argument("--stage", choices=("all", "check", "dump", "fetch", "verify"),
                        default="all", help="Run one stage only (resume after an interruption)")
    parser.add_argument("--format-card", action="store_true",
                        help="Rebuild the card's two-partition layout before dumping")
    parser.add_argument("--deadline", type=int, default=1800,
                        help="Wall-clock cap for the in-camera session (seconds)")
    parser.add_argument("--card-wait", type=int, default=0,
                        help="Wait up to N seconds for the card to appear")
    parser.add_argument("--yes", action="store_true", help="Do not prompt for physical steps")
    args = parser.parse_args(argv)

    if not VENV_PYTHON.exists():
        raise SystemExit(f"missing venv interpreter: {VENV_PYTHON}\n"
                         "run: uv pip install --python analysis/.venv/bin/python "
                         "-r analysis/requirements.txt")

    if args.format_card:
        announce("准备 SD 卡")
        format_card(args.card)

    if args.stage in ("all", "check"):
        announce("1/4 检查前置条件")
        check_card(args.card, want_layout=not args.format_card,
                   read_partition=args.card_partition)
    if args.stage in ("all", "dump"):
        announce("2/4 在相机内转储")
        stage_dump(args)
    if args.stage in ("all", "fetch"):
        announce("3/4 取回镜像")
        stage_fetch(args)
        check_card(args.card, read_partition=args.card_partition)
    if args.stage in ("all", "verify"):
        announce("4/4 验证")
        stage_verify(args)

    if args.stage == "all":
        announce("完成")
        print(f"  镜像与清单: {args.out}")
        print(f"  说明文档  : {Path(args.out) / 'README.md'}")
    else:
        print(f"\n  {args.stage} 阶段完成；继续下一步："
              f"{NEXT_STAGE.get(args.stage, '无')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
