"""Install the pre-brick safety net: card-resident rescue script + an /etc/profile hook.

The premise matters: this must be installed **while the camera is healthy**. Once the
device is in a state from which our session channel cannot authenticate, nothing can be
installed any more.

What it can and cannot save (measured, see RECOVERY-AND-LESSONS.md):

* `/bin/busybox` is **dynamically linked** and `/bin/sh` is a symlink to it, so a *fatally*
  broken preload library — undefined symbols, failed relocations — prevents busybox, hence
  `/sbin/init`, from starting. The hook then never runs. That case can only be prevented
  (never leave an unverified library armed), not rescued.
* Everything short of that is rescuable: an entry naming a missing library, garbage in
  `/etc/ld.so.preload`, a full writable partition, or leftover files. The hook mounts the
  card, the card's script neutralises the preload and restores any file the manifest names.

All writes here go through the real shell (the command channel), not the firmware file
API, so normal filesystem semantics apply and nothing is limited by that layer's quirks.
"""
BACKUP_DIR = "/tmp/sd/a7c2-backup"
RESCUE_PATH = "/tmp/sd/a7c2-rescue.sh"
MANIFEST = f"{BACKUP_DIR}/MANIFEST"
CARD_DEVICE = "/dev/mmca2"
CARD_POINT = "/tmp/sd"

# Files worth restoring from the card. `/etc/ld.so.preload` is deliberately NOT restorable:
# the un-brick action is to make it contribute no libraries, and copying a previously armed
# entry back would re-break the camera. Its content is still saved for reference.
PRESERVE = ("/tmp/jiritsu/a7c2-payload.so", "/etc/profile")
REFERENCE_ONLY = ("/etc/ld.so.preload",)


def rescue_script_text():
    """The script that lives on the card and runs as root at every login shell."""
    return f"""#!/bin/sh
# A7C2 rescue. Runs as root from /etc/profile on every boot while the card carries this
# file at {RESCUE_PATH}. Idempotent. The log is written onto the card so the result can be
# read even if the camera's own storage is in a bad state.
BK={BACKUP_DIR}
LOG={CARD_POINT}/a7c2-rescue.log
{{
  echo "=== rescue $(busybox date 2>/dev/null || echo unknown) ==="
  # 1) the loader preload must contribute no libraries: this is the un-brick action
  : > /etc/ld.so.preload
  echo "neutralised /etc/ld.so.preload"
  # 2) restore every file the manifest names (payload, profile, ...)
  if [ -f "$BK"/MANIFEST ]; then
    while read -r target cardfile; do
      [ -n "$target" ] || continue
      if [ -f "$BK/$cardfile" ]; then
        cp "$BK/$cardfile" "$target" && echo "restored $target"
      else
        echo "missing on card: $cardfile"
      fi
    done < "$BK"/MANIFEST
  else
    echo "no manifest on card: only the preload was neutralised"
  fi
  sync
  echo "=== done ==="
}} >> "$LOG" 2>&1
"""


def hook_text():
    """The /etc/profile addition: mount the card, run the rescue script if present."""
    return f"""# a7c2 rescue hook (installed by the device owner while the camera was healthy).
# Mounts the SD card and runs its rescue script if one is there; does nothing otherwise,
# so a normal boot without a card is unaffected.
if [ -b {CARD_DEVICE} ]; then
  [ -d {CARD_POINT} ] || mkdir -p {CARD_POINT}
  mount -t ext2 {CARD_DEVICE} {CARD_POINT} 2>/dev/null
  [ -x {RESCUE_PATH} ] && {RESCUE_PATH}
fi
"""


def _flat_name(camera_path):
    return "file-" + camera_path.strip("/").replace("/", "_")


def installer_lines(preserve=PRESERVE, reference_only=REFERENCE_ONLY,
                    mount_device=CARD_DEVICE, mount_point=CARD_POINT):
    """Shell lines that install the safety net; printed here so they can be reviewed."""
    lines = [
        f"mkdir -p {mount_point}",
        f"mount -t ext2 {mount_device} {mount_point} 2>&1; echo CARD_MOUNT_RC=$?",
        f"mkdir -p {BACKUP_DIR}",
        f": > {MANIFEST}",
    ]
    for camera_path in preserve:
        lines += [
            f"if [ -f {camera_path} ]; then cp {camera_path} {BACKUP_DIR}/{_flat_name(camera_path)} "
            f"&& echo \"{camera_path} {_flat_name(camera_path)}\" >> {MANIFEST} "
            f"&& echo \"saved {camera_path}\"; else echo \"skip {camera_path} (absent)\"; fi",
        ]
    for camera_path in reference_only:
        lines += [
            f"if [ -f {camera_path} ]; then cp {camera_path} "
            f"{BACKUP_DIR}/{_flat_name(camera_path)}.saved && echo \"saved {camera_path} "
            f"(reference only, never restored)\"; fi",
        ]
    lines += [
        f"cat > {RESCUE_PATH} <<'A7C2RESCUE'",
        *rescue_script_text().splitlines(),
        "A7C2RESCUE",
        f"chmod +x {RESCUE_PATH}; echo RESCUE_RC=$?",
        "cat > /etc/profile <<'A7C2HOOK'",
        *hook_text().splitlines(),
        "A7C2HOOK",
        "echo PROFILE_WRITTEN",
        "sync",
        f"echo '--- card backup ---'; ls -l {BACKUP_DIR} 2>&1",
        f"echo '--- manifest ---'; cat {MANIFEST} 2>&1",
        f"echo '--- rescue script head ---'; busybox head -3 {RESCUE_PATH} 2>&1 || busybox dd if={RESCUE_PATH} bs=1 count=120 2>/dev/null",
        "echo '--- /etc/profile ---'; cat /etc/profile 2>&1",
    ]
    return lines
