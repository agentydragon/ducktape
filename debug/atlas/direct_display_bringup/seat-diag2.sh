#!/usr/bin/env bash
# seat-diag2: confirm PLM single-greeter collision + try reviving the
# seatphysical greeter kwin now that seat0's greeter is gone.
set -x
exec >/home/agentydragon/seat-diag2.out 2>&1

PLMUID=989
asplm() {
  sudo -u plasmalogin XDG_RUNTIME_DIR=/run/user/$PLMUID \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$PLMUID/bus "$@"
}

echo "########## 1. snapshot before"
loginctl list-sessions
asplm systemctl --user show-environment
asplm systemctl --user list-units --all 'plasma*'
echo "----- environ of surviving startplasma (2898, session c2)"
tr '\0' '\n' </proc/2898/environ
echo "----- DRM clients"
for d in 0 1 2; do
  echo "== card$d"
  cat /sys/kernel/debug/dri/$d/clients 2>/dev/null
done

echo "########## 2. start greeter target"
asplm systemctl --user start plasma-login-wayland.target
sleep 6

echo "########## 3. snapshot after"
asplm systemctl --user list-units --all 'plasma*'
ps aux | grep -E 'kwin|plasma-login-greeter|plasma-login-wallpaper' | grep -v grep
echo "----- kwin cgroup + environ"
KPID=$(pgrep -u plasmalogin kwin_wayland | head -1)
if [ -n "$KPID" ]; then
  cat /proc/$KPID/cgroup
  tr '\0' '\n' </proc/$KPID/environ | grep -E 'XDG_|WAYLAND|SEAT|SESSION|VT'
fi
echo "----- DRM clients after"
for d in 0 1 2; do
  echo "== card$d"
  cat /sys/kernel/debug/dri/$d/clients 2>/dev/null
done
echo "----- fresh kwin journal"
journalctl -b _UID=$PLMUID --since '-2 min' --no-pager | grep -Ei 'kwin|drm|output|greeter' | tail -40
echo "########## done — is the physical monitor lit?"
