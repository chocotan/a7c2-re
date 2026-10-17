#!/usr/bin/env bash
# Bounded attempt to boot the unmodified camera kernel on QEMU's generic virt board.
# No writable firmware, block disks, networking or physical devices are supplied.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
analysis_dir="$project_dir/analysis"
tool_root="${QEMU_SYSTEM_DIR:-$project_dir/sources/qemu-11.1.2/build-sony-ui}"
binary_relative="${QEMU_SYSTEM_BINARY:-qemu-system-aarch64}"
sony_timers="${SONY_TIMER_MODEL:-0}"
native_window="${MENU_NATIVE_WINDOW:-0}"
native_display="${QEMU_NATIVE_DISPLAY:-none}"
case "$native_window" in 0|1) ;; *) echo 'MENU_NATIVE_WINDOW must be 0 or 1' >&2; exit 2 ;; esac
case "$native_display" in none|gtk|sdl) ;; *) echo 'QEMU_NATIVE_DISPLAY must be none, gtk or sdl' >&2; exit 2 ;; esac
display_args=(-display none)
display_environment=()
if [[ "$native_window" == 1 ]]; then
  display_args=(-display "$native_display" -device ramfb,id=a7c2-screen)
  if [[ "$native_display" != none ]]; then
    # Only the local X11 socket and its cookie file enter the VM sandbox.
    [[ "${DISPLAY:-}" =~ ^:([0-9]+)(\.[0-9]+)?$ ]] || { echo 'Native window requires a local X11/Xwayland DISPLAY (for example :0)' >&2; exit 2; }
    x_socket="/tmp/.X11-unix/X${BASH_REMATCH[1]}"
    x_auth="${XAUTHORITY:-$HOME/.Xauthority}"
    [[ -S "$x_socket" && -r "$x_auth" ]] || { echo 'Local X11 socket or XAUTHORITY is unavailable' >&2; exit 2; }
    display_environment=(--dir /tmp/.X11-unix --ro-bind "$x_socket" "$x_socket"
      --dir /run --ro-bind "$x_auth" /run/a7c2-xauth --ro-bind /etc/fonts /etc/fonts
      --setenv DISPLAY "$DISPLAY" --setenv XAUTHORITY /run/a7c2-xauth
      --setenv GDK_BACKEND x11 --setenv SDL_VIDEODRIVER x11 --setenv NO_AT_BRIDGE 1
      --setenv XDG_CACHE_HOME /tmp/cache --setenv XDG_CONFIG_HOME /tmp/config)
    display_args=(-display "$native_display,gl=off" -device ramfb,id=a7c2-screen
      -name 'Sony A7C II - experimental menu')
  fi
  if [[ "${QEMU_NATIVE_INPUT:-0}" == 1 ]]; then
    display_args+=(-chardev socket,id=sonykeys,path=/out/native-input.sock,server=on,wait=off
      -device sony-ui-input,chardev=sonykeys,display=a7c2-screen)
  fi
elif [[ "$native_display" != none ]]; then
  echo 'Native display requires MENU_NATIVE_WINDOW=1' >&2; exit 2
fi
timer_environment=()
if [[ "$sony_timers" == 1 ]]; then
  timer_environment=(--setenv QEMU_SONY_TIMERS 1)
fi
bootfs="$analysis_dir/unpacked/firmware.tar_unpacked/0700_part_image/dev/nflasha3_unpacked"
test_initrd="${5:-$bootfs/initrd.img}"
ram_mb="${1:-1024}"
case "$ram_mb" in 1024|4096) ;; *) echo 'Use 1024 or 4096 MiB' >&2; exit 2 ;; esac
board_dtb="${2:-}"
loader_args=()
output_dir="$analysis_dir/qemu-kernel-output-$ram_mb"
if [[ -n "$board_dtb" ]]; then
  output_dir="${output_dir}-dtb"
  install -D -m 0644 "$board_dtb" "$output_dir/virt.dtb"
  loader_args=(-device loader,file=/out/virt.dtb,addr=0x40002000)
