#!/bin/bash
# Run the menu probe with the standard flag set, waiting for the probe lock instead of failing on it.
#
# By default this rebuilds menu-observe.so and the initramfs first: forgetting the rebuild silently runs
# the previous observer and makes experiments look like clean negatives. Set SKIP_REBUILD=1 to skip.
# Any MENU_* environment variables exported by the caller are forwarded to the guest as before.
set -euo pipefail
analysis_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$analysis_dir"
LOCK="$analysis_dir/.menu-probe.lock"
exec 9>>"$LOCK"
flock 9
export MENU_PROBE_LOCK_FD=9

if [ -f "$analysis_dir/native-assets.json" ]; then
    runtime_dir="$(cd -- "$analysis_dir/.." && pwd)"
    python3 "$analysis_dir/prepare-assets.py" --runtime "$runtime_dir" --check-runtime
fi

if [ "${SKIP_REBUILD:-0}" != "1" ]; then
    bash "$analysis_dir/build-observers.sh" >/dev/null || { echo "observer build failed"; exit 1; }
    bash "$analysis_dir/prepare-menu-initramfs.sh" >/dev/null || { echo "initramfs build failed"; exit 1; }
    echo "rebuilt observer + initramfs"
fi

exec python3 "$analysis_dir/qemu-menu-probe.py" \
    --initramfs --sony-timers --load-ipc --seed-backup --load-dmm --load-power --load-kikilog \
    --no-cp-updater --capid-service --sa-address-service --hdmi-format-service --hdmi-photo-service \
    --dmm-service --software-gpe --firmware-identity --display-info-service --seed-power-on \
    --menu-target --bootin --observe
