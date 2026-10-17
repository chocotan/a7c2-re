#!/usr/bin/env python3
"""Run a bounded camera-application probe in the isolated Sony-kernel VM."""
import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import signal
from gpe_captures import raster_sources
from headless_osd_capture import capture as capture_headless_osd

def diagnostic_done(console, marker):
    clean = console.replace(b'\r\n', b'\n')
    clean = re.sub(rb'\[\s*\d+\.\d+\] (?:UDIF:|\[K\])[^\n]*\n', b'', clean)
    return b'\n' + marker.encode() + b'\n' in clean

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--load-ipc", action="store_true", help="load original IPC kernel modules")
parser.add_argument("--bootin", action="store_true", help="use the normal boot manager")
parser.add_argument("--bootall", action="store_true", help="request all im.elf startup phases")
parser.add_argument("--seed-backup", action="store_true", help="load update defaults into the guest Backup driver")
parser.add_argument("--load-dmm", action="store_true", help="load original shared-memory manager")
parser.add_argument("--menu-target", action="store_true", help="use the copied qemu registry without absent USB services")
parser.add_argument("--sony-timers", action="store_true", help="use the custom QEMU timer model")
parser.add_argument("--observe", action="store_true", help="log library loads and use ordinary test thread scheduling")
parser.add_argument("--load-power", action="store_true", help="attempt original power-management driver initialization")
parser.add_argument("--initramfs", action="store_true", help="use raw cpio test root and original workspace reservations")
parser.add_argument("--initramfs-file", type=Path, help="select a separately generated cpio experiment")
parser.add_argument("--no-cp-updater", action="store_true", help="report unavailable virtual CP updater transport as an error")
parser.add_argument("--load-kikilog", action="store_true", help="load the original KikiLog driver before its userspace library")
parser.add_argument("--seed-power-on", action="store_true", help="prepare the virtual loader boot-type record using native Backup API")
parser.add_argument("--transport-errors", action="store_true", help="negative control: copied IPC module returns -1021 when the public transport callback is absent")
parser.add_argument("--capid-service", action="store_true", help="emulate capture-ID table storage and its verified CP protocols; image capture remains unavailable")
parser.add_argument("--sa-address-service", action="store_true", help="apply the original av-cam SA address mappings in the virtual transport")
parser.add_argument("--hdmi-format-service", action="store_true", help="emulate the verified HDMI audio format parameter and completion, without physical audio output")
parser.add_argument("--hdmi-photo-service", action="store_true", help="emulate premium-photo parameter and desired output preset 6, without a physical HDMI link")
parser.add_argument("--dmm-service", action="store_true", help="reserve a virtual CP pool for the verified 0x63a DMM unit lifecycle")
parser.add_argument("--software-gpe", action="store_true", help="execute verified uniform fills in real bitmap memory; other raster operations remain unsupported")
parser.add_argument("--headless-osd", action="store_true", default=os.environ.get('MENU_HEADLESS_OSD') == '1',
                    help="consume AP RGBA4444 frames to bounded guest snapshots; no CP compositor/panel (default off)")
parser.add_argument("--native-display", choices=('none', 'gtk', 'sdl'),
                    default=os.environ.get('QEMU_NATIVE_DISPLAY', 'none') if os.environ.get('MENU_NATIVE_WINDOW') == '1' else None,
                    help="enable standard ramfb and guest snapshot bridge (none permits QMP screendump)")
parser.add_argument("--firmware-identity", action="store_true", help="supply verified selected-FDAT identity files on virtual RAM storage; no real base-updater partition")
parser.add_argument("--display-info-service", action="store_true", help="return the verified panel/EVF info protocol for explicitly configured 640x480 virtual displays")
args = parser.parse_args()
if args.native_display is not None and args.native_display not in ('none', 'gtk', 'sdl'):
    parser.error('QEMU_NATIVE_DISPLAY must be none, gtk or sdl')
if args.native_display is not None and not (args.headless_osd and args.initramfs):
    parser.error('--native-display requires --headless-osd --initramfs')
if args.initramfs_file and not args.initramfs:
    parser.error('--initramfs-file requires --initramfs')
if args.capid_service and not (args.observe and args.seed_backup):
    parser.error('--capid-service requires --observe --seed-backup')
if args.sa_address_service and not args.observe:
    parser.error('--sa-address-service requires --observe')
if args.hdmi_format_service and not args.observe:
    parser.error('--hdmi-format-service requires --observe')
if args.hdmi_photo_service and not args.observe:
    parser.error('--hdmi-photo-service requires --observe')
if args.dmm_service and not (args.observe and args.initramfs and args.load_dmm):
    parser.error('--dmm-service requires --observe --initramfs --load-dmm')
if args.software_gpe and not (args.observe and args.initramfs):
    parser.error('--software-gpe requires --observe --initramfs')
if args.headless_osd and not (args.software_gpe and args.load_ipc and os.environ.get('MENU_GPE_RGBA4444') == '1'):
    parser.error('--headless-osd requires --software-gpe --load-ipc and MENU_GPE_RGBA4444=1')
if args.firmware_identity and not args.initramfs:
    parser.error('--firmware-identity requires --initramfs')
if args.display_info_service and not args.observe:
    parser.error('--display-info-service requires --observe')
boot_type = int(os.environ.get('QEMU_BOOT_TYPE', '1'), 0)
if not 1 <= boot_type <= 8:
    parser.error('QEMU_BOOT_TYPE must be between 1 and 8')
base = Path(__file__).resolve().parent
lock_path = base / '.menu-probe.lock'
inherited_lock = os.environ.get('MENU_PROBE_LOCK_FD')
if inherited_lock is None:
    lock = lock_path.open('a')
else:
    if not inherited_lock.isdecimal() or int(inherited_lock) < 3:
        raise RuntimeError('Invalid inherited probe lock descriptor')
    lock = os.fdopen(int(inherited_lock), 'a')
    actual, expected = os.fstat(lock.fileno()), lock_path.stat()
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
        raise RuntimeError('Inherited descriptor does not refer to the probe lock')
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
runs = base / 'menu-runs'
runs.mkdir(exist_ok=True)
out = Path(tempfile.mkdtemp(prefix='run-', dir=runs))
(base / 'latest-menu-run.txt').write_text(str(out) + '\n')
live_fd = os.environ.get('MENU_CONTROL_FD')
env = dict(os.environ, QEMU_TIMEOUT_SECONDS="0" if live_fd else "180", QEMU_TRACE_FLAGS="cpu_reset,guest_errors,unimp",
           QEMU_OUTPUT_DIR=str(out), SONY_DMM_SERVICE='1' if args.dmm_service else '0')