fi
boot_memory="${3:-0}"
case "$boot_memory" in 0|1) ;; *) echo 'Boot memory flag must be 0 or 1' >&2; exit 2 ;; esac
memory_config="$ram_mb"
if [[ "$boot_memory" == 1 ]]; then
  if [[ "$ram_mb" != 4096 || -z "$board_dtb" ]]; then
    echo 'Boot memory experiment requires 4096 MiB and a DTB' >&2; exit 2
  fi
  original_output="$output_dir"
  output_dir="${output_dir}-bootmem"
  install -D -m 0644 "$original_output/virt.dtb" "$output_dir/virt.dtb"
  # Sparse host allocation covers the fixed boot-parameter address.
  # The supplied DTB still describes only 4 GiB to the guest kernel.
  memory_config=16384
fi
skip_hwtimer="${4:-0}"
case "$skip_hwtimer" in 0|1) ;; *) echo 'Timer flag must be 0 or 1' >&2; exit 2 ;; esac
if [[ "$native_window" == 1 ]] && { [[ "$skip_hwtimer" != 1 || "$boot_memory" != 1 ]] || [[ "$test_initrd" != *.cpio && "$test_initrd" != *.cpio.gz ]]; }; then
  echo 'Native ramfb requires high-DDR boot memory and cpio initramfs' >&2; exit 2
fi
cmdline='console=ttyAMA0 earlycon=pl011,0x09000000 loglevel=8 root=/dev/ram0 init=/bin/sh'
high_ddr=0
if [[ "$test_initrd" != "$bootfs/initrd.img" ]]; then
  high_ddr=1
fi
if [[ "$skip_hwtimer" == 1 ]]; then
  if [[ "$boot_memory" != 1 ]]; then
    echo 'Timer experiment requires the boot memory setup' >&2; exit 2
  fi
  original_output="$output_dir"
  output_dir="${output_dir}-skip-hwtimer"
  install -D -m 0644 "$original_output/virt.dtb" "$output_dir/virt.dtb"
  # ponytail: skip seven absent Sony devices; add CXD90057 device models for full boot.
  if [[ "$test_initrd" != "$bootfs/initrd.img" ]]; then
    original_output="$output_dir"
    output_dir="${output_dir}-menu"
    install -D -m 0644 "$original_output/virt.dtb" "$output_dir/virt.dtb"
    image_kib=$(( ($(stat -c %s "$test_initrd") + 1023) / 1024 ))
    cmdline+=" ramdisk_size=$image_kib"
    if [[ "$test_initrd" == *.cpio || "$test_initrd" == *.cpio.gz ]]; then
      cmdline+=' rdinit=/init'
      workspace_parameters="$(python3 - "$analysis_dir/unpacked/firmware.tar_unpacked/0800_appli/setting/kemco.txt" <<'PY'
from pathlib import Path
import sys
print(' '.join(line for line in Path(sys.argv[1]).read_text().splitlines()
               if line.startswith('memrsv=') and 'tmonitor.' not in line))
PY
)"
      cmdline+=" $workspace_parameters"
      if [[ "${SONY_DMM_SERVICE:-0}" == 1 ]]; then
        # Separate virtual CP buffers from both Linux pages and camera DMM pools.
        cmdline+=' memrsv=0x00100000@0x41e000000:uc'
      fi
      if [[ "$native_window" == 1 ]]; then
        # Independent scanout: two 2 MiB slots, DMA/config in last page.
        cmdline+=' memrsv=0x00500000@0x41e100000:uc'
      fi
      if [[ "${MENU_MEDIA_SELFTEST:-0}" == 1 ]]; then
        cmdline+=' memrsv=0x00400000@0x41e600000:uc'
      fi
    fi
  fi
  # The camera's own cmdline carries driver parameters (pcie.*, wdt.*, adrs_conv.*, ...) that we
  # normally drop. MENU_EXTRA_CMDLINE lets a run pass them through verbatim.
  if [[ -n "${QEMU_EXTRA_CMDLINE:-}" ]]; then
    cmdline+=" $QEMU_EXTRA_CMDLINE"
  fi
  cmdline+=' initcall_debug initcall_blacklist=__boottime___udif_init_udif_hwtimer,__boottime___udif_init_udif_wdt,__boottime___udif_init_udif_fusb_otg,__boottime___udif_init_udif_pcie,__boottime___udif_init_udif_ddrc,__boottime___udif_init_pcidma,__boottime_sony_usb_init'
  if [[ "$high_ddr" == 1 ]]; then
    # GPIO now has register backing; let the original OTG notification state initialize.
    cmdline="${cmdline/,__boottime___udif_init_udif_fusb_otg/}"
  fi
  if [[ "$sony_timers" == 1 ]]; then
    cmdline="${cmdline/__boottime___udif_init_udif_hwtimer,/}"
  fi
  # This kernel uses the fixed-address DTB, so update that DTB's bootargs too.
  python3 - "$output_dir/virt.dtb" "$project_dir/sources/qemu-tools/usr/lib/libfdt.so.1" "$cmdline" "$test_initrd" "$high_ddr" <<'PY'
