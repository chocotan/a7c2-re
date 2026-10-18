"""Run one shell command on an ILCE-7CM2 (A7C II) 2.01 through the Jiritsu
service's own libJit loader entry, with no loader configuration written.

Why this exists: this project's previous entry (arm /etc/ld.so.preload, then
send a DEBUG command to make some process exec and pull the payload in) leaves a
loader decision on disk. If the 12 MiB partition that holds /etc fills up, the
disarm write fails and the camera is left in a state we cannot confirm. The
Jiritsu service does not need any of that: after a successful file write to the
exact path /tmp/jiritsu/libJit.so it dlopens that object itself, so the object we
write runs in the service process with no configuration left behind. See
README.md for the evidence and the risk table.

Flow:
  1. select+read /tmp/jiritsu/libJit.so   -> must be absent, or we stop
  2. write placeholders to the start/status records (truncates stale data)
  3. write the shell script to /tmp/jiritsu/a7c2-exec.request
  4. write the payload object to /tmp/jiritsu/libJit.so  (this is the trigger)
  5. poll the start record, then the status record, then fetch the output
  6. delete this run's temporary files

Usage (needs root for the USB device node; the camera in Mass Storage mode):
    sudo ./.venv/bin/python libjit_exec.py run --command 'id; cat /proc/mounts'
    sudo python3 libjit_exec.py run --command 'mount /dev/mmca2 /tmp/sd' --keep
    python3 libjit_exec.py frames --command 'uname -a'        # offline, no USB
    sudo python3 libjit_exec.py cleanup                       # after an abort
"""
import argparse
import hashlib
import json
import os
import secrets
import signal
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PAYLOAD = BASE / "payload" / "libjit_exec.so"
DEFAULT_SCRIPTS = BASE.parents[1] / "2026-10-16-system-dump" / "scripts"

import libjit_requests as jr


# Every event goes to stderr and, when --keep is active, into this list so the
# kept evidence holds the wire trace too and not just the summary lines.
SINK = None


def log(event, **fields):
    record = {"event": event, "time": time.time(), **fields}
    print(json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)
    if SINK is not None:
        SINK.append(record)


def resolve_paths(scripts=None, pmca=None, require_pmca=True):
    """Locate the pinned transport module and the vendored PMCA checkout.

    This directory does not copy the Senser framing: it reuses the reviewed
    implementation from the system-dump deliverable in the same repository, so
    there is exactly one place where that byte layout is maintained.
    """
    scripts_dir = Path(scripts or os.environ.get("A7C2_SCRIPTS") or DEFAULT_SCRIPTS).resolve()
    module = scripts_dir / "jiritsu_version_read.py"
    if not module.is_file():
        raise SystemExit(f"transport module not found: {module}\n"
                         "Pass --scripts <dir> or set A7C2_SCRIPTS.")
    pmca_dir = Path(pmca or os.environ.get("A7C2_PMCA") or scripts_dir / "vendor" / "pmca-adamwuqwq")
    if require_pmca and not (pmca_dir / "pmca" / "usb" / "sony.py").is_file():
        raise SystemExit(f"vendored Sony-PMCA-RE not found: {pmca_dir}\n"
                         "Clone it there, or pass --pmca <dir> / set A7C2_PMCA:\n"
                         f"  git clone https://github.com/adamwuqwq/Sony-PMCA-RE "
                         f"{pmca_dir}")
    for path in (scripts_dir, pmca_dir):
        if path.is_dir() and str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return scripts_dir, pmca_dir


def camera_mounts():
    """Mounted block devices belonging to this camera, matched via sysfs ancestry.

    Only this camera's volumes are ever unmounted; a plain /dev/sdX guess could
    take someone's disk away.
    """
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


def unmount_camera_volumes(volumes):
    """Release this camera's volumes through the desktop's mount machinery.

    udisksctl is what the desktop's automount registers with, so it releases the
    volume cleanly; plain umount is the fallback. Only volumes already matched to
    this camera by `camera_mounts()` are touched.
    """
    import subprocess
    for volume in volumes:
        for mountpoint in volume["mounts"]:
            log("camera_volume_unmounting", block=volume["block"], mount=mountpoint)
            if subprocess.run(["udisksctl", "unmount", "-b", f"/dev/{volume['block']}"],
                              capture_output=True, text=True).returncode != 0:
                subprocess.run(["umount", mountpoint], capture_output=True, check=False)
    return camera_mounts()


