#!/usr/bin/env python3
"""Read, write and delete camera files through the vendor's own service file API.

Why this exists next to the MSC-layer writer: the vendor's `FileControl` (pFunc 0xFF01)
takes **absolute paths**, includes a **delete** operation, and its own callers use it on
`/setting/...`, so it reaches partitions our traversal writer has never exercised.

That matters for recovery. A failed cleanup left `/etc/ld.so.preload` at 0 bytes, and
the MSC-layer write cannot extend an existing empty file (`0x10000009`), so the preload
could no longer be armed. Deleting the file and creating it again — creation is proven to
work — restores that route without a factory reset.

**Outcome on this camera (2026-10-16): the API is not usable.** Every file operation
returned `Senser file control error 128` — for rootfs, `/setting` and `/etc` paths alike,
and after a successful `readHasp()` (the vendor's own backend reads the HASP before using
these APIs, so that step was added and ruled out as the gate). Our own MSC-layer
read/write (pFunc 0x0f/0x10/0x11) remains the working path on this body. The tool is kept
because the negative result is worth reproducing, and because it documents the layer.

Reads are verified by content, never by a return code: `--read` saves what came back and
prints its size and digest, and callers compare.

    sudo ./.venv/bin/python senser_file.py --read /setting/sen/smode --save smode.bin
    sudo ./.venv/bin/python senser_file.py --delete /etc/a7c2-write-check.bin
    sudo ./.venv/bin/python senser_file.py --write /setting/sen/smode --from smode.bin

Only the paths given on the command line are touched; nothing here iterates or guesses.
"""
import argparse
import hashlib
import io
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "vendor" / "pmca-adamwuqwq"))

VID = 0x054C
MSC_PID = 0x0E8A
SERVICE_PIDS = (0x02A9, 0x0336)


def find_devices(product=None):
    import usb.core
    return list(usb.core.find(find_all=True, idVendor=VID,
                              **({"idProduct": product} if product else {})))


def wait_for_service(port, timeout=12.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for dev in find_devices():
            if (dev.bus, tuple(dev.port_numbers)) == port and dev.idProduct in SERVICE_PIDS:
                return dev
        time.sleep(0.25)
    return None


def open_service(device, log):
    """Authenticate into the service identity.

    Two entries are supported: from the mass-storage identity (two stages, the camera
    re-enumerates), or directly on a camera that is *already* sitting on a service
    interface — which is where most sessions leave it, so using it saves a power cycle.
    """
    import usb.util
    from pmca.usb.sony import SonySenserAuthDevice
    from pmca.usb.driver.generic import GenericUsbDriver
    from pmca.usb.driver.generic.libusb import UsbBackend

    if device.idProduct in SERVICE_PIDS:
        driver = GenericUsbDriver(UsbBackend(device))
        auth = SonySenserAuthDevice(driver)
        auth.start()
        auth.authenticate()
        log(f"直接使用已在服务模式的接口 {device.idProduct:#06x}（无需断电重启）")
        return device, driver

    port = (device.bus, tuple(device.port_numbers))
    auth = SonySenserAuthDevice(GenericUsbDriver(UsbBackend(device)))
    auth.start()
    auth.authenticate()
    log("MSC 身份认证完成")
    usb.util.dispose_resources(device)

    service = wait_for_service(port)
    if service is None:
        raise SystemExit("相机 12 秒内没有出现服务接口（0x02a9/0x0336）")
    driver = GenericUsbDriver(UsbBackend(service))
    auth = SonySenserAuthDevice(driver)
    auth.start()
    auth.authenticate()
    log(f"服务接口 {service.idProduct:#06x} 认证完成")
    return service, driver


def camera_api(driver):
    from pmca.usb.sony import SonySenserCamera, SonySenserDevice
    return SonySenserCamera(SonySenserDevice(driver))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--delete", action="append", metavar="CAMERA_PATH", default=[],
                        help="Delete this absolute camera path (runs before --write)")
    parser.add_argument("--write", action="append", metavar="CAMERA_PATH=LOCAL", default=[],
                        help="Write bytes from a local file to a camera path")
    parser.add_argument("--read", action="append", metavar="CAMERA_PATH", default=[],
                        help="Read this absolute camera path (runs last, to verify)")
    parser.add_argument("--save-dir", metavar="DIR", help="Where --read saves what it got")
    parser.add_argument("--yes", action="store_true", help="Do not prompt")
    args = parser.parse_args(argv)
    if not (args.delete or args.write or args.read):
        parser.error("give at least one of --delete, --write, --read")
    if args.read and not args.save_dir:
        parser.error("--read needs --save-dir so the bytes are kept as evidence")
    for path in args.delete + args.read:
        if not path.startswith("/"):
            parser.error(f"camera paths must be absolute, got {path!r}")
    for spec in args.write:
        if "=" not in spec:
            parser.error(f"--write wants CAMERA_PATH=LOCAL, got {spec!r}")
        path, local = spec.split("=", 1)
        if not path.startswith("/"):
            parser.error(f"camera paths must be absolute, got {path!r}")
        if not Path(local).is_file():
            parser.error(f"no such local file: {local}")
    return args