from pathlib import Path
import ctypes, re, struct, sys
native = 'memrsv=0x00500000@0x41e100000:uc'
media = 'memrsv=0x00400000@0x41e600000:uc'
if media in sys.argv[3].split():
    reservations = re.findall(r'(?:^|\s)memrsv=(0x[0-9a-fA-F]+)@(0x[0-9a-fA-F]+)(?=[:\s]|$)', sys.argv[3])
    assert sys.argv[3].split().count(media) == 1, 'Duplicate media export reservation'
    for length, start in reservations:
        start, length = int(start, 16), int(length, 16)
        if (start, length) == (0x41e600000, 0x400000):
            continue
        if start < 0x41ea00000 and start + length > 0x41e600000:
            raise RuntimeError('Reservation overlaps media export pool')
if native in sys.argv[3].split():
    # Reject overlaps from both kemco and caller-supplied extra reservations.
    reservations = re.findall(r'(?:^|\s)memrsv=(0x[0-9a-fA-F]+)@(0x[0-9a-fA-F]+)(?=[:\s]|$)', sys.argv[3])
    assert sys.argv[3].split().count(native) == 1, 'Duplicate ramfb reservation'
    for length, start in reservations:
        start, length = int(start, 16), int(length, 16)
        if (start, length) == (0x41e100000, 0x500000):
            continue
        if start < 0x41e600000 and start + length > 0x41e100000:
            raise RuntimeError('Reservation overlaps native ramfb pool')
path = Path(sys.argv[1])
fdt = ctypes.CDLL(str(Path(sys.argv[2]).resolve()))
source = ctypes.create_string_buffer(path.read_bytes())
buffer = ctypes.create_string_buffer(16384)
if fdt.fdt_open_into(source, buffer, len(buffer)) != 0:
    raise RuntimeError('Cannot expand board DTB')
chosen = fdt.fdt_path_offset(buffer, b'/chosen')
value = sys.argv[3].encode() + b'\0'
if chosen < 0 or fdt.fdt_setprop(buffer, chosen, b'bootargs', value, len(value)) != 0:
    raise RuntimeError('Cannot set board bootargs')
# Keep the fixed-address DTB's initrd size consistent with the selected test image.
fdt.fdt_getprop.restype = ctypes.c_void_p
length = ctypes.c_int()
ptr = fdt.fdt_getprop(buffer, chosen, b'linux,initrd-start', ctypes.byref(length))
if not ptr or length.value not in (4, 8):
    raise RuntimeError('Cannot read board initrd address')
start = int.from_bytes(ctypes.string_at(ptr, length.value), 'big')
if sys.argv[5] == '1':
    start = 0x440000000
    value = start.to_bytes(length.value, 'big')
    if fdt.fdt_setprop(buffer, chosen, b'linux,initrd-start', value, len(value)) != 0:
        raise RuntimeError('Cannot set high initrd address')
end = start + Path(sys.argv[4]).stat().st_size
value = end.to_bytes(length.value, 'big')
if fdt.fdt_setprop(buffer, chosen, b'linux,initrd-end', value, len(value)) != 0:
    raise RuntimeError('Cannot set board initrd end')
