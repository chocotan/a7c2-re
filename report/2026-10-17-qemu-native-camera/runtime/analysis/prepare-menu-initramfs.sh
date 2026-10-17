#!/usr/bin/env bash
# Separate raw cpio test root; the original kernel has no gzip initrd support.
set -euo pipefail
analysis_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$analysis_dir/menu-root"
boot="$analysis_dir/unpacked/firmware.tar_unpacked/0700_part_image/dev/nflasha3_unpacked"
output="$analysis_dir/menu-initramfs.cpio"
exclude_updater=()
if [[ "${1:-}" == --without-updater ]]; then
  output="$analysis_dir/menu-initramfs-no-updater.cpio"
  exclude_updater=(-path './usr/lib/libupdaterufp.so' -prune -o)
elif [[ $# != 0 ]]; then
  echo 'Usage: prepare-menu-initramfs.sh [--without-updater]' >&2
  exit 2
fi
for file in "$boot/"*.bin "$boot/"*.hcd; do cp -a "$file" "$root/system/"; done
# im.elf loads IMDB from a fixed /usr/lib path; only this generated test root uses the qemu-filtered copy.
chmod u+w "$root/usr/lib/libIMDB.so"
cp "$analysis_dir/libIMDB-menu.so" "$root/usr/lib/libIMDB.so"
chmod 0555 "$root/usr/lib/libIMDB.so"
mkdir -p "$root/simulation" "$root/seed-setting"
cp -f "$analysis_dir/libIMDB-menu.so" "$root/simulation/libIMDB.so"
cp "$analysis_dir/menu-observe.so" "$root/simulation/menu-observe.so"
# The real camera Backup.bin and /setting contents are kept beside the research notes. When they
# are present the guest starts from the device own persistent state instead of the update-package
# defaults; set SEED_BACKUP=alllang to force the package default back.
real_setting="$analysis_dir/../../qemu-boot-research/updater-integration/real-setting"
if [ -f "$analysis_dir/native-assets.json" ]; then
  # The installed runtime has one validated seed; never blend in research settings.
  echo 'seed: using verified installed ALLLANG assets'
elif [ "${SEED_BACKUP:-real}" = real ] && [ -f "$real_setting/Backup.bin" ]; then
  cp -a "$analysis_dir/unpacked/firmware.tar_unpacked/0800_appli/setting/." "$root/seed-setting/"
  cp -f "$real_setting/Backup.bin" "$root/seed-backup.bin"
  for extra in defdet.bin psd.bin Backup.bak .hasp .etools ulogio.bin facedmy.bin kemco.txt DmmCfg.bin cxd90057.dtb; do
    [ -f "$real_setting/$extra" ] && cp -f "$real_setting/$extra" "$root/seed-setting/$extra"
  done
  mkdir -p "$root/seed-setting/mode" "$root/seed-setting/appfw_infra" "$root/seed-setting/gps" "$root/seed-setting/updater"
  # -f: the real updater files are 0444, so a plain -a cannot overwrite a previous copy
  [ -d "$real_setting/updater" ] && cp -af "$real_setting/updater/." "$root/seed-setting/updater/" 2>/dev/null || true
  echo "seed: using the real device /setting and Backup"
else
  cp -a "$analysis_dir/unpacked/firmware.tar_unpacked/0800_appli/setting/." "$root/seed-setting/"
  cp "$analysis_dir/unpacked/firmware.tar_unpacked/0110_backup/SYSIPSX-DSLR/PP/CX89701_ALLLANG.bin" "$root/seed-backup.bin"
  echo "seed: using the update package ALLLANG Backup"
fi
printf '%s\n' '#!/bin/busybox sh' \
  '/bin/busybox mount -t devtmpfs devtmpfs /dev' \
  'exec /bin/busybox sh </dev/ttyAMA0 >/dev/ttyAMA0 2>&1' > "$root/init"
chmod 0755 "$root/init"
# PRELOAD_ALL=1 puts the observer into every dynamically linked guest process through
# /etc/ld.so.preload, so senders and queue creators outside im.elf become visible.
mkdir -p "$root/etc"
if [ "${PRELOAD_ALL:-0}" = 1 ]; then
  printf '/simulation/menu-observe.so\n' > "$root/etc/ld.so.preload"
  echo "preload: observer injected into every guest process"
else
  rm -f "$root/etc/ld.so.preload"
fi
# ponytail: speech dictionaries are excluded from this menu-only test image;
# include them if screen-reader behavior becomes part of the experiment.
pad_bytes="${CPIO_PAD_BYTES:-0}"
if [ "$pad_bytes" != "0" ]; then
  head -c "$pad_bytes" /dev/zero > "$root/simulation/.pad"
else
  rm -f "$root/simulation/.pad"
fi
(cd "$root" && find . -path './usr/share/screenreader' -prune -o "${exclude_updater[@]}" -print0 |
  cpio --null -o -H newc --quiet) > "$output"
printf 'Prepared raw cpio menu test root (screen-reader speech files excluded).\n'
ls -lh "$output"