if args.native_display is not None:
    env.update(MENU_NATIVE_WINDOW='1', QEMU_NATIVE_DISPLAY=args.native_display)
if live_fd:
    env['QEMU_UI_LIVE'] = '1'
    def stop_live(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop_live)
# Must be set before QEMU starts: the guest setup lines below run after the VM is already up.
if os.environ.get("MENU_EXTRA_CMDLINE"):
    env["QEMU_EXTRA_CMDLINE"] = os.environ["MENU_EXTRA_CMDLINE"]
if args.sony_timers:
    env['SONY_TIMER_MODEL'] = '1'
    env.setdefault('QEMU_SYSTEM_DIR', str(base.parent / 'sources/qemu-11.1.2/build-sony-ui'))
    env.setdefault('QEMU_SYSTEM_BINARY', 'qemu-system-aarch64')
command = ["bash", str(base / "qemu-kernel-probe.sh"), "4096",
           str(base / "qemu-virt-board/virt-packed.dtb"), "1", "1",
           str(args.initramfs_file or base / ("menu-initramfs.cpio" if args.initramfs else "menu-root.ext2"))]
def sha256_file(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()

manifest = {'probe_args': vars(args).copy(), 'settle_seconds': int(os.environ.get('MENU_SETTLE_SECONDS', '90')),
            'boot_type': boot_type, 'files': {}, 'original_libraries_unchanged': False,
            'trace_mutex': os.environ.get('MENU_TRACE_MUTEX') == '1'}
manifest['probe_args']['initramfs_file'] = str(args.initramfs_file) if args.initramfs_file else None
for path in [Path(command[-1]), base / 'qemu-menu-probe.py', base / 'menu-observe.c', base / 'menu-observe.so',
             base / 'menu-regs.c', base / 'menu-root/simulation/menu-regs', base / 'menu-readmem.c',
             base / 'capid-service.c', base / 'capid-service.h',
             base / 'sa-address-service.c', base / 'menu-root/system/av-cam.bin',
             base / 'hdmi-format-service.c', base / 'hdmi-format-service.h',
             base / 'dmm-service.c', base / 'dmm-service.h', base / 'menu-root/cp/DmmCfg.bin',
             base / 'gpe-raster.c', base / 'headless-osd.c', base / 'headless-osd.h',
             base / 'headless-osd-export.c', base / 'headless_osd_capture.py',
             base / 'ramfb-viewer.c', base / 'menu-root/simulation/ramfb-viewer',
             base / 'mutex-trace.c', base / 'menu-root/simulation/mutex-trace.so',
             base / 'guest-overlay/simulation/input-state.sh',
             base / 'menu-root/simulation/headless-osd-export',
             base / 'firmware-identity.json', base / 'menu-root/simulation/firmware-identity.fat',
             base / 'menu-root/simulation/firmware-version.dat4',
             base / 'qemu-kernel-probe.sh', base / 'menu-root/usr/lib/libmpr.so',
             base / 'menu-root/cp/cpapp-b.bin', base / 'menu-root/simulation/seed-power-on',
             base / 'seed-power-on.c', base / 'menu-root/simulation/libIMDB.so',
             base / 'seed-date-clock.c', base / 'menu-root/simulation/seed-date-clock',
             base / 'menu-root/simulation/osal-uipc-transport-errors.ko',
             base / 'menu-root/usr/lib/appFw.so', base / 'menu-root/usr/lib/libObj.so',
             base / 'menu-root/usr/lib/libgpe.so', base / 'menu-root/usr/lib/libupdaterufp.so',
             Path(env.get('QEMU_SYSTEM_DIR', str(base.parent / 'sources/qemu-11.1.2/build-sony-ui'))) / env.get('QEMU_SYSTEM_BINARY', 'qemu-system-aarch64')]:
    if path.is_file():
        manifest['files'][str(path)] = sha256_file(path)
identity = json.loads((base / 'runtime-manifest.json').read_text())
for library in ('usr/lib/appFw.so', 'usr/lib/libObj.so'):
    if manifest['files'][str(base / 'menu-root' / library)] != identity[library]:
        raise RuntimeError('Original library identity changed: ' + library)
manifest['original_libraries_unchanged'] = True
if args.dmm_service and manifest['files'][str(base / 'menu-root/cp/DmmCfg.bin')] != 'd3466cee62fd86d927236a2c6c37dd633f76cdc181b4ad07a9863696aab76660':
    raise RuntimeError('CP DMM config differs from the verified two-slot model')
if args.software_gpe and manifest['files'][str(base / 'menu-root/usr/lib/libgpe.so')] != '80fc6236379933f80c2689aa002e0cf39483da8d45e876705d2a427cd1a2a7d1':
    raise RuntimeError('GPE library differs from the verified worker/GOT layout')
(out / 'runtime-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
with (out / "console.log").open("a" if live_fd else "w") as console:
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=console,
                            stderr=subprocess.STDOUT, env=env)
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(15)
            deadline = time.monotonic() + 8
            while True:
                try:
                    from menu_live import connect_in_run
                    connect_in_run(sock, out, 'qmp.sock')
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("QMP unavailable; inspect console.log")
                    time.sleep(0.1)
            with sock.makefile("rwb") as stream:
                if "QMP" not in json.loads(stream.readline()):
                    raise RuntimeError("Unexpected QMP greeting")

                def qmp(name, arguments=None):
                    message = {"execute": name}
                    if arguments:
                        message['arguments'] = arguments
                    stream.write((json.dumps(message) + "\n").encode())
                    stream.flush()
                    while True:
                        result = json.loads(stream.readline())
                        if "error" in result:
                            raise RuntimeError(result["error"])
                        if "return" in result:
                            return

                qmp("qmp_capabilities")
                deadline = time.monotonic() + 25
                while b"/ #" not in (out / "console.log").read_bytes():
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("Merged filesystem did not reach shell")
                    time.sleep(0.1)
                setup = ["mount -t proc proc /proc", "mount -t sysfs sysfs /sys",
                         "mount -t tmpfs tmpfs /tmp", "mount -t tmpfs tmpfs /setting",
                         "mount -t tmpfs tmpfs /tmp_wbshd",
                         "export PATH=/bin:/sbin:/usr/bin:/usr/sbin",
                         "export LD_LIBRARY_PATH=/usr/lib:/lib",
                         "mkdir -p /dev/cgroup", "mount -t cgroup -o cpuset nodev /dev/cgroup",
                         "for group in cpu0 cpu1 cpu2 cpu3rt cpu0_1 cpu0_1_2 cpu0_1_2_3rt; do mkdir -p /dev/cgroup/$group; echo 0 > /dev/cgroup/$group/cpuset.mems; echo 0 > /dev/cgroup/$group/cpuset.cpus; done",
                         "echo MENU_ROOT_READY", "ls -l /usr/bin/im.elf /usr/lib/appFw.so"]
                if args.load_ipc:
                    setup += ["insmod /kmod/devno.ko", "insmod /usr/kmod/osal_utm.ko",
                              "insmod " + ("/simulation/osal-uipc-transport-errors.ko" if args.transport_errors else "/usr/kmod/osal_uipc.ko"), "ls -l /dev/uipc"]
                extra_module_setup = []
                if os.environ.get("MENU_EXTRA_MODULES"):
                    # The real /sbin/initrc/insmod_drv2 loads 38 modules; the probe used to load 10.
                    # These files are present in the test root but were never insmod-ed. The PCIe/CP
                    # transport modules go last so a fault there cannot hide the other results.
                    modules = ["input", "ldec", "icdt", "ioexp", "indctr", "udate",
                               "usb_portMonitor", "usbg_sen", "usb_extcmd", "mcmn_drv",
                               "codec_drv", "mmc_drv", "ms_drv", "nvme-core", "nvme", "compass"]
                    if os.environ["MENU_EXTRA_MODULES"] not in ("safe", "no-pcie"):
                        # pcieatu's udm_pciatu thread null-derefs without a PCIe model, so the
                        # transport trio only goes in when explicitly requested.
                        modules += ["pci_ep_cp", "pcieatu", "pcie-if"]
                    setup += ["echo set > /proc/udm/drivers", "echo UPM_DRIVER_SET_RC=$?"]
                    for module in modules:
                        skip = {"usb_portMonitor": "missing-USB-core-null-vbus",
                                "mmc_drv": "unmapped-MMC-controller-read32",
                                "ms_drv": "Sony-MS-controller-not-modeled"}.get(module)
                        if os.environ["MENU_EXTRA_MODULES"] == "safe" and skip:
                            extra_module_setup.append("echo MOD_SKIP %s %s" % (module, skip))
                            continue
                        extra_module_setup.append("insmod /usr/kmod/%s.ko >/dev/null 2>&1 && echo MOD_OK %s || echo MOD_FAIL %s" % (module, module, module))
                if args.sony_timers:
                    setup += ["insmod /usr/kmod/utimer.ko"]
                if args.seed_backup:
                    setup += ["cp /seed-backup.bin /setting/Backup.bin",
                              "insmod /usr/kmod/backup.ko Backup_load=1 Backup_save=0",
                              "ls -l /dev/mem /dev/backup", "insmod /usr/kmod/stream.ko",
                              "insmod /usr/kmod/stream2.ko"]
                if args.load_dmm:
                    setup += ["cp -a /seed-setting/. /setting/",
                              "insmod /usr/kmod/osal_ulogio.ko ulogio_initfile=/setting/ulogio.bin",
                              "insmod /usr/kmod/dmm.ko DmmConfig=/setting/DmmCfg.bin",
                              "insmod /usr/kmod/grm_ma.ko"]
                # input/ioexp/udate and peers import Backup_*; icdt also follows DMM in initrc.
                setup += extra_module_setup
                if args.firmware_identity:
                    setup += ["mkdir -p /setting/updater /mnt",
                              "cp /simulation/firmware-version.dat4 /setting/updater/dat4",
                              "mknod /dev/ram1 b 1 1", "ln -s /dev/ram1 /dev/nflasha1",
                              "dd if=/simulation/firmware-identity.fat of=/dev/ram1 bs=4096",
                              "echo VIRTUAL_FIRMWARE_IDENTITY_FILES_READY"]
                if args.seed_power_on:
                    setup += [f"/simulation/seed-power-on {boot_type}",
                              "echo POWER_ON_SEED_STATUS=$?"]
                if args.load_power:
                    setup += ["insmod /usr/kmod/upm.ko"]
                if os.environ.get('MENU_DATE_SERVICE') == '1':
                    # The native date/settings API is served by this original driver.
                    setup += ["if insmod /usr/kmod/udate.ko && /simulation/seed-date-clock; then touch /tmp/date-clock.ready; echo DATE_CLOCK_INIT_OK; else echo DATE_CLOCK_INIT_FAILED; fi"]
                if args.load_kikilog:
                    setup += ["insmod /usr/kmod/kikilog.ko", "ls -l /dev/usb/kikilog"]
                if args.no_cp_updater:
                    setup += ["export QEMU_NO_CP_UPDATER=1"]
                if args.capid_service:
                    setup += ["export QEMU_CAPID_SERVICE=1"]
                if args.sa_address_service:
                    setup += ["export QEMU_SA_ADDRESS_SERVICE=1"]
                if args.hdmi_format_service:
                    setup += ["export QEMU_HDMI_FORMAT_SERVICE=1"]
                if args.hdmi_photo_service:
                    setup += ["export QEMU_HDMI_PHOTO_SERVICE=1"]
                if args.dmm_service:
                    setup += ["export QEMU_DMM_SERVICE=1"]
                if args.software_gpe:
                    setup += ["export QEMU_SOFTWARE_GPE=1"]
                if args.headless_osd:
                    setup += ["export MENU_HEADLESS_OSD=1"]
                if args.native_display is not None:
                    setup += ["/simulation/ramfb-viewer >/dev/null &",
                              "echo RAMFB_VIEWER_PID=$!"]
                if os.environ.get("MENU_GPE_RGBA4444"):
                    setup += ["export QEMU_GPE_RGBA4444=1"]
                if args.display_info_service:
                    setup += ["export QEMU_DISPLAY_INFO_SERVICE=1"]
                if os.environ.get("MENU_BB_READY_LATE"):
                    # Experiment E2 (screen-blocker/REPORT.md): hold the single 0x2003/0x8201
                    # fulfilment until after the cold-boot chain and an injected key.
                    setup += ["export MENU_BB_READY_LATE=1"]
                if os.environ.get("MENU_SUBSCRIPTION_SERVICE"):
                    setup += ["export QEMU_SUBSCRIPTION_SERVICE=1"]
                if os.environ.get("MENU_SUBSCRIPTION_EVENT"):
                    setup += ["export QEMU_SUBSCRIPTION_EVENT=1"]
                if os.environ.get("MENU_SCREEN_REGISTRY"):
                    setup += ["export MENU_SCREEN_REGISTRY=1"]
                if os.environ.get("MENU_CTOR_CENSUS"):
                    setup += ["export MENU_CTOR_CENSUS=1"]
                if os.environ.get("MENU_OSD_OBSERVE") or os.environ.get("MENU_SOFTWARE_LZ77"):
                    setup += ["export MENU_OSD_OBSERVE=1"]
                if os.environ.get("MENU_SOFTWARE_LZ77"):
                    setup += ["export QEMU_SOFTWARE_LZ77=1"]
                if os.environ.get("MENU_FRAME_OBSERVE") or os.environ.get("MENU_APPLY_MENU"):
                    setup += ["export MENU_FRAME_OBSERVE=1"]
                if os.environ.get("MENU_APPLY_MENU"):
                    setup += ["export MENU_APPLY_MENU=1"]
                if os.environ.get("MENU_ACTIVATE_CAPTURED"):
                    setup += ["export MENU_ACTIVATE_CAPTURED=" + os.environ["MENU_ACTIVATE_CAPTURED"]]
                if os.environ.get("MENU_OPEN_SET"):
                    setup += ["export MENU_OPEN_SET=" + os.environ["MENU_OPEN_SET"]]
                if os.environ.get("MENU_OPEN_SET_LOG_ONLY"):
                    setup += ["export MENU_OPEN_SET_LOG_ONLY=1"]
                if os.environ.get("MENU_OPEN_SET_EXECUTE"):
                    setup += ["export MENU_OPEN_SET_EXECUTE=1"]
                if os.environ.get("MENU_UI_THREAD_ONLY"):
                    setup += ["export MENU_UI_THREAD_ONLY=1"]
                if os.environ.get("MENU_BUILD2_THEN_INIT"):
                    setup += ["export MENU_BUILD2_THEN_INIT=1"]
                if os.environ.get("MENU_BUILD_CONTROLLER2"):
                    setup += ["export MENU_BUILD_CONTROLLER2=" + os.environ["MENU_BUILD_CONTROLLER2"]]
                if os.environ.get("MENU_MODE_SENTINELS"):
                    setup += ["export MENU_MODE_SENTINELS=1"]
                if os.environ.get("MENU_ACTIVATE_MODE"):
                    setup += ["export MENU_ACTIVATE_MODE=" + os.environ["MENU_ACTIVATE_MODE"]]
                if os.environ.get("MENU_FORCE_OPEN_AFTER"):
                    setup += ["export MENU_FORCE_OPEN_AFTER=1"]
                if os.environ.get("MENU_FORCE_IN_FACTORY"):
                    setup += ["export MENU_FORCE_IN_FACTORY=1"]
                if os.environ.get("MENU_STDIO_KMSG"):
                    setup += ["export MENU_STDIO_KMSG=1"]
                if os.environ.get("MENU_INIT_LOG_ONLY"):
                    setup += ["export MENU_INIT_LOG_ONLY=1"]
                if os.environ.get("MENU_BUILD_CONTROLLER"):
                    setup += ["export MENU_BUILD_CONTROLLER=" + os.environ["MENU_BUILD_CONTROLLER"]]
                if os.environ.get("MENU_OPEN_SCREEN"):
                    setup += ["export MENU_OPEN_SCREEN=1"]
                if os.environ.get("MENU_MENU_INIT"):
                    setup += ["export MENU_MENU_INIT=1"]
                if os.environ.get("MENU_INIT_SELECTOR"):
                    setup += ["export MENU_INIT_SELECTOR=" + os.environ["MENU_INIT_SELECTOR"]]
                if os.environ.get("MENU_FORCE_SET"):
                    setup += ["export MENU_FORCE_SET=" + os.environ["MENU_FORCE_SET"]]
                if os.environ.get("MENU_FACTORY_HOOK"):
                    setup += ["export MENU_FACTORY_HOOK=1"]
                if os.environ.get("MENU_UX_NOTIFY_DRAW"):
                    setup += ["export MENU_UX_NOTIFY_DRAW=1"]
                if os.environ.get("MENU_TASK_MAIN_EVENTS"):
                    setup += ["export MENU_TASK_MAIN_EVENTS=1"]
                if os.environ.get("MENU_SEQ_EVENT"):
                    setup += ["export MENU_SEQ_EVENT=1"]
                if os.environ.get("MENU_SEQ_EVENT_SEL"):
                    setup += ["export MENU_SEQ_EVENT_SEL=" + os.environ["MENU_SEQ_EVENT_SEL"]]
                if os.environ.get("MENU_DISPLAY_SIZE"):
                    setup += [f"export QEMU_DISPLAY_SIZE={os.environ['MENU_DISPLAY_SIZE']}"]
                if os.environ.get("MENU_DISPLAY_ASPECT"):
                    setup += [f"export QEMU_DISPLAY_ASPECT={os.environ['MENU_DISPLAY_ASPECT']}"]
                if args.menu_target:
                    setup += ["export LD_LIBRARY_PATH=/simulation:/usr/lib:/lib",
                              "ls -l /simulation/libIMDB.so",
                              "unset LD_DEBUG"]
                if args.observe:
                    setup += ["MENU_OBSERVE_SELFTEST=1 LD_PRELOAD=/simulation/menu-observe.so /simulation/check-menu-observe",
                              "echo OBSERVER_CHECK_STATUS=$?", "/simulation/menu-regs --selftest", "echo REGS_SELFTEST_STATUS=$?", "rm -f /tmp/menu-load.log",
                              "rm -f /tmp/gpe-requests.bin",
                              "/simulation/menu-readmem /usr/lib/appFw.so 0 16 > /tmp/readmem-check.txt",
                              "/simulation/menu-readmem /usr/lib/appFw.so 0 4097; bad=$?; /simulation/menu-readmem /proc/self/mem 0 4 libc-2.25.so > /tmp/readmem-module-check.txt; mapped=$?; if [ $bad -eq 2 ] && [ $mapped -eq 0 ] && grep -q 'bytes=16' /tmp/readmem-check.txt && grep -q '7f 45 4c 46' /tmp/readmem-module-check.txt; then echo READMEM_CHECK_PASS; else echo READMEM_CHECK_FAILED; fi"]
                if live_fd:
                    # Research logs are unbounded; the live UI only needs immutable OSD exports.
                    setup += ["rm -f /tmp/menu-load.log /tmp/gpe-requests.bin /tmp/im.log",
                              "ln -s /dev/null /tmp/menu-load.log; ln -s /dev/null /tmp/gpe-requests.bin; ln -s /dev/null /tmp/im.log"]
                preloads = []
                media_selftest = os.environ.get('MENU_MEDIA_SELFTEST') == '1'
                if media_selftest:
                    if live_fd or args.native_display is not None or args.headless_osd:
                        raise RuntimeError('Media selftest requires an exclusive non-display guest')
                    setup += ['export A7C2_FILE_MEDIA_TEST=1',
                              'cp /simulation/media-template.fat /image_mmca']
                    preloads.append('/simulation/file-media-config.so')
                if os.environ.get('MENU_TRACE_MUTEX') == '1':
                    setup += ['export MUTEX_TRACE_CONSOLE=1']
                    preloads.append('/simulation/mutex-trace.so')
                if args.observe:
                    preloads.append('/simulation/menu-observe.so')
                prefix = 'LD_PRELOAD=' + ':'.join(preloads) + ' ' if preloads else ''
                if os.environ.get('MENU_DATE_SERVICE') == '1':
                    prefix = 'test -f /tmp/date-clock.ready && ' + prefix
                if media_selftest:
                    setup += ['(' + prefix + '/simulation/fsys-file-test && ' + prefix +
                              '/simulation/fsys-file-test verify; echo $? > /tmp/media-test.rc) > /tmp/media-test.log 2>&1 &']
                elif args.bootin:
                    setup += [prefix + "/usr/bin/bootin.elf 'target=qemu ' dmode=3 > /tmp/im.log 2>&1 &"]
                else:
                    options = ' target=qemu' + (' bootall' if args.bootall else '')
                    setup += [prefix + "/usr/bin/im.elf" + options + " > /tmp/im.log 2>&1 &"]
                setup += ["echo IM_LAUNCH_PID=$!"]
                proc.stdin.write(("\n".join(setup) + "\n").encode())
                proc.stdin.flush()
                if media_selftest:
                    from menu_live import serial_command
                    result = serial_command(proc, out / 'console.log',
                        'while [ ! -f /tmp/media-test.rc ]; do sleep 1; done; cat /tmp/media-test.log; test "$(cat /tmp/media-test.rc)" = 0', timeout=90)
                    if b'FSYS_FILE_TEST_DONE result=0 verify_only=1' not in result:
                        raise RuntimeError('Original Fsys verification marker missing')
                    qmp('pmemsave', {'val': 0x41e600000, 'size': 0x400000, 'filename': '/out/media-disk.img'})
                    qmp('quit')
                    print('FSYS_MEDIA_IMAGE_CAPTURED ' + str(out / 'media-disk.img'), flush=True)
                    raise SystemExit(0)
                settle = int(os.environ.get("MENU_SETTLE_SECONDS", "90"))
                settle_deadline = time.monotonic() + settle
                crashed_during_settle = False
                # Candidate raster from an earlier run. Address stability and pixel format remain
                # unverified; these memory samples alone do not prove a displayed frame.
                screen_dumps = 0
                next_screen_dump = time.monotonic() + 10
                while time.monotonic() < settle_deadline:
                    if re.search(rb'(?:^|\n)DATE_CLOCK_INIT_FAILED\r?\n', (out / 'console.log').read_bytes()):
                        raise RuntimeError('Original date clock initialization failed; application not started')
                    if b'Exception>' in (out / "console.log").read_bytes():
                        crashed_during_settle = True
                        break
                    if time.monotonic() >= next_screen_dump and screen_dumps < 10:
                        qmp('pmemsave', {'val': 0x43a7b4100, 'size': 640 * 480 * 2,
                                         'filename': f'/out/screen-{screen_dumps:02d}.bin'})
                        # Opaque bytes only until pixel channel order and actual OSD submission agree.
                        matches = re.findall(rb'OBS FRAME_BUFFERS call=(\d+) width=(\d+) height=(\d+) bmp_format=(\d+) on=(0x[0-9a-f]+) off=(0x[0-9a-f]+)',
                                             (out / 'console.log').read_bytes())
                        if matches:
                            frame, width, height, fmt = map(int, matches[-1][:4])
                            bpp = {1: 2, 7: 2, 2: 4, 8: 4}.get(fmt, 0)
                            size = width * height * bpp
                            if 0 < width <= 8192 and 0 < height <= 8192 and 0 < size <= 0x1000000:
                                qmp('stop')
                                try:
                                    for role, raw_address in zip(('on', 'off'), matches[-1][4:]):
                                        address = int(raw_address, 16)
                                        if not 0x430400000 <= address <= 0x500000000 - size:
                                            continue
                                        name = f'framebuffer-{role}-{screen_dumps:02d}'
                                        qmp('pmemsave', {'val': address, 'size': size, 'filename': f'/out/{name}.bin'})
                                        (out / f'{name}.json').write_text(json.dumps({
                                            'source': 'observed GRM descriptor; scanout not verified',
                                            'frame_call': frame, 'role': role, 'address': hex(address),
                                            'width': width, 'height': height, 'row_pixels': width,
                                            'bmp_format': fmt, 'bytes': size, 'channel_order_verified': False,
                                            'descriptor_may_have_changed_since_log': True,
                                        }, indent=2) + '\n')
                                finally:
                                    qmp('cont')
                        print(f"SCREEN_DUMP {screen_dumps}", flush=True)
                        screen_dumps += 1
                        next_screen_dump = time.monotonic() + 15
                    time.sleep(1.0)
                if crashed_during_settle:
                    qmp('pmemsave', {'val': 0x405000000, 'size': 0x800000,
                                     'filename': '/out/menu-ulogio.bin'})
                    qmp('pmemsave', {'val': 0x4102bb400, 'size': 0x8000,
                                     'filename': '/out/menu-monitor.bin'})
                    qmp('pmemsave', {'val': 0x4102d0000, 'size': 0x10000,
                                     'filename': '/out/menu-loader.bin'})
                    import re as _re
                    _m = _re.search(rb'stackpage: ([0-9a-f]+)', (out / "console.log").read_bytes())
                    if _m:
                        _va = int(_m.group(1), 16)
                        _phys = _va - 0xffffffc000000000 + 0x400000000
                        if 0 < _phys < 0x800000000:
                            qmp('pmemsave', {'val': _phys & ~0xFFF, 'size': 0x4000,
                                             'filename': '/out/crash-stack.bin'})
                            print(f"CRASH_STACK_DUMPED va={hex(_va)} phys={hex(_phys)}", flush=True)
                    print("GUEST_CRASHED_DURING_SETTLE_DUMPS_CAPTURED", flush=True)
                    # Ask the Sony exception monitor for registers and a backtrace
                    # before quitting: the console capture keeps the answer.
                    try:
                        proc.stdin.write(b'help\nbt\nregs\n')
                        proc.stdin.flush()
                        time.sleep(6)
                    except Exception:
                        pass
                    qmp('quit')
                    raise RuntimeError("guest exception monitor during settle")
                if os.environ.get('MENU_DATE_SERVICE') == '1' and not re.search(
                        rb'(?:^|\n)DATE_CLOCK_INIT_OK\r?\n', (out / 'console.log').read_bytes()):
                    raise RuntimeError('Original date clock verification marker missing')
                if live_fd:
                    from menu_live import run_live
                    try:
                        run_live(proc, out, int(live_fd))
                    finally:
                        if proc.poll() is None:
                            qmp('quit')
                    raise SystemExit(0)
                # Optional key injection: MENU_KEY_INJECT="key_code,sub,press_status,ex,from[,hold_seconds[,release_status]]"
                # Multiple shots separated by ';' are injected in sequence with a pause between them.
                inject = os.environ.get("MENU_KEY_INJECT", "")
                if inject:
                    queue = os.environ.get("MENU_KEY_QUEUE", "0x807a0020")
                    src = os.environ.get("MENU_KEY_SRC", "0x92840747")
                    shot = []
                    for shot_index, spec in enumerate(inject.split(";")):
                        parts = spec.split(",")
                        if len(parts) not in (5, 6, 7):
                            raise SystemExit("MENU_KEY_INJECT expects key_code,sub,press_status,ex,from[,hold_seconds[,release_status]] per shot, ';'-separated")
                        shot.append("echo INJECT_SHOT_BEGIN=%d" % shot_index)
                        shot.append("/simulation/inject-key %s %s %s %s %s %s %s" % (queue, src, *parts[:5]))
                        if len(parts) >= 6:
                            release_status = parts[6] if len(parts) == 7 else "0x7fffffff"
                            shot += ["sleep %s" % parts[5],
                                     "/simulation/inject-key %s %s %s %s %s %s %s" % (queue, src, parts[0], parts[1], release_status, parts[3], parts[4])]
                        shot.append("sleep %s" % os.environ.get("MENU_KEY_POST_WAIT", "8"))
                    proc.stdin.write(("\n".join(shot) + "\n").encode())
                    proc.stdin.flush()
                # Optional CP event injection: MENU_CP_EVENT_INJECT="frame,code,sub,param[,wait_seconds]"
                # Multiple events separated by ';' are delivered to the dataflow CP port in order.
                cp_events = os.environ.get("MENU_CP_EVENT_INJECT", "")
                if cp_events:
                    cp_queue = os.environ.get("MENU_CP_EVENT_QUEUE", "0x81cf618b")
                    cp_src = os.environ.get("MENU_CP_EVENT_SRC", "0x81cf618a")
                    shot = []
                    for shot_index, spec in enumerate(cp_events.split(";")):
                        parts = spec.split(",")
                        if len(parts) not in (4, 5):
                            raise SystemExit("MENU_CP_EVENT_INJECT expects frame,code,sub,param[,wait_seconds] per shot, ';'-separated")
                        if shot_index:
                            shot.append("sleep %s" % (parts[4] if len(parts) == 5 else "3"))
                        shot.append("echo CP_EVENT_SHOT_BEGIN=%d" % shot_index)
                        shot.append("/simulation/inject-cp-event %s %s %s %s %s %s" % (cp_queue, cp_src, *parts[:4]))
                    shot.append("sleep %s" % os.environ.get("MENU_CP_EVENT_POST_WAIT", "10"))
                    proc.stdin.write(("\n".join(shot) + "\n").encode())
                    proc.stdin.flush()
                diagnostic = ["/bin/busybox sh /simulation/diag.sh"]  # baked-in script: giant one-liners get silently truncated when the guest tty input queue overruns
                if args.headless_osd:
                    diagnostic += ["/simulation/headless-osd-export; echo HEADLESS_OSD_EXPORT_RC=$?"]
                if args.native_display is not None:
                    diagnostic += ["cat /tmp/ramfb-viewer.log /tmp/ramfb-viewer.status"]
                if os.environ.get('MENU_INPUT_STATE') == '1':
                    diagnostic += ["/bin/busybox sh /simulation/input-state.sh; echo INPUT_STATE_STATUS=$?"]
                if os.environ.get('MENU_UI_STACK') == '1':
                    # Explicit diagnostic: briefly ptrace-stop this guest UI thread,
                    # read its registers/frame chain, then detach without writing it.
                    diagnostic += ["tid=$(/bin/busybox sed -n '/FRAME_DRAW_ENTER call=1 tid=/{s/.* tid=\\([0-9]*\\).*/\\1/;p;q;}' /tmp/menu-load.log); echo BOOT_UI_TID=$tid; test -n \"$tid\" && /simulation/menu-regs \"$tid\"; echo BOOT_UI_STACK_STATUS=$?"]
                # Earlier queued key commands can emit prompts; only our own marker proves completion.
                prompt_deadline = time.monotonic() + int(os.environ.get("MENU_DIAG_TIMEOUT", "240"))
                for diagnostic_index, line in enumerate(diagnostic):
                    marker = f"MENU_DIAG_COMMAND_DONE_{diagnostic_index}"
                    proc.stdin.write((line + "\nprintf '\\n" + marker + "\\n'\n").encode())
                    proc.stdin.flush()
                    while not diagnostic_done((out / "console.log").read_bytes(), marker):
                        if b'Exception>' in (out / "console.log").read_bytes():
                            # Guest hit the Sony exception monitor before diag completed:
                            # capture the crash-state memory for offline analysis.
                            qmp('pmemsave', {'val': 0x405000000, 'size': 0x800000,
                                             'filename': '/out/menu-ulogio.bin'})
                            qmp('pmemsave', {'val': 0x4102bb400, 'size': 0x8000,
                                             'filename': '/out/menu-monitor.bin'})
                            print("GUEST_CRASH_DUMPS_CAPTURED", flush=True)
                            try:
                                proc.stdin.write(b'help\nbt\nregs\n')
                                proc.stdin.flush()
                                time.sleep(6)
                            except (OSError, BrokenPipeError):
                                pass
                            qmp('quit')
                            raise RuntimeError("guest exception monitor before diagnostics")
                        if time.monotonic() > prompt_deadline:
                            raise RuntimeError("diagnostic command timed out waiting for completion marker: " + line[:60])
                        time.sleep(0.5)
                proc.stdin.flush()
                deadline = time.monotonic() + int(os.environ.get("MENU_DIAG_TIMEOUT", "120"))
                while b"\nMENU_PROBE_FINISHED" not in (out / "console.log").read_bytes():
                    captured = (out / "console.log").read_bytes()
                    if b'Exception>' in captured:
                        if args.dmm_service:
                            qmp('pmemsave', {'val': 0x41e000000, 'size': 0x100000,
                                             'filename': '/out/cp-dmm-pool.bin'})
                        qmp('pmemsave', {'val': 0x405000000, 'size': 0x800000,
                                         'filename': '/out/menu-ulogio.bin'})
                        qmp('pmemsave', {'val': 0x4102d0000, 'size': 0x10000,
                                         'filename': '/out/menu-loader.bin'})
                        data = (out / 'menu-loader.bin').read_bytes()
                        count = int.from_bytes(data[:4], 'little')
                        (out / 'loader.txt').write_text(data[8:8 + min(count, 65528)].decode(errors='replace'))
                        if args.software_gpe:
                            fills = re.findall(r'GPE_SOFTWARE_FILL sequence=(\d+) address=(0x[0-9a-f]+) bytes=(\d+) width=(\d+) height=(\d+) format=(\d+) result=0',
                                               (out / 'loader.txt').read_text())
                            if fills:
                                sequence, address, size, width, height, fmt = fills[0]
                                qmp('pmemsave', {'val': int(address, 16), 'size': int(size), 'filename': '/out/gpe-first-fill.bin'})
                                (out / 'gpe-first-fill.json').write_text(json.dumps({'address': address, 'bytes': int(size),
                                    'width': int(width), 'height': int(height), 'format': int(fmt),
                                    'source': 'original UX uniform fill, not a rendered menu'}, indent=2) + '\n')
                        (out / 'summary.json').write_text(json.dumps({
                            'run': str(out), 'input_image': command[-1],
                            'probe_collected': False, 'guest_exception': True,
                            'loader_log_bytes': count, 'loader_log_truncated': count > 65528,
                            'loader_window_covers_first_64KiB_only': True,
                            'visible_menu_verified': False,
                        }, indent=2) + '\n')
                        before = len(captured)
                        proc.stdin.write(b'exit\ncat /tmp/menu-load.log\necho FAILED_RUN_LOG_COLLECTED\n')
                        proc.stdin.flush()
                        recovery_deadline = time.monotonic() + 5
                        while time.monotonic() < recovery_deadline:
                            recovered = (out / 'console.log').read_bytes()[before:]
                            if b'\nFAILED_RUN_LOG_COLLECTED' in recovered:
                                (out / 'failure-loader-console.txt').write_bytes(recovered)
                                break
                            time.sleep(0.1)
                        qmp('quit')
                        raise RuntimeError('Guest entered Sony exception monitor; see console.log')
                    if time.monotonic() > deadline:
                        raise RuntimeError("Probe did not finish; inspect guest crash/output")
                    time.sleep(0.1)
                if args.headless_osd:
                    try:
                        info = capture_headless_osd((out / 'console.log').read_bytes(), out)
                        print(f"HEADLESS_OSD_CAPTURE seq={info['sequence']} sha256={info['sha256']}", flush=True)
                    except (ValueError, IndexError) as error:
                        (out / 'headless-osd-error.txt').write_text(str(error) + '\n')
                        print(f"HEADLESS_OSD_CAPTURE_FAILED {error}", flush=True)
                if args.seed_backup:
                    qmp('stop')
                    if args.software_gpe:
                        sources = raster_sources((out / 'console.log').read_text(errors='replace'))
                        captured_sources, used_bytes = [], 0
                        for source in sources:
                            if len(captured_sources) >= 64 or used_bytes + source['bytes'] > 0x2000000:
                                continue
                            name = f"raster-source-{source['address']:x}-f{source['format']}-s{source['row_pixels']}.bin"
                            qmp('pmemsave', {'val': source['address'], 'size': source['bytes'], 'filename': '/out/' + name})
                            captured_sources.append(dict(source, filename=name))
                            used_bytes += source['bytes']
                        (out / 'raster-sources.json').write_text(json.dumps({
                            'source': 'post-diagnostic snapshot; allocation may have been reused since draw request',
                            'captured': captured_sources, 'dropped': len(sources) - len(captured_sources),
                            'bytes': used_bytes, 'channel_order_verified': False,
                        }, indent=2) + '\n')
                        print(f"RASTER_SOURCES captured={len(captured_sources)} dropped={len(sources)-len(captured_sources)} bytes={used_bytes}", flush=True)
                    if args.capid_service:
                        qmp('pmemsave', {'val': 0x4102e0000, 'size': 0x4014, 'filename': '/out/capid-table.bin'})
                    qmp('pmemsave', {'val': 0x4102bb400, 'size': 0x8000, 'filename': '/out/menu-monitor.bin'})
                    qmp('pmemsave', {'val': 0x405000000, 'size': 0x800000, 'filename': '/out/menu-ulogio.bin'})
                    qmp('pmemsave', {'val': 0x4102d0000, 'size': 0x10000,
                                     'filename': '/out/menu-loader.bin'})
                if args.native_display is not None:
                    try:
                        qmp('screendump', {'filename': '/out/ramfb-screen.ppm'})
                        print('RAMFB_SCREENDUMP ramfb-screen.ppm (pixel comparison still required)', flush=True)
                    except RuntimeError as error:
                        (out / 'ramfb-screen-error.txt').write_text(str(error) + '\n')
                        print(f'RAMFB_SCREENDUMP_FAILED {error}', flush=True)
                qmp("quit")
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
def convert_screens(run_dir):
    """Candidate RGB565 interpretations only: neither display use nor pixel format is verified."""
    try:
        from PIL import Image
    except Exception:
        return []
    written = []
    for path in sorted(run_dir.glob('screen-*.bin')):
        raw = path.read_bytes()
        if len(raw) < 640 * 480 * 2:
            continue
        for order in ('little', 'big'):
            image = Image.new('RGB', (640, 480))
            pixels = image.load()
            for index in range(640 * 480):
                value = int.from_bytes(raw[index * 2:index * 2 + 2], order)
                pixels[index % 640, index // 640] = (
                    ((value >> 11) & 0x1F) * 255 // 31,
                    ((value >> 5) & 0x3F) * 255 // 63,
                    (value & 0x1F) * 255 // 31)
            target = path.with_name(path.stem + f'-candidate-rgb565-{order}.png')
            image.save(target)
            written.append((target, image.getcolors(maxcolors=65536) or []))
    if os.environ.get('MENU_GPE_RGBA4444'):
        pixel_sources = [(path, path.with_suffix('.json')) for path in sorted(run_dir.glob('framebuffer-*.bin'))]
        committed = run_dir / 'headless-osd-pixels.bin'
        if committed.exists():
            pixel_sources.append((committed, run_dir / 'headless-osd-frame.json'))
        for path, metadata_path in pixel_sources:
            metadata = json.loads(metadata_path.read_text())
            width, height, stride = metadata['width'], metadata['height'], metadata['row_pixels']
            raw = path.read_bytes()
            if metadata['bmp_format'] not in (1, 7) or not (0 < width <= stride <= 16384 and 0 < height <= 8192):
                continue
            if len(raw) < ((height - 1) * stride + width) * 2:
                continue
            rgba = bytearray(width * height * 4)
            for y in range(height):
                for x in range(width):
                    offset = (y * stride + x) * 2
                    value = raw[offset] | raw[offset + 1] << 8
                    i = (y * width + x) * 4
                    rgba[i:i + 4] = bytes(((value >> 12) * 17, (value >> 8 & 15) * 17,
                                            (value >> 4 & 15) * 17, (value & 15) * 17))
            image = Image.frombytes('RGBA', (width, height), bytes(rgba))
            target = path.with_name(path.stem + '-rgba4444-software.png')
            image.save(target)
            target.with_suffix('.json').write_text(json.dumps(dict(metadata,
                raw_capture=path.name, decoder='experimental RGBA4444 software model; not hardware format proof'), indent=2) + '\n')
            written.append((target, image.getcolors(maxcolors=65536) or []))
    return written

screens = convert_screens(out)
for target, colors in screens:
    flat = sorted(colors, key=lambda item: -item[0])[:3]
    print(f"SCREEN_PNG {target.name} distinct={len(colors)} top={[(count, '#' + ''.join(f'{v:02x}' for v in color)) for count, color in flat]}", flush=True)

text = (out / 'console.log').read_text()
loader = out / 'menu-loader.bin'
loads = ''
if loader.exists():
    data = loader.read_bytes()
    count = int.from_bytes(data[:4], 'little')
    loads = data[8:8 + min(count, 65528)].decode(errors='replace')
    (out / 'loader.txt').write_text(loads)
def census(name, default=0):
    """Read a guest-side counter from the diag output. The kernel console can inject its own text
    into the middle of a guest line (including between 'name=' and the digits), so every occurrence
    is scanned and the diag script prints the whole block twice as insurance."""
    values = [int(found.group(1)) for found in re.finditer(r'\b' + re.escape(name) + r'=(\d+)', text)]
    return max(values) if values else default


summary = {'run': str(out), 'probe_collected': True,
           'input_image': command[-1],
           'cp_updater_transport_unavailable': args.no_cp_updater,
           'original_kikilog_requested': args.load_kikilog,
           'virtual_capid_service': args.capid_service,
           'virtual_sa_address_service': args.sa_address_service,
           'virtual_hdmi_audio_parameter': args.hdmi_format_service,
           'virtual_hdmi_photo_parameter': args.hdmi_photo_service,
           'virtual_cp_dmm_pool': args.dmm_service,
           'software_pixel_model': 'experimental straight RGBA4444/source-over/nearest; not hardware-bit-exact' if os.environ.get('MENU_GPE_RGBA4444') else 'limited verified operations',
           'software_lz77_requested': bool(os.environ.get('MENU_SOFTWARE_LZ77')),
           'loader_log_bytes': count if loader.exists() else 0,
           'loader_log_truncated': count > 65528 if loader.exists() else False,
           # The guest keeps the whole observer log in /tmp/menu-load.log and diag.sh counts
           # it there, so the census numbers below are the authoritative source. The 64 KiB
           # memory window (loader.txt) only holds the FIRST 64 KiB of records and must not be
           # used to conclude that a late event (appFw init) never happened.
           'lifecycle_evidence_source': 'guest census of the full log (/tmp/menu-load.log) via diag.sh',
           'loader_window_covers_first_64KiB_only': True,
           'appfw_library_loaded': census('appfw_loaded') > 0,
           'appfw_init_symbol_resolved': census('resolved_asi') > 0,
           'appfw_init_entered': census('enter_asi') > 0,
           'appfw_init_returned': census('return_asi') > 0,
           'uxengine_init2_entered': census('ux2_enter') > 0,
           'uxengine_init2_returned': census('ux2_return') > 0,
           'uxengine_init2_hook_installed': census('ux2_hook') > 0,
           'uxengine_init2_census': {'enter': census('ux2_enter'), 'return': census('ux2_return'),
                                     'hook': census('ux2_hook')},
           'gui_library_loaded': census('gui_loaded') > 0,
           'visible_menu_verified': False,
           'initialization_logs': str(out / 'loader.txt'),
           'full_guest_log_excerpts': str(out / 'console.log')}
(out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary, indent=2))
print('Capture completion does not establish a rendered menu.')