# Map the actual QEMU GIC distributor at the VA expected by Sony's IRQ helpers.
node = fdt.fdt_add_subnode(buffer, 0, b'sony-gic-alias')
if node < 0:
    raise RuntimeError('Cannot add GIC alias node')
props = {
    b'device_type': b'iomap\0',
    b'phys': struct.pack('>Q', 0x08000000),
    b'virt': struct.pack('>Q', 0xfffffffff13a1000),
    b'size': struct.pack('>Q', 0x1000),
    b'memtype': struct.pack('>I', 1),
}
for name, value in props.items():
    if fdt.fdt_setprop(buffer, node, name, value, len(value)) != 0:
        raise RuntimeError('Cannot set GIC alias property')
if sys.argv[5] == '1':
    # Minimal GPIO register backing, not a behavioral camera-device model.
    node = fdt.fdt_add_subnode(buffer, 0, b'sony-register-backing')
    if node < 0:
        raise RuntimeError('Cannot add register backing')
    for name, value in {
        b'device_type': b'iomap\0',
        b'phys': struct.pack('>Q', 0xf101d000),
        b'virt': struct.pack('>Q', 0xfffffffff101d000),
        b'size': struct.pack('>Q', 0x20000),
        b'memtype': struct.pack('>I', 1),
    }.items():
        if fdt.fdt_setprop(buffer, node, name, value, len(value)) != 0:
            raise RuntimeError('Cannot map register backing')
    node = fdt.fdt_add_subnode(buffer, 0, b'sony-scu-map')
    if node < 0:
        raise RuntimeError('Cannot add SCU register map')
    for name, value in {
        b'device_type': b'iomap\0', b'phys': struct.pack('>Q', 0xf1388000),
        b'virt': struct.pack('>Q', 0xfffffffff1388000),
        b'size': struct.pack('>Q', 0x2000), b'memtype': struct.pack('>I', 1),
    }.items():
        if fdt.fdt_setprop(buffer, node, name, value, len(value)) != 0:
            raise RuntimeError('Cannot set SCU register map')
    # Locate RAM by device_type, then move the declared DDR bank to Sony's base.
    offset = fdt.fdt_next_node(buffer, -1, None)
    while offset >= 0:
        n = ctypes.c_int()
        p = fdt.fdt_getprop(buffer, offset, b'device_type', ctypes.byref(n))
        if p and ctypes.string_at(p, n.value).rstrip(b'\0') == b'memory':
            # Extra test RAM beyond camera workspaces holds the in-memory /usr copy.
            ddr_size = 0x140000000 if sys.argv[4].endswith(('.cpio', '.cpio.gz')) else 0x100000000
            value = struct.pack('>QQ', 0x400000000, ddr_size)
            if fdt.fdt_setprop(buffer, offset, b'reg', value, len(value)) != 0:
                raise RuntimeError('Cannot relocate guest DDR bank')
            break
        offset = fdt.fdt_next_node(buffer, offset, None)
    else:
        raise RuntimeError('Cannot find guest DDR bank')
    # Protect original Backup storage and monitor work area from the guest allocator.
    fdt.fdt_add_mem_rsv.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64]
    for base, length in ((0x404000000, 0x400000), (0x405000000, 0x800000),
                         (0x410268000, 0x7c000)):
        if fdt.fdt_add_mem_rsv(buffer, base, length) != 0:
            raise RuntimeError('Cannot reserve Sony workspace')
if sys.argv[5] == '1':
    node = fdt.fdt_add_subnode(buffer, 0, b'sony-timer-map')
    if node < 0:
        raise RuntimeError('Cannot add timer mapping')
    for name, value in {
        b'device_type': b'iomap\0', b'phys': struct.pack('>Q', 0xf1042000),
        b'virt': struct.pack('>Q', 0xfffffffff1042000),
        b'size': struct.pack('>Q', 0x4000), b'memtype': struct.pack('>I', 1),
    }.items():
        if fdt.fdt_setprop(buffer, node, name, value, len(value)) != 0:
            raise RuntimeError('Cannot set timer map property')