class ServiceSession:
    """Two-stage authentication into the 054c:0336 service identity."""

    def __init__(self, timeout=25, unmount=False):
        self.timeout = timeout
        self.unmount = unmount
        self.device = None
        self.auth = None
        self.transport = None

    def __enter__(self):
        import usb.core
        import usb.util
        from pmca.usb.sony import SonySenserAuthDevice, SonySenserDevice, SonySenserCamera

        timeout = self.timeout
        from pmca.usb.driver.generic import GenericUsbDriver, GenericUsbException
        from pmca.usb.driver.generic.libusb import UsbBackend

        class DiagnosingBackend(UsbBackend):
            """Keeps the libusb error text, which the vendor backend discards."""

            def __init__(self, device):
                super().__init__(device)
                self.dev.default_timeout = max(1000, int(timeout * 1000))

            def read(self, ep, length, timeout=None):
                try:
                    return self.dev.read(ep, length, timeout).tobytes()
                except usb.core.USBError as exc:
                    raise GenericUsbException(
                        f"bulk read of {length} bytes failed: {type(exc).__name__}: {exc} "
                        f"(errno={getattr(exc, 'errno', None)})") from exc

        def find(pid=None):
            devices = [d for d in usb.core.find(find_all=True, idVendor=0x054C)
                       if d.idProduct in (0x0E8A, 0x0336)]
            if pid is not None:
                devices = [d for d in devices if d.idProduct == pid]
            if len(devices) > 1:
                raise RuntimeError("more than one matching camera; refusing an ambiguous choice")
            return devices[0] if devices else None

        def wait_for(pid, seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                found = find(pid)
                if found is not None:
                    return found
                time.sleep(0.25)
            raise RuntimeError(f"USB identity 054c:{pid:04x} did not appear")

        def open_driver(device):
            backend = DiagnosingBackend(device)
            backend.reset()  # detaches the kernel driver, not a physical USB reset
            usb.util.claim_interface(device, 0)
            return GenericUsbDriver(backend)

        volumes = camera_mounts()
        if volumes and self.unmount:
            volumes = unmount_camera_volumes(volumes)
        if volumes:
            summary = ", ".join(f"{v['block']}->{m}" for v in volumes for m in v["mounts"])
            raise RuntimeError(f"camera volumes are mounted ({summary}); unmount them first "
                               "(a service session needs the volume released)")
        device = find()
        if device is None:
            raise RuntimeError("no matching camera connected")
        if device.idProduct == 0x0E8A:
            driver = open_driver(device)
            auth = SonySenserAuthDevice(driver)
            auth.start()
            auth.authenticate()
            log("mass_storage_authentication_passed")
            usb.util.dispose_resources(device)
            device = wait_for(0x0336, timeout)
            log("service_identity_seen")
        driver = open_driver(device)
        auth = SonySenserAuthDevice(driver)
        auth.start()
        auth.authenticate()
        log("service_authentication_passed")
        self.device, self.auth = device, auth
        self.transport = SonySenserDevice(driver)
        product = SonySenserCamera(self.transport).readHasp()
        if len(product) != 88:
            raise RuntimeError(f"ProductInfo reply is {len(product)} bytes, expected 88")
        return self

    def __exit__(self, *_):
        import usb.util
        if self.auth is not None:
            try:
                self.auth.stop()
            except Exception as exc:  # a failed stop must not hide the real error
                log("auth_stop_failed", error=str(exc))
        if self.device is not None:
            usb.util.dispose_resources(self.device)


class Runner:
    def __init__(self, session, keep_dir=None):
        self.session = session
        self.keep_dir = keep_dir

    def _record(self, event, **fields):
        log(event, **fields)

    def call(self, request, limit):
        import jiritsu_version_read as jv
        from jiritsu_version_read import JiritsuResultError
        jv.authorize(self.session.transport, (request,),
                     max_bytes=max(len(request), 4096), max_requests=4)
        try:
            return jv.exchange(self.session.transport, request, limit, log)
        except JiritsuResultError:
            raise

    def read_path(self, path, limit):
        """Read a work-area path; returns None when the firmware reports 'absent'."""
        from jiritsu_version_read import JiritsuResultError
        self.call(jr.select_frame(path), 4)
        try:
            return self.call(jr.READ_FRAME, 4 + limit)
        except JiritsuResultError as exc:
            if exc.result == jr.ABSENT:
                return None
            raise

    def write_path(self, path, data):
        self.call(jr.select_frame(path), 4)
        self.call(jr.write_frame(data, max(len(data), 4096)), 4)

    def save(self, name, data):
        if self.keep_dir is not None and data is not None:
            self.keep_dir.mkdir(parents=True, exist_ok=True)
            (self.keep_dir / name).write_bytes(data)


def poll(runner, path, parser, seconds, interval=0.5):
    """Poll one record file. Returns (value, raw); value is None on timeout."""
    deadline = time.monotonic() + seconds
    raw = None
    while True:
        raw = runner.read_path(path, 4096)
        if raw is not None:
            value = parser(raw)
            if value is not None:
                return value, raw
        if time.monotonic() >= deadline:
            return None, raw
        time.sleep(interval)


def run(args):
    payload = Path(args.payload).resolve()
    if not payload.is_file():
        raise SystemExit(f"payload object not found: {payload} (run payload/build.sh first)")
    blob = payload.read_bytes()
    if not blob.startswith(b"\x7fELF"):
        raise SystemExit(f"{payload} is not an ELF object")
    if len(blob) > jr.MAX_PAYLOAD:
        raise SystemExit(f"payload is {len(blob)} bytes, limit is {jr.MAX_PAYLOAD}")

    nonce = secrets.token_hex(4)
    script = jr.build_script(args.command, nonce)
    log("prepared", payload=str(payload), payload_sha256=hashlib.sha256(blob).hexdigest(),
        payload_bytes=len(blob), script_bytes=len(script), nonce=nonce)

    if args.dry_run:
        for name, path, data in (("request", jr.REQUEST_PATH, script),
                                 ("payload", jr.LIBJIT_PATH, blob)):
            print(json.dumps({"path": path, "bytes": len(data),
                              "select_hex": jr.select_frame(path).hex(),
                              "write_head_hex": jr.write_frame(data, jr.MAX_UPLOAD)[:16].hex()}))
        print(json.dumps({"script": script.decode("ascii")}))
        return 0

    keep_dir = Path(args.keep).resolve() if args.keep else None
    if keep_dir is not None:
        global SINK
        SINK = []
    result = {"nonce": nonce, "payload_sha256": hashlib.sha256(blob).hexdigest(),
              "command": args.command, "armed": False, "module_ran": False,
              "completed": None, "exit_status": None}

    with ServiceSession(timeout=args.timeout, unmount=args.unmount_camera_volumes) as session:
        runner = Runner(session, keep_dir)

        existing = runner.read_path(jr.LIBJIT_PATH, 4096)
        if existing is not None:
            # Never overwrite an object of unknown origin: it may be the camera's
            # own, and the write would make the service load whatever it is.
            raise SystemExit(f"{jr.LIBJIT_PATH} already exists ({len(existing)} bytes); "
                             "inspect and remove it before arming")
        runner._record("libjit_path_absent")
        # adj_cmd_set_file2 returns 0x11 and writes nothing while this marker
        # exists, so check it first instead of learning it from a failed write.
        if runner.read_path(jr.JBUSY_PATH, 64) is not None:
            raise SystemExit(f"{jr.JBUSY_PATH} exists; the firmware refuses the libJit write "
                             "while it is present (another loader may be active). "
                             "Do not remove it by hand without knowing why it is there")
        runner._record("jbusy_absent")
        runner._record("jmode_state", present=runner.read_path(jr.JMODE_PATH, 64) is not None)

        # Truncate the records this run will read back, so a stale line from an
        # earlier run cannot be mistaken for this one.
        runner.write_path(jr.START_PATH, jr.START_PENDING)
        runner.write_path(jr.STATUS_PATH, jr.STATUS_PENDING)
        runner.write_path(jr.REQUEST_PATH, script)
        if runner.read_path(jr.REQUEST_PATH, 4096) != script:
            raise SystemExit("request script readback mismatch; nothing armed")

        runner._record("about_to_arm", payload_bytes=len(blob))
        result["armed"] = True
        # The trigger: the handler writes this file, fsyncs it, then dlopens it
        # and immediately dlcloses it. Its reply only arrives after _init returns.
        try:
            runner.write_path(jr.LIBJIT_PATH, blob)
        except Exception as exc:  # result code, not transport failure
            from jiritsu_version_read import JiritsuResultError
            if isinstance(exc, JiritsuResultError) and exc.result == jr.DLOPEN_FAILED:
                runner._record("dlopen_failed", file_written=True, result=hex(exc.result))
                result["dlopen_failed"] = True
            else:
                raise
        runner._record("arm_write_returned", dlopen_failed=bool(result.get("dlopen_failed")))

        start, raw_start = poll(runner, jr.START_PATH, jr.parse_start, args.start_timeout)
        runner.save("start.bin", raw_start)
        if start is None:
            runner._record("module_did_not_run",
                           dlopen_failed=bool(result.get("dlopen_failed")),
                           note="no start record; see the dlopen result above")
            if runner.read_path(jr.LIBJIT_PATH, 4096) is None:
                runner._record("libjit_path_gone_without_record")
        else:
            result["module_ran"] = True
            runner._record("module_ran", **start)
            if start["euid"] != 0:
                runner._record("not_root", euid=start["euid"])
            if start["status"] != 0:
                runner._record("runner_failed", status=start["status"])

            status, raw_status = poll(runner, jr.STATUS_PATH,
                                      lambda raw: jr.parse_status(raw, nonce), args.timeout)
            runner.save("status.bin", raw_status)
            result["completed"] = status is not None
            result["exit_status"] = status
            if status is None:
                runner._record("command_did_not_complete", seconds=args.timeout)

        output = runner.read_path(jr.OUTPUT_PATH, args.max_output)
        runner.save("output.bin", output)
        if output is not None:
            text = output.decode("utf-8", "replace")
            result["output_bytes"] = len(output)
            result["output_truncated"] = len(output) >= args.max_output
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            sys.stdout.flush()

        # The object unlinks itself as its first action; absence here is the
        # expected state whether or not the rest of it worked.
        result["libjit_remaining"] = runner.read_path(jr.LIBJIT_PATH, 4096) is not None
        result["jmode_after"] = runner.read_path(jr.JMODE_PATH, 64) is not None
        if not args.keep:
            # Cleanup uses overwrite-truncation, not a delete command: case 0x12
            # (cmd_file_delete2) exists in the dispatcher, but its payload layout
            # is not in this project's decompiled evidence, so this tool does not
            # send frames it cannot justify. Four one-byte files remain, reported.
            leftovers = []
            for path in (jr.REQUEST_PATH, jr.START_PATH, jr.STATUS_PATH, jr.OUTPUT_PATH):
                if runner.read_path(path, 4096) is None:
                    continue
                runner.write_path(path, b"\n")
                remaining = runner.read_path(path, 4096)
                leftovers.append({"path": path,
                                  "bytes": None if remaining is None else len(remaining)})
            result["left_behind"] = leftovers
            runner._record("cleanup_truncated", left_behind=leftovers)

    if keep_dir is not None:
        keep_dir.mkdir(parents=True, exist_ok=True)
        (keep_dir / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        (keep_dir / "events.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in (SINK or [])))

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["module_ran"]:
        return 2
    if result["completed"] is False:
        return 3
    return 0 if not result["exit_status"] else 4


def cleanup(args):
    """Truncate this tool's work-area files after an aborted run."""
    with ServiceSession(timeout=args.timeout, unmount=args.unmount_camera_volumes) as session:
        runner = Runner(session)
        for path in (jr.LIBJIT_PATH, jr.REQUEST_PATH, jr.START_PATH,
                     jr.STATUS_PATH, jr.OUTPUT_PATH):
            if runner.read_path(path, 4096) is None:
                log("absent", path=path)
                continue
            runner.write_path(path, b"\n")
            remaining = runner.read_path(path, 4096)
            log("truncated", path=path, bytes=None if remaining is None else len(remaining))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scripts", help="directory holding jiritsu_version_read.py")
    parser.add_argument("--pmca", help="directory holding the vendored pmca package")
    sub = parser.add_subparsers(dest="command_name", required=True)

    run_parser = sub.add_parser("run", help="run one command through the libJit entry")
    run_parser.add_argument("--command", required=True, help="ASCII shell script body")
    run_parser.add_argument("--payload", default=str(PAYLOAD),
                            help="ELF shared object to load (default: payload/libjit_exec.so)")
    run_parser.add_argument("--timeout", type=float, default=60.0,
                            help="seconds to wait for the command to finish (default 60)")
    run_parser.add_argument("--start-timeout", type=float, default=15.0,
                            help="seconds to wait for the module's own record (default 15)")
    run_parser.add_argument("--max-output", type=int, default=262144,
                            help="output readback limit in bytes (default 256 KiB); "
                                 "write larger data to the SD card instead")
    run_parser.add_argument("--keep", help="keep evidence and the camera-side files here")
    run_parser.add_argument("--unmount-camera-volumes", action="store_true",
                            help="unmount this camera's auto-mounted volumes before connecting")
    run_parser.add_argument("--dry-run", action="store_true",
                            help="print the frames without touching a camera")
    run_parser.set_defaults(func=run)

    clean_parser = sub.add_parser("cleanup", help="delete this tool's work-area files")
    clean_parser.add_argument("--timeout", type=float, default=25.0)
    clean_parser.add_argument("--unmount-camera-volumes", action="store_true")
    clean_parser.set_defaults(func=cleanup)

    args = parser.parse_args()
    resolve_paths(args.scripts, args.pmca,
                  require_pmca=not getattr(args, "dry_run", False))

    def deadline(*_):
        raise TimeoutError("overall deadline exceeded")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(600)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
