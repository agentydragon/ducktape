#!/usr/bin/env bash
# seat-diag3: inject session c2's (seatphysical greeter) env into the
# plasmalogin user manager, then start the greeter units aimed at it.
set -x
exec >/home/agentydragon/seat-diag3.out 2>&1

PLMUID=989
asplm() {
  sudo -u plasmalogin XDG_RUNTIME_DIR=/run/user/$PLMUID \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$PLMUID/bus "$@"
}

echo "########## 0. session c2 still alive + active?"
loginctl show-session c2 -p Name -p Seat -p State -p Active -p Class -p Type

echo "########## 1. retarget user-manager env at session c2 / seatphysical"
# Values from /proc/2898/environ (seatphysical startplasma, session c2).
asplm systemctl --user set-environment \
  XDG_SEAT=seatphysical \
  XDG_SESSION_ID=c2 \
  XDG_SESSION_CLASS=greeter \
  XDG_SESSION_TYPE=wayland \
  XDG_SEAT_PATH=/org/freedesktop/DisplayManager/Seatphysical \
  XDG_SESSION_PATH=/org/freedesktop/DisplayManager/Session1 \
  SDDM_SOCKET=/tmp/plasmalogin--ToRhly
asplm systemctl --user unset-environment XDG_VTNR
asplm systemctl --user show-environment | grep -E 'XDG_SEAT|XDG_SESSION|XDG_VTNR|SDDM_SOCKET'

echo "########## 2. reset-failed + start"
asplm systemctl --user reset-failed
asplm systemctl --user start plasma-login-wayland.target
sleep 8

echo "########## 3. result"
asplm systemctl --user list-units --all 'plasma*'
KPID=$(pgrep -u plasmalogin kwin_wayland | head -1)
echo "kwin pid: ${KPID:-none}"
echo "== card0 clients"
cat /sys/kernel/debug/dri/0/clients
echo "== kwin journal"
journalctl -b _UID=$PLMUID --since '-2 min' --no-pager | grep -Ei 'kwin|drm|card|output|greeter|session' | tail -40
echo "########## done — is the physical monitor lit?"
