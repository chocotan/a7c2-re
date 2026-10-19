#!/bin/bash
# Start/stop the research sshd on an ILCE-7CM2 (firmware 2.01).
#
# Prerequisite: USB Connection set to Mass Storage, camera volumes unmounted,
# camera at 054c:0e8a. Each run consumes one USB session, so the camera must be
# re-plugged before every invocation (it ends up at 054c:096f).
#
# The sshd instance uses its own config on tmpfs (/tmp_network/a7c2ssh), port
# 2222, key-only auth, UsePAM off. The vendor /etc/ssh/sshd_config and port 22
# are never touched.
set -u
cd /home/choco/Downloads/20261009sony7c2 || exit 1
HERE=final/a7c2-re/report/2026-10-19-ssh-feasibility
PY=/home/choco/Downloads/20261009sony7c2/analysis/.venv/bin/python
SCRIPTS=final/a7c2-re/report/2026-10-18-libjit-exec/scripts
KEY="$HERE/keys/id_ed25519"
HOST=192.168.31.194
PUB=$(cat "$HERE/keys/id_ed25519.pub")

wait_device() {
  echo "waiting for 054c:0e8a (re-plug the camera) ..."
  for _ in $(seq 1 150); do
    lsusb | grep -q '054c:0e8a' && { echo "device present"; return 0; }
    sleep 3
  done
  echo "NOT_MASS_STORAGE_ABORT"; lsusb | grep -i 054c; return 1
}

unmount_volumes() {
  for m in /run/media/choco/A7C2CAM /run/media/choco/PMHOME; do
    mountpoint -q "$m" && sudo udisksctl unmount -b "$(findmnt -n -o SOURCE --target "$m")"
  done
  lsblk -o NAME,LABEL,MOUNTPOINT | grep -iE 'A7C2|PMHOME' || echo "UNMOUNTED_OK"
}

run_guest() {  # $1 = shell command, $2 = evidence tag
  cd "$SCRIPTS" || exit 1
  sudo "$PY" libjit_exec.py run --keep "$HERE/evidence/$(date +%F)-$2" \
    --max-output 65536 --command "$1" 2>&1 | grep -v '"event": "jiritsu_' | tail -40
  cd - >/dev/null || exit 1
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
  wait_device || exit 1
  unmount_volumes
  run_guest "${GUEST_START/__PUBKEY__/$PUB}" start
  sleep 2
  echo "=== login probe ==="
  timeout 25 ssh -i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o IdentitiesOnly=yes -o BatchMode=yes -o LogLevel=ERROR -o ConnectTimeout=8 \
    -p 2222 root@"$HOST" 'cat /proc/$$/status | grep -E "^(Uid|Gid):"' && echo "SSH_OK"
}

cmd_stop() {
  wait_device || exit 1
  unmount_volumes
  run_guest "$GUEST_STOP" stop
}

case "${1:-}" in
  start) cmd_start ;;
  stop)  cmd_stop ;;
  *) echo "usage: $0 {start|stop}"; exit 2 ;;
esac