def main(argv=None):
    args = parse_args(argv)

    def log(text):
        print(f"  {text}", file=sys.stderr, flush=True)

    devices = find_devices(MSC_PID) or [d for d in find_devices() if d.idProduct in SERVICE_PIDS]
    if len(devices) != 1:
        print("未检测到可用相机：需要 054c:0e8a（海量存储）或 054c:0336/02a9（服务模式）；"
              "若停在 096f：Linux 重新插拔 USB，Windows 需断电重启", file=sys.stderr)
        return 3
    if not args.yes:
        reply = input(f"  将对相机执行 {[n for n in ('read','write','delete') if getattr(args, n)][0]} "
                      f"{getattr(args, 'read') or getattr(args, 'write') or args.delete}；继续？[回车/q] ")
        if reply.strip().lower() == "q":
            raise SystemExit("operator aborted")

    service, driver = open_service(devices[0], log)
    camera = camera_api(driver)

    # The vendor's own backend reads the HASP before touching files or memory
    # (`SenserPlatformBackend.start()`), and every file operation returned 128 while
    # this step was missing — so it is treated as the authorisation gate, not as an
    # optional probe. Its result is logged rather than assumed.
    try:
        camera.readHasp()
        log("HASP 读取成功（厂商流程中的授权步骤）")
    except Exception as error:                          # noqa: BLE001
        log(f"HASP 读取失败：{error}（后续操作很可能仍被拒）")

    failures = []

    for path in args.delete:
        try:
            camera.deleteFile(path)
            log(f"已删除 {path}")
        except Exception as error:                      # noqa: BLE001 - the code is the result
            log(f"删除 {path} 失败：{error}")
            failures.append(("delete", path, str(error)))

    for spec in args.write:
        path, local = spec.split("=", 1)
        data = Path(local).read_bytes()
        try:
            camera.writeFile(path, data)
            log(f"已写入 {path}（{len(data)} 字节，命令已返回，未验证）")
        except Exception as error:                      # noqa: BLE001
            log(f"写入 {path} 失败：{error}")
            failures.append(("write", path, str(error)))

    for index, path in enumerate(args.read):
        buffer = io.BytesIO()
        try:
            camera.readFile(path, buffer)
        except Exception as error:                      # noqa: BLE001
            log(f"读取 {path} 失败：{error}")
            failures.append(("read", path, str(error)))
            continue
        data = buffer.getvalue()
        target = Path(args.save_dir) / f"file-{index}-{path.strip('/').replace('/', '_')}"
        target.write_bytes(data)
        uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if uid and gid and os.geteuid() == 0:
            os.chown(target, int(uid), int(gid))
        log(f"{path}: {len(data)} 字节 sha256={hashlib.sha256(data).hexdigest()[:16]}… → {target}")

    if failures:
        log(f"共 {len(failures)} 项失败：{failures}")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
