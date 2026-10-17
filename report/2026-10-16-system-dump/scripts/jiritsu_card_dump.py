"""Write camera partition images onto a card the camera's kernel can mount.

The camera's userspace stack only understands FAT/exFAT, so the card carries two
partitions: an exFAT one the camera accepts, and an ext2 one that the camera's
kernel can mount (its kernel has ext2 but not exFAT or ext4). The root worker
mounts the ext2 partition, dd's the partitions onto it, and this driver waits
inside the same session so a later mode switch cannot drop the mount.

Two camera quirks shape the commands sent here:

* The worker captures a command's output by reading its pipe until EOF *before*
  it reaps the child, so a background job that inherits that pipe would block the
  worker until the job ends. The dump subshell therefore redirects its own stdout
  and stderr to /dev/null, leaving the pipe to close as soon as the shell exits.
* The busybox build has no `head`, `tail` or `sha256sum`; a command that calls a
  missing applet exits 127 and its output is lost to the broken pipe. The driver
  reports which tools exist instead of assuming any.
"""
import re
import time

from jiritsu_preload_deploy import submit_command

MOUNT_DEVICE = "/dev/mmca2"
MOUNT_POINT = "/tmp/sd"
POLL_INTERVAL = 3.0
POLL_ATTEMPTS = 300          # ~15 minutes at 3 s intervals
TOOLS = ("sh", "ls", "cat", "dd", "rm", "mkdir", "mount", "sync", "df", "md5sum", "sed")
DEFAULT_IMAGES = (("cnflasha7", "a7c2-rootfs.img"), ("cnflasha15", "a7c2-usr.img"))


def parse_image_spec(text):
    """Parse `DEVICE:NAME` into the pair the driver expects."""
    device, separator, name = text.partition(":")
    if not separator or not device or not name or "/" in name:
        raise ValueError(f"expected DEVICE:NAME (for example nflasha7:a7c2-rootfs.img), got {text!r}")
    return (device.lstrip("/").removeprefix("dev/"), name)


def parse_block_devices(listing):
    """Extract block device paths from `ls -l /dev/cnflasha* ...` output.

    Only lines beginning with `b` are block devices (symlinks start with `l`), and
    whole-disk nodes without a partition suffix are ignored: the camera exposes the
    plaintext view as `cnflasha<N>` and the boot slots as `nflashaB<N>`.
    """
    devices = []
    for line in listing.decode("ascii", "replace").splitlines():
        if not line.startswith("b"):
            continue
        for field in line.split():
            if not field.startswith("/dev/"):
                continue
            name = field.rsplit("/", 1)[1]
            if re.search(r"(?:\d+|B\d+)$", name) and name not in devices:
                devices.append(field)
    return devices


# Canonical image names. Discovery walks the camera's device nodes, whose names
# (`cnflasha7`) say nothing about what the partition holds, so the two partitions the
# dumps are usually about keep their documented names and the rest keep the logical
# `nflashaN` name the partition table uses.
IMAGE_NAMES = {"cnflasha7": "a7c2-rootfs.img", "cnflasha15": "a7c2-usr.img"}


def image_name(device):
    """Name an image after the partition, not after the device node it was read from."""
    base = device.rsplit("/", 1)[1]
    if base in IMAGE_NAMES:
        return IMAGE_NAMES[base]
    return f"{base[1:] if base.startswith('c') else base}.img"


def discover_image_specs(transport, requests, log):
    """Ask the camera which plaintext devices exist and name images after them."""
    listing = submit_command(
        transport, requests, "ls -l /dev/cnflasha* /dev/nflashaB* 2>&1; true",
        "discover", log)
    devices = parse_block_devices(listing)
    if not devices:
        raise ValueError(f"no plaintext devices found on the camera: {listing[-200:]!r}")
    log("partitions_discovered", devices=[d.rsplit("/", 1)[1] for d in devices])
    return tuple((device.lstrip("/").removeprefix("dev/"), image_name(device))
                 for device in devices)


def card_dump_driver(images=None, mount_device=MOUNT_DEVICE,
                     mount_point=MOUNT_POINT, poll_interval=POLL_INTERVAL,
                     poll_attempts=POLL_ATTEMPTS):
    """Return a deploy driver that writes each device to `mount_point`."""
    if images is not None and not images:
        raise ValueError("an explicit image list must not be empty")

    def driver(transport, requests, log, record):
        sources = tuple(images) if images else discover_image_specs(transport, requests, log)
        probes = "; ".join(
            f'command -v {tool} >/dev/null 2>&1 && echo "tool:{tool}=yes" '
            f'|| echo "tool:{tool}=NO"' for tool in TOOLS)
        mount = submit_command(
            transport, requests,
            f"mkdir -p {mount_point}; {probes}; ls -l /dev/mmca* 2>&1; "
            f"for D in {mount_device} /dev/mmca1; do mount -t ext2 $D {mount_point} && break; done 2>&1; "
            f"M=$?; echo MOUNT_RC=$M; ls -l {mount_point} 2>&1; true",
            "mnt", log)
        if b"MOUNT_RC=0" not in mount:
            raise ValueError(f"card mount failed on {mount_device}: {mount[-300:]!r}")
        missing = [line.split(b"=", 1)[0].split(b":", 1)[1].decode()
                   for line in mount.splitlines() if line.strip().endswith(b"=NO")]
        if missing:
            log("camera_tools_missing", tools=missing)
        log("card_mounted", device=mount_device, point=mount_point)

        steps = " ; ".join(
            f"dd if=/dev/{device} of={mount_point}/{name} bs=1M 2>{mount_point}/{name}.log"
            for device, name in sources)
        submit_command(
            transport, requests,
            f"rm -f {mount_point}/DONE; ( {steps} ; sync ; "
            f"ls -l {mount_point} > {mount_point}/LISTING ; echo done > {mount_point}/DONE ) "
            f">/dev/null 2>&1 & echo STARTED; true",
            "start", log)
        log("card_dump_started", images=[name for _, name in sources])

        listing = b""
        for attempt in range(1, poll_attempts + 1):
            time.sleep(poll_interval)
            listing = submit_command(
                transport, requests,
                f"cat {mount_point}/DONE 2>/dev/null; ls -l {mount_point}/*.img 2>&1; true",
                f"p{attempt:03d}", log)
            if b"done" in listing and b".img" in listing:
                log("card_dump_finished", attempt=attempt)
                break
        else:
            raise ValueError("card dump did not finish inside the polling window")

        report = submit_command(
            transport, requests,
            f"cat {mount_point}/LISTING 2>&1; echo ---LOGS---; "
            + " ; ".join(f'echo "== {name}.log"; cat {mount_point}/{name}.log 2>&1'
                         for _, name in sources)
            + f" ; echo ---DF---; df -h {mount_point} 2>&1; true",
            "report", log)
        submit_command(transport, requests,
                       f"sync; umount {mount_point} 2>&1; echo UMNT_RC=$?; true", "umnt", log)

        summary = {"mount_device": mount_device, "mount_point": mount_point,
                   "images": [name for _, name in sources],
                   "listing": listing.decode("ascii", "replace"),
                   "report": report.decode("ascii", "replace")}
        record["card_dump"] = summary
        record["payload_ran"] = True
        record["mailbox_ok"] = True
        log("card_dump_unmounted", point=mount_point)
        return report          # the bytes contract the deploy harness expects

    return driver
