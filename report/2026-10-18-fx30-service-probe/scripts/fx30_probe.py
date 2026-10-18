"""只读探测另一台 Sony 机型是否复用 A7C2 的 Senser/Jiritsu 服务链。

用途：ILCE-7CM2 的那条链（大量存储 `054c:0e8a` -> Senser 认证 -> `054c:0336`
服务身份 -> Jiritsu 文件命令）是本项目唯一验证过的路线。换个机型能不能走，
不能靠推断。这个脚本只做两件事：

  1. 对该机型的大量存储身份做一次 Senser 认证，观察它变成什么 USB 身份；
  2. 如果变成已知的 Senser 身份（`0x0336` 或 `0x02a9`），再做第二次认证，
     读 ProductInfo 与 `/version.txt`。

**不写任何文件、不执行任何代码。** 全程只读一个固定路径的版本文件。

用法：
    sudo ./.venv/bin/python fx30_probe.py --vendor-pid 0x0e0e --unmount-volumes
    sudo ./.venv/bin/python fx30_probe.py --vendor-pid 0x0e8a --read-only-registry

退出码：0 = 走到服务模式并读完；2 = 认证后没有出现已知 Senser 身份；3 = 认证失败。
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
# The Senser framing and the vendored PMCA checkout live in the system-dump
# deliverable; this directory reuses them rather than keeping a second copy of
# the byte layout or a second clone.
SCRIPTS = Path(os.environ.get("A7C2_SCRIPTS")
               or BASE.parents[1] / "2026-10-16-system-dump" / "scripts").resolve()
PMCA = Path(os.environ.get("A7C2_PMCA")
            or SCRIPTS / "vendor" / "pmca-adamwuqwq").resolve()
# Protocol-wide Senser identities. PMCA works with these for many bodies; the
# mass-storage identity in --vendor-pid is the part that differs per model.
SENSER_IDS = (0x0336, 0x02A9)


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), file=sys.stderr, flush=True)


def camera_mounts():
    """只认这台相机自己的卷：靠 sysfs 祖先链上的 idVendor/idProduct 判定。"""
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
                if vendor.read_text().strip().lower() == "054c":
                    result.append({"block": block.name, "mounts": mounted[devnum],
                                   "pid": product.read_text().strip().lower()})
                break
    return result


def unmount(volumes):
    import subprocess
    for volume in volumes:
        for mountpoint in volume["mounts"]:
            log("unmounting", block=volume["block"], mount=mountpoint)
            if subprocess.run(["udisksctl", "unmount", "-b", f"/dev/{volume['block']}"],
                              capture_output=True, text=True).returncode != 0:
                subprocess.run(["umount", mountpoint], capture_output=True, check=False)


def list_sony():
    import usb.core
    return [(d.idProduct, d.bus, d.address, d)
            for d in usb.core.find(find_all=True, idVendor=0x054C)]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vendor-pid", default="0x0e0e", help="mass-storage PID to authenticate (hex)")
    parser.add_argument("--unmount-volumes", action="store_true")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--io-timeout", type=float, default=15.0)
    parser.add_argument("--save", help="directory for the readback evidence")
    args = parser.parse_args()
    vendor_pid = int(args.vendor_pid, 16)

    for path in (PMCA, SCRIPTS):
        if not path.is_dir():
            raise SystemExit(f"missing dependency directory: {path}")
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import usb.core
    import usb.util
    from pmca.usb.sony import SonySenserAuthDevice, SonySenserCamera, SonySenserDevice
    from pmca.usb.driver.generic import GenericUsbDriver, GenericUsbException
    from pmca.usb.driver.generic.libusb import UsbBackend

    save = Path(args.save).resolve() if args.save else None
    if save:
        save.mkdir(parents=True, exist_ok=True)

    volumes = camera_mounts()
    if volumes and args.unmount_volumes:
        unmount(volumes)
        volumes = camera_mounts()
    if volumes:
        raise SystemExit(f"camera volumes still mounted: {volumes}")

    class Backend(UsbBackend):
        def __init__(self, device):
            super().__init__(device)
            self.dev.default_timeout = max(1000, int(args.io_timeout * 1000))

        def read(self, ep, length, timeout=None):
            try:
                return self.dev.read(ep, length, timeout).tobytes()
            except usb.core.USBError as exc:
                raise GenericUsbException(f"bulk read of {length} bytes failed: {exc}") from exc

    def open_driver(device):
        backend = Backend(device)
        backend.reset()  # detach the kernel driver, not a physical USB reset
        usb.util.claim_interface(device, 0)
        return GenericUsbDriver(backend)

    found = [entry for entry in list_sony() if entry[0] == vendor_pid]
    log("scan", sony_devices=[f"{pid:04x}" for pid, *_ in list_sony()])
    if len(found) != 1:
        raise SystemExit(f"expected exactly one 054c:{vendor_pid:04x} device, found {len(found)}")
    pid, bus, address, device = found[0]
    log("target", pid=f"{pid:04x}", bus=bus, address=address)

    # 第一步：对大量存储身份做 Senser 认证。PMCA 对多机型用同一套握手，
    # 所以这一步本身不是机型相关的；结果只有"接受/拒绝"两种。
    driver = open_driver(device)
    auth = SonySenserAuthDevice(driver)
    try:
        auth.start()
        log("mass_storage_auth_start_accepted")
        auth.authenticate()
        log("mass_storage_auth_passed")
    except Exception as exc:
        log("mass_storage_auth_failed", error=f"{type(exc).__name__}: {exc}")
        try:
            auth.stop()
        finally:
            usb.util.dispose_resources(device)
        return 3
    finally:
        try:
            usb.util.dispose_resources(device)
        except Exception:
            pass

    # 第二步：看它重新枚举成什么身份。
    deadline = time.monotonic() + args.timeout
    service = None
    seen = set()
    while time.monotonic() < deadline:
        for candidate_pid, candidate_bus, candidate_address, candidate in list_sony():
            seen.add(f"{candidate_pid:04x}")
            if candidate_pid in SENSER_IDS:
                service = (candidate_pid, candidate_bus, candidate_address, candidate)
                break
        if service:
            break
        time.sleep(0.5)
    log("post_auth_scan", seen=sorted(seen))
    if service is None:
        log("no_senser_identity", expected=[f"{p:04x}" for p in SENSER_IDS], seen=sorted(seen))
        return 2

    service_pid, service_bus, service_address, service_device = service
    log("service_identity", pid=f"{service_pid:04x}", bus=service_bus, address=service_address)
    time.sleep(1.0)

    # 第三步：第二次认证 + 只读 ProductInfo 与版本文件。
    driver = open_driver(service_device)
    auth = SonySenserAuthDevice(driver)
    record = {"vendor_pid": f"{vendor_pid:04x}", "service_pid": f"{service_pid:04x}"}
    try:
        auth.start()
        auth.authenticate()
        log("service_auth_passed")
        transport = SonySenserDevice(driver)
        info = SonySenserCamera(transport).readHasp()
        record["product_info_bytes"] = len(info)
        record["product_info_sha256"] = hashlib.sha256(info).hexdigest()
        log("product_info", bytes=len(info), sha256=record["product_info_sha256"])
        if save:
            (save / "product-info.bin").write_bytes(info)

        from jiritsu_version_read import read_version
        content = read_version(transport, log)
        record["version_bytes"] = len(content)
        record["version_text"] = content.decode("utf-8", "replace").strip()
        log("version_file", text=record["version_text"])
        if save:
            (save / "version.bin").write_bytes(content)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        log("service_read_failed", error=record["error"])
    finally:
        try:
            auth.stop()
        except Exception as exc:
            log("auth_stop_failed", error=str(exc))
        usb.util.dispose_resources(service_device)

    print(json.dumps(record, ensure_ascii=False, indent=2))
    if save:
        (save / "probe.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    log("note", message="会话结束后相机需要重新连接或断电重启才能回到大量存储身份")
    return 0 if "error" not in record else 3


if __name__ == "__main__":
    sys.exit(main())
