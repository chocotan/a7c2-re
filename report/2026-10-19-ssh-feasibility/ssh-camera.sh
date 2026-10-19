#!/bin/bash
# Start/stop a research sshd on an ILCE-7CM2 (firmware tested: 2.01).
#
# Generic: no host-specific paths, IPs or keys are baked in.
#   A7C2_HOST       camera address on the LAN            (required)
#   A7C2_SSH_KEY    client key path      (default ~/.ssh/a7c2-camera)
#   A7C2_PYTHON     python with pyusb+Cryptodome         (default: repo venv)
#   A7C2_PMCA       vendored PMCA-RE checkout            (default: repo vendor)
#
# A key pair is generated on first use if A7C2_SSH_KEY does not exist; only the
# public half ever reaches the camera.
#
# Prerequisites: USB Connection set to Mass Storage, camera volumes unmounted,
# camera at 054c:0e8a. Each run consumes one USB session, so re-plug the camera
# before every invocation (it ends up at 054c:096f).
#
# The instance uses its own config on tmpfs (/tmp_network/a7c2ssh), port 2222,
# key-only auth, UsePAM off. The vendor /etc/ssh/sshd_config and port 22 are
# never touched. It writes no persistent state; /etc/passwd is NOT modified here
# (see REPORT.md for why root's login shell must already be /bin/sh).
set -u

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(cd -- "$HERE/../../../.." && pwd)
LIBJIT="$ROOT/final/a7c2-re/report/2026-10-18-libjit-exec/scripts"
KEY=${A7C2_SSH_KEY:-$HOME/.ssh/a7c2-camera}
PORT=2222

die() { echo "$*" >&2; exit 1; }

resolve_python() {
  if [ -n "${A7C2_PYTHON:-}" ]; then echo "$A7C2_PYTHON"; return; fi
  if [ -x "$ROOT/analysis/.venv/bin/python" ]; then echo "$ROOT/analysis/.venv/bin/python"; return; fi
  command -v python3 || die "no python3; set A7C2_PYTHON"
}

ensure_key() {
  if [ ! -f "$KEY" ]; then
    mkdir -p "$(dirname -- "$KEY")"
    ssh-keygen -t ed25519 -N '' -C "a7c2-camera" -f "$KEY" >/dev/null || die "ssh-keygen failed"
    echo "generated client key: $KEY" >&2
  fi
  PUB=$(cat "$KEY.pub")
}

wait_device() {
  echo "waiting for 054c:0e8a (re-plug the camera) ..."
  for _ in $(seq 1 150); do
    lsusb | grep -q '054c:0e8a' && { echo "device present"; return 0; }
    sleep 3
  done
  die "camera is not in mass-storage mode: $(lsusb | grep -i 054c || echo 'no Sony USB device')"
}

unmount_volumes() {
  local source mountpoint
  while read -r mountpoint; do
    [ -n "$mountpoint" ] || continue
    source=$(findmnt -n -o SOURCE --target "$mountpoint") || continue
    sudo udisksctl unmount -b "$source" || echo "UNMOUNT_FAIL $mountpoint" >&2
  done < <(lsblk -o MOUNTPOINT,LABEL --noheadings 2>/dev/null |
           awk '$2 ~ /^(A7C2CAM|PMHOME)$/ {print $1}')
  lsblk -o NAME,LABEL,MOUNTPOINT --noheadings 2>/dev/null |
    awk '$2 ~ /^(A7C2CAM|PMHOME)$/ && $3 != "" {print "still mounted: "$0}' || true
}

run_guest() {  # $1 = guest shell command, $2 = evidence tag
  ( cd "$LIBJIT" && sudo "$(resolve_python)" libjit_exec.py run \
      --keep "$HERE/evidence/$(date +%F)-$2" --max-output 65536 --command "$1" ) \
    2>&1 | grep -v '"event": "jiritsu_' | tail -40
}

read -r -d '' GUEST_START <<'EOS'
mkdir -p /var/empty; chmod 755 /var/empty
D=/tmp_network/a7c2ssh
[ -d $D ] || { mkdir -p $D; chmod 700 $D; }
[ -f $D/host_ecdsa ] || { /usr/bin/ssh-keygen -t ecdsa -b 256 -N "" -f $D/host_ecdsa; chmod 600 $D/host_ecdsa; }
echo "__PUBKEY__" > $D/authorized_keys; chmod 600 $D/authorized_keys
cat > $D/sshd_config <<EOF
Port 2222
AddressFamily inet
ListenAddress 0.0.0.0
HostKey $D/host_ecdsa
PermitRootLogin yes
PubkeyAuthentication yes
PasswordAuthentication no
AuthorizedKeysFile $D/authorized_keys
UsePAM no
PidFile $D/sshd.pid
StrictModes no
MaxSessions 4
MaxAuthTries 3
LoginGraceTime 30
EOF
/usr/sbin/sshd -t -f $D/sshd_config; echo TEST_RC=$?
/usr/sbin/sshd -f $D/sshd_config; echo START_RC=$?
sleep 2
cat /proc/net/tcp | grep -i "08AE"
EOS

read -r -d '' GUEST_STOP <<'EOS'
D=/tmp_network/a7c2ssh
[ -f $D/sshd.pid ] && kill $(cat $D/sshd.pid) 2>/dev/null
pkill -f "sshd -f $D/sshd_config" 2>/dev/null
sleep 2
echo REMAINING_LISTENERS=$(cat /proc/net/tcp | grep -ci "08AE")
rm -rf $D /tmp/jiritsu/ssh
rmdir /var/empty 2>/dev/null
ls -d $D /var/empty 2>&1
EOS

cmd_start() {
  wait_device
  unmount_volumes
  ensure_key
  run_guest "${GUEST_START/__PUBKEY__/$PUB}" start
  echo "=== login probe ==="
  sleep 2
  ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR \
      -o ConnectTimeout=8 -p "$PORT" root@"$HOST" \
      'cat /proc/$$/status | grep -E "^(Uid|Gid):"' && echo "SSH_OK"
}

cmd_stop() {
  wait_device
  unmount_volumes
  run_guest "$GUEST_STOP" stop
}

HOST=${A7C2_HOST:-}
while [ $# -gt 0 ]; do
  case "$1" in
    -H|--host) HOST=${2:-}; shift 2 ;;
    start|stop) ACTION=$1; shift ;;
    *) die "usage: A7C2_HOST=<camera-ip> $0 {start|stop}" ;;
  esac
done

[ -n "${ACTION:-}" ] || die "usage: A7C2_HOST=<camera-ip> $0 {start|stop}"
[ -n "$HOST" ] || die "set A7C2_HOST=<camera-ip> (or pass -H <ip>)"
[ -d "$LIBJIT" ] || die "libJit deliverable not found: $LIBJIT"

case "$ACTION" in
  start) cmd_start ;;
  stop)  cmd_stop ;;
esac
