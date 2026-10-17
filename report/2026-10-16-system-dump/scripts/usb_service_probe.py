"""Sony Senser entry verification: authentication + ProductInfo + session exit.

Default: descriptor enumeration only. --authenticate may change USB mode and
claim interface 0. It refuses mounted camera volumes. --read-version also sends
two fixed Jiritsu requests to select/read a version file (may mount service storage).
--system-dump-preflight checks six fixed mount/preload/device paths with bounded replies.
--inspect-sd mounts the first medium, reads its mapping, and unmounts it.
--write-check writes one 1 KiB random file inside the service Work area and reads it back.
--one-shot-probe is disabled pending deployment and recovery validation.
Supported options do not upload payloads or alter preload configuration.
Uses the locally archived PMCA authentication/crypto implementation.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

# Sizes observed from the camera's own /proc/partitions (KiB x 1024).
PARTITION_BYTES = {
    "pnflasha7": 16_777_216,
    "pnflasha15": 807_403_520,
    "pnflasha5": 314_572_800,
    "pnflasha8": 67_108_864,
    "pnflasha1": 25_100_288,
    "pnflasha3": 78_643_200,
    "pnflasha11": 314_572_800,
}

ANALYSIS = Path(__file__).resolve().parent
PMCA = ANALYSIS / "vendor" / "pmca-adamwuqwq"
sys.path.insert(0, str(PMCA))

MAX_READ_TARGETS = 512


# Files our own checks leave on the camera's internal flash. The preload payload and
# the worker mailbox are deliberately NOT here: they are the mechanism (the loader reads
# the payload at every exec), not droppings.
INTERNAL_DROPPINGS = ("/tmp/jiritsu/a7c2-probe-result.bin",
                      "/tmp/jiritsu/a7c2-write-check.bin",
                      "/tmp/jiritsu/a7c2-chunk.bin",
                      "/etc/a7c2-write-check.bin",
                      "/setting/a7c2-repo-test.bin",
                      "/etc/profile-ran.txt")
CLEANUP_COMMAND = "rm -f " + " ".join(INTERNAL_DROPPINGS)

# Mount the card before the session's own work. The worker is the only shell we have, so
# this is the earliest moment a mount can happen; everything card-resident then works for
# the rest of the session. The preload payload itself cannot move to the card: it is read
# by the loader at exec time, before any shell exists to mount anything.
MOUNT_CARD_COMMAND = ("mkdir -p /tmp/sd; [ -b /dev/mmca2 ] && "
                      "mount -t ext2 /dev/mmca2 /tmp/sd 2>&1; echo CARD_MOUNT_RC=$?; "
                      "ls -d /tmp/sd 2>&1")


def read_payload(filename):
    """Read a built payload from the first standard location that has it.

    The working project keeps the artifacts beside the scripts; a published copy keeps
    them in a sibling `payload/` directory. Searching both keeps one script usable in
    either layout, and the failure names every location tried. The caller still checks
    the digest, so a wrong file is caught regardless of where it came from.
    """
    candidates = (ANALYSIS / filename,
                  ANALYSIS / "payload" / filename,
                  ANALYSIS.parent / "payload" / filename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_bytes()
    raise FileNotFoundError(f"{filename} not found; looked in "
                            + ", ".join(str(candidate) for candidate in candidates))


def card_images_from_sources(sources):
    """Explicit DEVICE:NAME pairs for --dump-to-card, or None to discover them.

    Discovery is the default because the camera exposes its partitions as
    `cnflasha*` device nodes that differ between bodies and firmware versions; an
    explicit list is only used when the operator asks for it.
    """
    if not sources:
        return None
    from jiritsu_card_dump import parse_image_spec
    return tuple(parse_image_spec(spec) for spec in sources)


def collect_read_targets(single, lists):
    """Merge --read-file-list contents with explicit --read-file values.

    Listed batches come first so a stress target passed with --read-file is read
    last: if it fails, the batch before it is already saved.
    """
    targets = []
    for list_path in lists:
        entries = [line.strip() for line in Path(list_path).read_text().splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
        targets.extend(entries)
    targets.extend(single)
    for target in targets:
        if not target.startswith("/"):
            raise ValueError(f"Read target must be absolute: {target!r}")
    if len(targets) > MAX_READ_TARGETS:
        raise ValueError(f"At most {MAX_READ_TARGETS} read targets per session")
    return targets


def camera_mounts():
    """Match mounted block devices to this camera family through sysfs ancestry."""
    mounted = {}
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        mounted.setdefault(fields[2], []).append(fields[4])
    result = []
    for block in Path("/sys/class/block").iterdir():
        devnum = (block / "dev").read_text().strip()
        if devnum not in mounted:
            continue
        for parent in block.resolve().parents:
            vendor, product = parent / "idVendor", parent / "idProduct"
            if vendor.exists() and product.exists():
                if (vendor.read_text().strip().lower() == "054c" and
                        product.read_text().strip().lower() in ("0e8a", "0336")):
                    result.append({"block": block.name, "mounts": mounted[devnum]})
                break
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authenticate", action="store_true")
    parser.add_argument("--read-version", action="store_true",
                        help="Authenticate and read only the fixed Jiritsu version path")
    parser.add_argument("--system-dump-preflight", action="store_true",
                        help="Read six fixed mount/preload/device paths; no file-content writes")
    parser.add_argument("--inspect-sd", action="store_true",
                        help="Mount SD, read service media mapping, unmount; no SD file writes")
    parser.add_argument("--write-check", action="store_true",
                        help="Write and read back one 1 KiB file inside the service Work area")
    parser.add_argument("--sd-write-check", action="store_true",
                        help="Mount the SD medium, write 1 KiB to its root, read it back, unmount")
    parser.add_argument("--write-file", action="append", metavar="CAMERA_PATH=LOCAL", default=[],
                        help="Write a local file to a camera-absolute path with the MSC-layer "
                             "writer, then read it back to verify; repeatable")
    parser.add_argument("--traversal-write-check", action="store_true",
                        help="Legacy capability pre-check: writes 256 bytes to "
                             "/etc/a7c2-write-check.bin, i.e. onto the camera's "
                             "12 MiB /tmp+/etc+/var partition. Arming now verifies its "
                             "own write by read-back, so this is no longer needed to "
                             "show that /etc is writable; the file stays behind until "
                             "removed with root.")
    parser.add_argument("--deploy-probe", action="store_true",
                        help="Arm the preload entry with the read-only probe and send one DEBUG request")
    parser.add_argument("--deploy-shell", action="store_true",
                        help="Arm the preload entry with the command-channel payload and send one DEBUG request")
    parser.add_argument("--approve-preload", metavar="TOKEN",
                        help="Required approval token for either deploy flag")
    parser.add_argument("--dump-partition", action="append", metavar="DEVICE", default=[],
                        help="Dump a block device to the host in verified chunks; repeatable")
    parser.add_argument("--allow-internal-chunking", action="store_true",
                        help="Permit --dump-partition to hold 1 MiB chunks on the camera's own "
                             "flash (it once filled a 12 MiB partition); prefer --dump-to-card")
    parser.add_argument("--dump-out", metavar="DIR",
                        help="Directory for dumped images (defaults to the evidence directory)")
    parser.add_argument("--dump-chunk", type=int, default=4 * 1024 * 1024, metavar="BYTES",
                        help="Chunk size for --dump-partition (default 4 MiB)")
    parser.add_argument("--dump-to-card", metavar="DEVICE", nargs="?", const="",
                        help="Write the images onto a mounted card device on the camera "
                             "(default /dev/mmca2) and leave them there; no host transfer")
    parser.add_argument("--no-mount-card", action="store_true",
                        help="Do not mount the card as the first command of a command/shell "
                             "session (it is mounted by default so card-resident data works)")
    parser.add_argument("--unmount-camera-volumes", action="store_true",
                        help="Unmount volumes the desktop auto-mounted for this camera before "
                             "authenticating (matched by sysfs ancestry, never other disks)")
    parser.add_argument("--run-command", action="append", metavar="SHELL", default=[],
                        help="Run a command inside the camera; repeatable, all in one session")
    parser.add_argument("--run-command-file", action="append", metavar="FILE", default=[],
                        help="Read commands from a file (one per line, # comments skipped)")
    parser.add_argument("--mem-read", action="append", metavar="SPEC", default=[],
                        help="Read memory: SOURCE:ID:OFFSET:SIZE[:NAME] "
                             "(SOURCE = phys for /dev/mem, pid for /proc/<pid>/mem)")
    parser.add_argument("--mem-read-card-only", action="store_true",
                        help="Leave the region on the card instead of pulling it over USB "
                             "(use for whole-RAM dumps)")
    parser.add_argument("--mem-read-also", action="append", metavar="SHELL", default=[],
                        help="Also run this command in the same session (reports its output)")
    parser.add_argument("--mem-read-save", metavar="FILE",
                        help="Write the retrieved region(s) here; with several regions this is a prefix")
    parser.add_argument("--shell", action="store_true",
                        help="Open a persistent in-camera shell (keeps cd/export, streams output)")
    parser.add_argument("--shell-script", metavar="FILE",
                        help="Feed this script to the persistent shell, one line per command")
    parser.add_argument("--run-command-interactive", action="store_true",
                        help="Read commands from stdin while the session is open")
    parser.add_argument("--run-command-json", metavar="FILE",
                        help="Write command results (command, exit status, output) as JSON here")
    parser.add_argument("--card-source", action="append", metavar="DEVICE:NAME", default=[],
                        help="Source device and image name for --dump-to-card, repeatable "
                             "(default pnflasha7:a7c2-rootfs.img and pnflasha15:a7c2-usr.img)")
    parser.add_argument("--mailbox-command", metavar="SHELL",
                        help="Command the deployed worker should run as its first task")
    parser.add_argument("--clear-preload", action="store_true",
                        help="Recovery: overwrite /etc/ld.so.preload with whitespace and confirm by read-back")
    parser.add_argument("--read-file", action="append", metavar="PATH", default=[],
                        help="Read one camera file by absolute path; repeatable")
    parser.add_argument("--read-file-list", action="append", metavar="FILE", default=[],
                        help="Read every path listed in a local newline-separated file")
    parser.add_argument("--verify-crypto", action="store_true",
                        help="Also read each file with the masked branch and compare against the recovered stream")
    parser.add_argument("--crypto-check", action="append", metavar="PATH", default=[],
                        help="Read one file both ways and compare the XOR against the recovered stream")
    parser.add_argument("--deadline", type=int, default=90, metavar="SECONDS",
                        help="Wall-clock cap for the service phase (default 90)")
    parser.add_argument("--io-timeout", type=int, default=30000, metavar="MILLISECONDS",
                        help="Per-bulk-transfer USB timeout (default 30000)")
    parser.add_argument("--one-shot-probe", action="store_true",
                        help="Disabled: execution and recovery integration is not validated")
    args = parser.parse_args()
    if args.one_shot_probe:
        parser.error("One-shot deployment is disabled; offline tests do not establish safe camera execution")
    if (args.dump_partition or args.dump_to_card is not None or args.run_command
            or args.run_command_file or args.run_command_interactive
            or args.shell or args.shell_script or args.mem_read):
        args.deploy_shell = True
    if args.dump_to_card is not None and args.dump_partition:
        parser.error("Choose either --dump-to-card or --dump-partition")
    if args.deploy_probe or args.deploy_shell:
        if args.deploy_probe and args.deploy_shell:
            parser.error("Choose one deploy target")
        if not args.approve_preload:
            parser.error("Deployment needs --approve-preload arm-preload-once")
    args.authenticate = args.authenticate or args.read_version or args.system_dump_preflight or args.inspect_sd or args.write_check or bool(args.read_file) or bool(args.read_file_list) or args.sd_write_check or args.traversal_write_check or bool(args.write_file) or args.deploy_probe or args.deploy_shell or args.clear_preload
    read_targets = collect_read_targets(args.read_file, args.read_file_list)
    import usb.core
    import usb.util

    os.umask(0o077)
    out = ANALYSIS / "evidence" / "usb-service" / datetime.now().strftime("run-%Y%m%d-%H%M%S-%f")
    out.mkdir(parents=True)
    events = []
    physical_port = None

    def log(event, **fields):
        record = {"event": event, **fields}
        events.append(record)
        with (out / "events.jsonl").open("a") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def find(pid=None):
        devices = list(usb.core.find(find_all=True, idVendor=0x054c))
        devices = [d for d in devices if d.idProduct in (0x0e8a, 0x0336)]
        if pid is not None:
            devices = [d for d in devices if d.idProduct == pid]
        if physical_port is not None:
            devices = [d for d in devices
                       if (d.bus, tuple(d.port_numbers or ())) == physical_port]
        if len(devices) > 1:
            raise RuntimeError("Multiple matching cameras; refusing ambiguous selection")
        return devices[0] if devices else None

    def wait_for(pid, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            dev = find(pid)
            if dev is not None:
                return dev
            time.sleep(0.25)
        raise RuntimeError(f"USB identity 054c:{pid:04x} did not appear")

    current = None
    started = False
    detached = False
    success = False
    failure = None
    version_read = False
    system_preflight_complete = False
    sd_probe_complete = False
    write_check_record = None
    file_records = []
    restored_ok = False
    # Accumulators the summary reads must exist before the try block: an early
    # failure (device open, authentication) would otherwise raise
    # UnboundLocalError here and hide the real error.
    deployment = None
    clear_record = None
    sd_write_record = None
    traversal_record = None
    read_failures = []
    write_file_records = []
    crypto_records = []

    def deadline(*_):
        raise TimeoutError("Service experiment deadline exceeded")

    try:
        current = find()
        if current is None:
            raise RuntimeError("No matching camera connected")
        physical_port = (current.bus, tuple(current.port_numbers or ()))
        if args.read_version or args.system_dump_preflight or args.inspect_sd or args.write_check or read_targets:
            if current.idProduct != 0x0e8a or not physical_port[1]:
                raise RuntimeError("Version probe requires the MSC identity and a known physical port")
            product = usb.util.get_string(current, current.iProduct)
            if product != "ILCE-7CM2":
                raise RuntimeError(f"Unexpected camera product: {product!r}")
            log("target_confirmed", product=product, physical_port=physical_port)
        cfg = current.get_active_configuration()
        interface = cfg[(0, 0)]
        endpoints = [{"address": f"0x{e.bEndpointAddress:02x}",
                      "attributes": e.bmAttributes, "max_packet": e.wMaxPacketSize}
                     for e in interface]
        log("identified", vid=f"{current.idVendor:04x}", pid=f"{current.idProduct:04x}",
            bus=current.bus, address=current.address,
            interface=interface.bInterfaceNumber, endpoints=endpoints)
        mounts = camera_mounts()
        log("camera_mounts", volumes=mounts)
        if not args.authenticate:
            success = True
            return
        if mounts and args.unmount_camera_volumes:
            for volume in mounts:
                for mountpoint in volume["mounts"]:
                    log("camera_volume_unmounting", block=volume["block"], mount=mountpoint)
                    # udisksctl is what the desktop's automount registers with, so it
                    # releases the volume cleanly; plain umount is the fallback.
                    result = subprocess.run(["udisksctl", "unmount", "-b", f"/dev/{volume['block']}"],
                                            capture_output=True, text=True)
                    if result.returncode != 0:
                        subprocess.run(["umount", mountpoint], capture_output=True, check=False)
            mounts = camera_mounts()
            if mounts:
                remaining = ", ".join(f"{v['block']}→{m}" for v in mounts for m in v["mounts"])
                raise RuntimeError(f"Camera volumes are still mounted ({remaining}); "
                                   "close whatever holds them (`sudo fuser -vm <mountpoint>`) "
                                   "and retry, or unmount them yourself")
        if mounts:
            raise RuntimeError("Camera volumes are mounted; safely unmount before authentication "
                               "(pass --unmount-camera-volumes to do it automatically)")
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(max(30, args.deadline))

        from pmca.usb.sony import SonySenserAuthDevice, SonySenserDevice, SonySenserCamera
        from pmca.usb.driver.generic import GenericUsbDriver, GenericUsbException
        from pmca.usb.driver.generic.libusb import UsbBackend

        class DiagnosingBackend(UsbBackend):
            """Keeps the underlying USBError text, which the vendor backend discards."""

            def __init__(self, device, timeout_ms):
                super().__init__(device)
                self.dev.default_timeout = timeout_ms

            def read(self, ep, length, timeout=None):
                try:
                    return self.dev.read(ep, length, timeout).tobytes()
                except usb.core.USBError as exc:
                    raise GenericUsbException(
                        f"bulk read of {length} bytes failed: {type(exc).__name__}: {exc} "
                        f"(errno={getattr(exc, 'errno', None)}, "
                        f"backend_error_code={getattr(exc, 'backend_error_code', None)})"
                    ) from exc

        sources = {}
        for relative in ("pmca/usb/sony.py", "pmca/usb/crypto.py", "pmca/usb/constants.py",
                         "pmca/usb/driver/generic/__init__.py", "pmca/usb/driver/generic/libusb.py"):
            sources[relative] = hashlib.sha256((PMCA / relative).read_bytes()).hexdigest()
        (out / "sources.json").write_text(json.dumps(sources, indent=2) + "\n")
        local_sources = {name: hashlib.sha256((ANALYSIS / name).read_bytes()).hexdigest()
                         for name in ("usb_service_probe.py", "jiritsu_version_read.py",
                                      "jiritsu_write_check.py", "jiritsu_read_file.py",
                                      "jiritsu_crypto.py")}
        (out / "probe-sources.json").write_text(json.dumps(local_sources, indent=2) + "\n")
        for name in local_sources:
            (out / f"source-{name}").write_bytes((ANALYSIS / name).read_bytes())

        class TracedAuth(SonySenserAuthDevice):
            def _sendAuthPacket(self, command, data=b""):
                result, reply = super()._sendAuthPacket(command, data)
                fields = {"pid": f"{self.driver.getId()[1]:04x}",
                          "command": command, "result": result, "reply_size": len(reply)}
                if command == 5 and reply:
                    fields["success_byte"] = reply[0]
                log("authentication_exchange", **fields)
                return result, reply

        def open_driver(dev):
            nonlocal detached
            backend = DiagnosingBackend(dev, max(1000, args.io_timeout))
            detached = dev.is_kernel_driver_active(0)
            backend.reset()  # detach kernel driver, NOT a physical USB reset
            usb.util.claim_interface(dev, 0)
            return GenericUsbDriver(backend)

        initial_pid = current.idProduct
        driver = open_driver(current)
        if initial_pid == 0x0e8a:
            auth = TracedAuth(driver)
            started = True
            auth.start()  # PMCA tolerates the documented Start control-request stall
            auth.authenticate()
            log("mass_storage_authentication_passed")
            usb.util.dispose_resources(current)
            detached = False
            current = wait_for(0x0336)
            log("service_identity_seen", bus=current.bus, address=current.address)
            driver = open_driver(current)

        auth = TracedAuth(driver)
        started = True
        auth.start()
        auth.authenticate()
        log("service_authentication_passed")
        transport = SonySenserDevice(driver)
        info = SonySenserCamera(transport).readHasp()
        (out / "product-info.bin").write_bytes(info)
        log("product_info_read", bytes=len(info), sha256=hashlib.sha256(info).hexdigest())
        if args.read_version:
            from jiritsu_version_read import read_version
            content = read_version(transport, log)
            (out / "version.bin").write_bytes(content)
            version_read = True
            log("version_file_read", bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        def save_preflight_file(name, content):
            (out / f"preflight-{name}.bin").write_bytes(content)
            log("system_preflight_content", name=name, bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest())

        if args.system_dump_preflight:
            from jiritsu_version_read import read_system_preflight
            results = read_system_preflight(transport, log, save_preflight_file)
            (out / "system-preflight.json").write_text(json.dumps(results, indent=2) + "\n")
            system_preflight_complete = True
        if args.inspect_sd:
            from jiritsu_version_read import inspect_sd_mapping
            inspect_sd_mapping(transport, log, save_preflight_file)
            sd_probe_complete = True
        if args.write_check:
            from jiritsu_write_check import run_write_check, WRITE_SIZE
            payload = os.urandom(WRITE_SIZE)
            write_check_record = run_write_check(transport, payload, log)
            (out / "write-check-payload.bin").write_bytes(payload)
            (out / "write-check.json").write_text(json.dumps(write_check_record, indent=2) + "\n")
        for index, remote in enumerate(read_targets):
            from jiritsu_read_file import JiritsuResultError, read_file, verify_stream
            try:
                if args.verify_crypto:
                    content, crypto_record = verify_stream(transport, remote, log)
                    (out / f"file-{index}-crypto.json").write_text(json.dumps(crypto_record, indent=2) + "\n")
                else:
                    content = read_file(transport, remote, log)
            except JiritsuResultError as exc:
                # application-level refusal: the stream is still in sync, keep going
                read_failures.append({"path": remote, "result": hex(exc.result)})
                log("read_file_refused", path=remote, result=hex(exc.result))
                continue
            name = remote.strip("/").replace("/", "_") or "root"
            (out / f"file-{index}-{name}").write_bytes(content)
            record = {"path": remote, "bytes": len(content),
                      "sha256": hashlib.sha256(content).hexdigest(),
                      "local_name": f"file-{index}-{name}"}
            file_records.append(record)
            log("read_file_saved", **record)
        if file_records:
            (out / "read-files.json").write_text(json.dumps(file_records, indent=2) + "\n")
        if read_failures:
            (out / "read-failures.json").write_text(json.dumps(read_failures, indent=2) + "\n")
        for index, remote in enumerate(args.crypto_check):
            from jiritsu_read_file import verify_stream
            content, crypto_record = verify_stream(transport, remote, log)
            (out / f"crypto-check-{index}.json").write_text(json.dumps(crypto_record, indent=2) + "\n")
            name = remote.strip("/").replace("/", "_") or "root"
            (out / f"crypto-{index}-{name}").write_bytes(content)
            crypto_records.append(crypto_record)
        if crypto_records:
            (out / "crypto-checks.json").write_text(json.dumps(crypto_records, indent=2) + "\n")
        if args.sd_write_check:
            from jiritsu_sd_write import run_sd_write_check, WRITE_SIZE as SD_WRITE_SIZE
            sd_payload = os.urandom(SD_WRITE_SIZE)
            sd_write_record = run_sd_write_check(transport, sd_payload, log)
            (out / "sd-write-payload.bin").write_bytes(sd_payload)
            (out / "sd-write.json").write_text(json.dumps(sd_write_record, indent=2) + "\n")
        for spec in args.write_file:
            if "=" not in spec:
                raise ValueError(f"--write-file wants CAMERA_PATH=LOCAL, got {spec!r}")
            camera_path, local = spec.split("=", 1)
            payload = Path(local).read_bytes()
            from jiritsu_write_file import write_file_verified
            file_record = write_file_verified(transport, camera_path, payload, log)
            write_file_records.append(file_record)
            if file_record.get("verified") is not True:
                success = False
                failure = (f"write-file did not verify for {camera_path}: "
                           f"{file_record.get('error') or file_record.get('readback_error')}")
        if not (args.run_command or args.run_command_file or args.shell
                or args.shell_script or args.mem_read):
            log("left_behind", reason="no command channel in this session, and the service "
                                      "channel cannot delete files",
                files=list(INTERNAL_DROPPINGS))

        if write_file_records:
            (out / "write-file.json").write_text(
                json.dumps(write_file_records, indent=2, ensure_ascii=False) + "\n")

        if args.deploy_probe or args.deploy_shell:
            from jiritsu_preload_deploy import APPROVAL_TOKEN, deploy_and_run
            pinned = {
                "probe": ("system_dump_probe.arm64.so",
                          "5a95d422dff2134b055e159f786184b2929553c3416f0a0b8d847aee816bc5f1"),
                "shell": ("a7c2_shell.arm64.so",
                          "d261855eb1ea461a77c7847fefef7e6960d4495433d245a9eeb458eef0b35e6e"),
            }
            kind = "probe" if args.deploy_probe else "shell"
            filename, expected = pinned[kind]
            payload = read_payload(filename)
            actual = hashlib.sha256(payload).hexdigest()
            if actual != expected:
                raise ValueError(f"Local {filename} is not the reviewed artifact: {actual[:16]}…")
            nonce = secrets.token_hex(4) if kind == "shell" else None
            (out / f"deployed-{filename}").write_bytes(payload)
            log("deployment_starting", kind=kind, filename=filename, bytes=len(payload),
                sha256=actual, nonce=nonce)
            extra = {}
            if args.mailbox_command:
                if kind != "shell":
                    raise ValueError("--mailbox-command only applies to the shell payload")
                extra["probe_command"] = args.mailbox_command
            if args.dump_to_card is not None:
                if kind != "shell":
                    raise ValueError("--dump-to-card needs the shell payload")
                from jiritsu_card_dump import MOUNT_DEVICE, card_dump_driver
                images = card_images_from_sources(args.card_source)
                extra["driver"] = card_dump_driver(
                    images=images, mount_device=args.dump_to_card or MOUNT_DEVICE)
                nonce = None
            if args.mem_read:
                if kind != "shell":
                    raise ValueError("--mem-read needs the shell payload")
                from jiritsu_memread import memread_driver, parse_spec
                specs = [parse_spec(item) for item in args.mem_read]
                extra["driver"] = memread_driver(specs, save=args.mem_read_save,
                                                 commands=args.mem_read_also,
                                                 card_only=args.mem_read_card_only)
                nonce = None
            elif args.shell or args.shell_script:
                if kind != "shell":
                    raise ValueError("--shell needs the shell payload")
                from jiritsu_shell import shell_driver
                script_lines = []
                if args.shell_script:
                    for line in Path(args.shell_script).read_text().splitlines():
                        if line.strip() and not line.lstrip().startswith("#"):
                            script_lines.append(line)
                if script_lines and not args.no_mount_card:
                    script_lines.insert(0, MOUNT_CARD_COMMAND)
                    log("card_mount_queued", point="/tmp/sd")
                if script_lines:
                    script_lines.append(CLEANUP_COMMAND)
                    log("cleanup_queued", files=list(INTERNAL_DROPPINGS))
                elif not args.shell:
                    raise ValueError("--shell needs --shell-script or interactive mode")
                extra["driver"] = shell_driver(script_lines, interactive=args.shell,
                                               json_path=None)
                nonce = None
            elif (args.run_command or args.run_command_file or args.run_command_interactive):
                if kind != "shell":
                    raise ValueError("--run-command needs the shell payload")
                from jiritsu_run_command import command_runner_driver
                commands = [] if args.no_mount_card else [MOUNT_CARD_COMMAND]
                if commands:
                    log("card_mount_queued", point="/tmp/sd")
                commands += list(args.run_command)
                for list_path in args.run_command_file:
                    for line in Path(list_path).read_text().splitlines():
                        if line.strip() and not line.lstrip().startswith("#"):
                            commands.append(line.rstrip("\n"))
                if not args.run_command_interactive:
                    # Clean up our own droppings with the shell we already have; the
                    # service channel has no delete operation at all.
                    commands.append(CLEANUP_COMMAND)
                    log("cleanup_queued", files=list(INTERNAL_DROPPINGS))
                extra["driver"] = command_runner_driver(
                    commands, interactive=args.run_command_interactive,
                    json_path=args.run_command_json)
                nonce = None
            elif args.dump_partition:
                if kind != "shell":
                    raise ValueError("--dump-partition needs the shell payload")
                if not args.allow_internal_chunking:
                    raise ValueError(
                        "--dump-partition holds 1 MiB chunks on the camera's own flash while "
                        "each one transfers, and doing that once filled a 12 MiB partition so "
                        "badly that unrelated writes (including the preload cleanup) failed. "
                        "Use --dump-to-card instead: the data goes to the SD card's ext2 "
                        "partition. Pass --allow-internal-chunking to override deliberately.")
                from jiritsu_preload_deploy import partition_dump_driver
                target_dir = Path(args.dump_out) if args.dump_out else out
                target_dir.mkdir(parents=True, exist_ok=True)
                drivers = []
                for device in args.dump_partition:
                    size = PARTITION_BYTES.get(device)
                    if size is None:
                        raise ValueError(f"Unknown partition size for {device}; "
                                         "add it to PARTITION_BYTES")
                    drivers.append(partition_dump_driver(device, size, target_dir,
                                                         args.dump_chunk))
                if len(drivers) == 1:
                    extra["driver"] = drivers[0]
                else:
                    def run_all(transport, requests, log, record, _drivers=drivers):
                        return [d(transport, requests, log, record) for d in _drivers]
                    extra["driver"] = run_all
                nonce = None
            value, record = deploy_and_run(
                transport, payload, log, approval=args.approve_preload,
                payload_kind=kind, expected_sha256=expected, nonce=nonce, **extra)
            deployment = {"kind": kind, "record": record,
                          "payload_bytes": len(payload), "payload_sha256": actual}
            if value:
                # Drivers may return a structured summary instead of raw bytes; the
                # evidence file always holds bytes, so serialise here rather than
                # constraining what a driver may return.
                blob = (value if isinstance(value, (bytes, bytearray))
                        else json.dumps(value, indent=2).encode())
                (out / ("probe-result.bin" if kind == "probe" else "mailbox-output.bin")).write_bytes(blob)
                deployment["value_bytes"] = len(blob)
                deployment["value_sha256"] = hashlib.sha256(blob).hexdigest()
            (out / "deployment.json").write_text(json.dumps(deployment, indent=2) + "\n")

        if args.clear_preload:
            from jiritsu_preload_clear import clear_preload
            clear_record = clear_preload(transport, log)
            (out / "preload-clear.json").write_text(json.dumps(clear_record, indent=2) + "\n")

        if args.traversal_write_check:
            from jiritsu_traversal_write import run_traversal_write_check, WRITE_SIZE as TW_SIZE
            tw_payload = os.urandom(TW_SIZE)
            traversal_record = run_traversal_write_check(transport, tw_payload, log)
            (out / "traversal-write-payload.bin").write_bytes(tw_payload)
            (out / "traversal-write.json").write_text(json.dumps(traversal_record, indent=2) + "\n")
        success = True
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        log("error", message=failure)
    finally:
        signal.alarm(0)
        if started and current is not None:
            try:
                current.ctrl_transfer(0x43, 1, 0xc800, 0x2855, b"", timeout=5000)
                log("stop_senser_requested")
            except Exception as exc:
                log("stop_request_exception", message=f"{type(exc).__name__}: {exc}")
        if current is not None:
            try:
                usb.util.release_interface(current, 0)
                if detached:
                    current.attach_kernel_driver(0)
            except usb.core.USBError:
                pass  # a mode switch may have already removed this device
            finally:
                usb.util.dispose_resources(current)
        if started:
            try:
                restored = wait_for(0x0e8a)
                log("ordinary_identity_restored", pid="0e8a", bus=restored.bus, address=restored.address)
                restored_ok = True
                usb.util.dispose_resources(restored)
            except Exception as exc:
                # The camera normally ends a session outside the mass-storage identity
                # (054c:096f) and needs a physical reconnect. That is operator-visible
                # state, not a failed task, so it does not change `passed`.
                log("restore_not_verified", message=str(exc))
                print("  ! 相机未回到 054c:0e8a（通常需手动重连一次 USB）；本任务结果不受影响",
                      flush=True)
        summary = {"passed": success, "authentication_requested": args.authenticate,
                   "failure": failure, "root_execution_tested": False,
                   "camera_file_write_tested": False,
                   "version_read_requested": args.read_version,
                   "version_read_passed": version_read,
                   "system_preflight_requested": args.system_dump_preflight,
                   "system_preflight_complete": system_preflight_complete,
                   "sd_probe_requested": args.inspect_sd,
                   "sd_probe_complete": sd_probe_complete,
                   "write_check_requested": args.write_check,
                   "write_check_matches": None if write_check_record is None else write_check_record["matches"],
                   "write_check_path": None if write_check_record is None else write_check_record["path"],
                   "files_requested": [record["path"] for record in file_records],
                   "files_read": len(file_records),
                   "files_refused": [item["path"] for item in read_failures],
                   "crypto_self_check_requested": args.verify_crypto,
                   "crypto_checks": [record["path"] for record in crypto_records],
                   "deadline_seconds": args.deadline,
                   "io_timeout_ms": args.io_timeout,
                   "sd_write_check_requested": args.sd_write_check,
                   "sd_write_check_matches": None if sd_write_record is None else sd_write_record["matches"],
                   "sd_write_path": None if sd_write_record is None else sd_write_record["path"],
                   "write_file_requested": [spec.split("=", 1)[0] for spec in args.write_file],
                   "write_file_verified": [record.get("verified") for record in write_file_records],
                   "traversal_write_requested": args.traversal_write_check,
                   "traversal_write_matches": None if traversal_record is None else traversal_record["matches"],
                   "traversal_write_target": None if traversal_record is None else traversal_record["resolved_path"],
                   "deployment_kind": None if deployment is None else deployment["kind"],
                   "deployment_payload_ran": None if deployment is None else deployment["record"]["payload_ran"],
                   "deployment_disarmed": None if deployment is None else deployment["record"]["disarmed"],
                   "preload_clear_requested": args.clear_preload,
                   "preload_clear_neutralised": None if clear_record is None else clear_record["neutralised"],
                   "ordinary_identity_restored": restored_ok}
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(f"Evidence: {out}", flush=True)
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
