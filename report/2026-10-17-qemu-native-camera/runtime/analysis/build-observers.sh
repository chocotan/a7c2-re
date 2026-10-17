#!/usr/bin/env bash
# Build guest helpers against the original firmware ABI, including its loader guard.
set -euo pipefail
analysis_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$analysis_dir/menu-root/simulation"
install -m 0755 "$analysis_dir/guest-overlay/simulation/diag.sh" "$analysis_dir/menu-root/simulation/diag.sh"
install -m 0755 "$analysis_dir/guest-overlay/simulation/input-state.sh" "$analysis_dir/menu-root/simulation/input-state.sh"
lib="$analysis_dir/menu-root/lib"
common=(--target=aarch64-linux-gnu -fuse-ld=lld -nostdlib -O2 -mno-outline-atomics -Wl,--no-undefined)
# ponytail: -mno-outline-atomics keeps __aarch64_ldadd4_relax/__clear_cache libcalls out;
# the guest root only ships the firmware's own libc/libgcc, which have no outline-atomics entry points.
libraries=("$lib/libc-2.25.so" "$lib/libdl-2.25.so" "$lib/libpthread-2.25.so" "$lib/ld-2.25.so")
clang "${common[@]}" -shared -fPIC "$analysis_dir/mutex-trace.c" "${libraries[@]}" \
  -o "$analysis_dir/menu-root/simulation/mutex-trace.so"
if [[ "${MENU_MEDIA_SELFTEST:-0}" == 1 ]]; then
  clang "${common[@]}" -shared -fPIC "$analysis_dir/file-media-config.c" "${libraries[@]}" \
    -o "$analysis_dir/menu-root/simulation/file-media-config.so"
  clang "${common[@]}" --sysroot=/usr/aarch64-linux-gnu -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
    "$analysis_dir/fsys-file-test.c" -Wl,--no-as-needed \
    "$analysis_dir/menu-root/usr/lib/libInfraFsys.so" "$analysis_dir/menu-root/usr/lib/libInfraMediaCommon.so" \
    "$analysis_dir/menu-root/usr/lib/libMWF.so" \
    "$analysis_dir/menu-root/usr/lib/libbackup.so" "$analysis_dir/menu-root/usr/lib/libosal_uipc.so" \
    "$analysis_dir/menu-root/usr/lib/libosal_utm.so" -Wl,--allow-shlib-undefined "${libraries[@]}" \
    -o "$analysis_dir/menu-root/simulation/fsys-file-test"
  python3 "$analysis_dir/make-media-fixture.py"
fi
clang "${common[@]}" --sysroot=/usr/aarch64-linux-gnu -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/ramfb-viewer.c" "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/ramfb-viewer"
clang "${common[@]}" -shared -fPIC -Wl,-soname,menu-observe.so \
  "$analysis_dir/menu-observe.c" "$analysis_dir/capid-service.c" "$analysis_dir/sa-address-service.c" \
  "$analysis_dir/hdmi-format-service.c" "$analysis_dir/dmm-service.c" "$analysis_dir/gpe-raster.c" "$analysis_dir/gpe-sprite.c" "$analysis_dir/sony-lz77.c" "$analysis_dir/headless-osd.c" "${libraries[@]}" -o "$analysis_dir/menu-observe.so"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/headless-osd-export.c" "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/headless-osd-export"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/menu-readmem.c" "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/menu-readmem"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/menu-regs.c" "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/menu-regs"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/input-table.c" "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/input-table"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/menu-scanmem.c" "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/menu-scanmem"
clang "${common[@]}" -shared -fPIC -DTEST_LIBRARY \
  "$analysis_dir/check-menu-observe.c" "${libraries[@]}" -o "$analysis_dir/menu-root/lib/observe-constructor.so"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/check-menu-observe.c" "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/check-menu-observe"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/seed-date-clock.c" -Wl,--no-as-needed \
  "$analysis_dir/menu-root/usr/lib/libosal_uipc.so" "$analysis_dir/menu-root/usr/lib/libosal_utm.so" \
  -Wl,--allow-shlib-undefined "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/seed-date-clock"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/seed-power-on.c" "$analysis_dir/menu-root/usr/lib/libbackup.so" \
  -Wl,--no-as-needed "$analysis_dir/menu-root/usr/lib/libosal_uipc.so" \
  "$analysis_dir/menu-root/usr/lib/libosal_utm.so" \
  -Wl,--allow-shlib-undefined "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/seed-power-on"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/seed-indicator-config.c" "$analysis_dir/menu-root/usr/lib/libbackup.so" \
  -Wl,--no-as-needed "$analysis_dir/menu-root/usr/lib/libosal_uipc.so" \
  "$analysis_dir/menu-root/usr/lib/libosal_utm.so" \
  -Wl,--allow-shlib-undefined "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/seed-indicator-config"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/inject-key.c" \
  -Wl,--no-as-needed "$analysis_dir/menu-root/usr/lib/libosal_uipc.so" \
  "$analysis_dir/menu-root/usr/lib/libosal_utm.so" \
  -Wl,--allow-shlib-undefined "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/inject-key"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/inject-cp-event.c" \
  -Wl,--no-as-needed "$analysis_dir/menu-root/usr/lib/libosal_uipc.so" \
  "$analysis_dir/menu-root/usr/lib/libosal_utm.so" \
  -Wl,--allow-shlib-undefined "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/inject-cp-event"
clang "${common[@]}" -Wl,-e,_start -Wl,--dynamic-linker=/lib/ld-linux-aarch64.so.1 \
  "$analysis_dir/backup-dump.c" "$analysis_dir/menu-root/usr/lib/libbackup.so" \
  -Wl,--no-as-needed "$analysis_dir/menu-root/usr/lib/libosal_uipc.so" \
  "$analysis_dir/menu-root/usr/lib/libosal_utm.so" \
  -Wl,--allow-shlib-undefined "${libraries[@]}" -o "$analysis_dir/menu-root/simulation/backup-dump"
