#!/bin/bash
set -Eeuo pipefail

log() {
  printf 'spike-init: %s\n' "$*" >/dev/console
}

mount_guest_filesystems() {
  mkdir -p /proc /sys /dev /dev/pts /run /state /state/workspaces
  mount -t proc -o nosuid,nodev,noexec proc /proc
  mount -t sysfs -o nosuid,nodev,noexec sysfs /sys
  if ! mountpoint -q /dev; then
    mount -t devtmpfs -o nosuid devtmpfs /dev
  fi
  mkdir -p /dev/pts
  mount -t devpts -o nosuid,noexec,mode=0620,gid=5 devpts /dev/pts
  mount -t tmpfs -o nosuid,nodev tmpfs /run

  mkdir -p /run/agentplane/environment /run/agentplane/config \
    /run/agentplane/sandbox /run/agentplane/trust \
    /run/agentplane/environment-runtime-private/environment-runtime-tls \
    /state/workspaces
  mount -t erofs -o ro,nosuid,nodev /dev/vdb /run/agentplane/environment
  mount -t ext4 -o rw,nosuid,nodev /dev/vdc /state
  mkdir -p /state/workspaces
  mount -t ext4 -o rw,nosuid,nodev /dev/vdd /state/workspaces
  mount -t iso9660 -o ro,nosuid,nodev /dev/vde /run/agentplane/config
  mount -t iso9660 -o ro,nosuid,nodev /dev/vdf /run/agentplane/sandbox
  mount -t iso9660 -o ro,nosuid,nodev /dev/vdg /run/agentplane/trust
  mount -t ext4 -o ro,nosuid,nodev,noexec /dev/vdh \
    /run/agentplane/environment-runtime-private/environment-runtime-tls

  ln -sfn /run/agentplane/environment-runtime-private/environment-runtime-tls \
    /run/agentplane/environment-runtime-tls
  ln -sfn /run/agentplane/trust /run/agentplane/environment-runtime-trust
}

configure_network() {
  ip link set lo up
  ip link set eth0 mtu 1280
  ip link set eth0 up
  ip address replace 192.0.2.2/30 dev eth0
  ip -6 address replace fd00:fc::2/64 dev eth0 nodad
  ip route replace default via 192.0.2.1 dev eth0
  ip -6 route replace default via fd00:fc::1 dev eth0
}

shutdown_guest() {
  local signal=$1
  trap - TERM INT
  if [[ -n ${guest_pid:-} ]]; then
    kill -"$signal" "$guest_pid" 2>/dev/null || true
  fi
}

mount_guest_filesystems
configure_network

if [[ ! -x /run/agentplane/environment/bin/agentplane-environment-runtime ]]; then
  log 'runner disk has no executable environment runtime'
  exit 1
fi
log 'HELLO WORLD: real Agentplane runner disk mounted'

if [[ ! -f /opt/spike/guest.py ]]; then
  log 'missing /opt/spike/guest.py'
  exit 1
fi

log 'starting standalone guest server'
python3 /opt/spike/guest.py &
guest_pid=$!
trap 'shutdown_guest TERM' TERM
trap 'shutdown_guest INT' INT

# PID 1 reaps orphaned guest children while retaining the server exit status.
while true; do
  completed_pid=
  if wait -n -p completed_pid; then
    wait_status=0
  else
    wait_status=$?
  fi
  if [[ ${completed_pid:-} == "$guest_pid" ]]; then
    log "guest server exited with status $wait_status"
    exit "$wait_status"
  fi
done