# Reject overlapping static virtual mappings before booting the unmodified kernel.
mappings = []
offset = fdt.fdt_next_node(buffer, -1, None)
while offset >= 0:
    count = ctypes.c_int()
    p = fdt.fdt_getprop(buffer, offset, b'device_type', ctypes.byref(count))
    if p and ctypes.string_at(p, count.value).rstrip(b'\0') == b'iomap':
        values = []
        for field in (b'virt', b'size'):
            p = fdt.fdt_getprop(buffer, offset, field, ctypes.byref(count))
            if not p or count.value != 8:
                raise RuntimeError('Invalid iomap property')
            values.append(int.from_bytes(ctypes.string_at(p, 8), 'big'))
        mappings.append((values[0], values[0] + values[1]))
    offset = fdt.fdt_next_node(buffer, offset, None)
mappings.sort()
if any(left[1] > right[0] for left, right in zip(mappings, mappings[1:])):
    raise RuntimeError('Overlapping iomap virtual ranges')
if fdt.fdt_pack(buffer) != 0:
    raise RuntimeError('Cannot pack board DTB')
size = struct.unpack_from('>I', buffer.raw, 4)[0]
path.write_bytes(buffer.raw[:size])
PY
fi
if [[ "$high_ddr" == 1 ]]; then
  memory_config=24576
  loader_args=(-device loader,file=/out/virt.dtb,addr=0x400002000
               -device loader,file=/kernel.img,addr=0x400080000,force-raw=on
               -device loader,file=/initrd.img,addr=0x440000000,force-raw=on
               -device loader,addr=0x400080000,cpu-num=0)
fi
mkdir -p "$output_dir"
if [[ -n "${QEMU_OUTPUT_DIR:-}" ]]; then
  selected_output="$QEMU_OUTPUT_DIR"
  mkdir -p "$selected_output"
  install -m 0644 "$output_dir/virt.dtb" "$selected_output/virt.dtb"
  output_dir="$selected_output"
fi
if [[ "${QEMU_PREPARE_ONLY:-0}" == 1 ]]; then
  printf 'Prepared board DTB: %s\n' "$output_dir/virt.dtb"
  exit 0
fi
observation_seconds="${QEMU_TIMEOUT_SECONDS:-15}"
[[ "$observation_seconds" =~ ^[0-9]+$ ]] || { echo 'Invalid timeout' >&2; exit 2; }
# Exit 124 means the observation window elapsed; zero allows an interactive session.
runner=(timeout --signal=TERM --kill-after=2s "${observation_seconds}s")
# The UI owns this process tree; exec preserves ownership for stop and parent death.
if [[ "${QEMU_UI_LIVE:-0}" == 1 ]]; then runner=(exec); fi
"${runner[@]}" \
  bwrap --unshare-all --die-with-parent --new-session --clearenv \
    --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib /lib64 \
    --proc /proc --dev /dev --tmpfs /tmp \
    --ro-bind "$tool_root" /tools \
    --ro-bind "$bootfs/vmlinux.bin" /kernel.img \
    --ro-bind "$test_initrd" /initrd.img \
    --bind "$output_dir" /out \
    --chdir / --setenv LD_LIBRARY_PATH /tools/usr/lib --setenv LC_ALL C \
    "${timer_environment[@]}" \
    "${display_environment[@]}" \
    "/tools/$binary_relative" \
      -machine virt,secure=off -cpu cortex-a35 -smp 1 -m "$memory_config" \
      ${QEMU_ICOUNT_ARGS:-} \
      -nodefaults "${display_args[@]}" -monitor none -serial stdio -nic none \
      -no-reboot -sandbox on -qmp unix:/out/qmp.sock,server=on,wait=off \
      "${loader_args[@]}" -kernel /kernel.img -initrd /initrd.img \
      -append "$cmdline" \
      -d "${QEMU_TRACE_FLAGS:-in_asm,cpu_reset,guest_errors,unimp}" -D /out/boot-trace.log
