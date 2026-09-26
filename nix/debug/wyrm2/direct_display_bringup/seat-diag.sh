#!/usr/bin/env bash
# Multi-seat display bring-up diagnostics (DM-agnostic).
#   run:  sudo bash seat-diag.sh
# Answers "why is the non-seat0 greeter/session black": DRM node→seat map, who
# holds DRM master on each card, logind seats/sessions, and the greeter's
# compositor journal. Works for any DM — the greeter user + compositor are
# auto-detected from logind's greeter-class sessions (gdm, plasmalogin, sddm, …).
# Writes to ./seat-diag.out (world-readable) + stdout.
out="$(dirname "$(readlink -f "$0")")/seat-diag.out"
exec > >(tee "$out") 2>&1
sec() { printf '\n========== %s ==========\n' "$1"; }

# Greeter session(s) and their user, from logind (not DM-specific).
greeter_sessions=$(loginctl list-sessions --no-legend 2>/dev/null | awk '$0 ~ /greeter/ {print $1}')
greeter_uids=$(for s in $greeter_sessions; do loginctl show-session "$s" -p User --value 2>/dev/null; done | sort -u)

sec "date / kernel / cmdline"
date
uname -a
echo
cat /proc/cmdline

sec "nvidia-drm parameters (modeset + fbdev) + loaded modules"
for p in modeset fbdev; do
  f=/sys/module/nvidia_drm/parameters/$p
  [ -e "$f" ] && echo "$p = $(cat "$f")" || echo "$p = (absent)"
done
echo
lsmod | grep -iE '^nvidia' || echo "(no nvidia modules)"

sec "modprobe.d nvidia config (does it set fbdev/modeset?)"
grep -rInE 'nvidia' /etc/modprobe.d/ 2>/dev/null || echo "(none)"

sec "DRM node -> PCI / driver / seat map"
for c in /sys/class/drm/card[0-9]*; do
  n=$(basename "$c")
  [[ "$n" == *-* ]] && continue # skip connector subdirs (card0-DP-1), keep card nodes
  [ -e "$c/device" ] || continue
  pci=$(basename "$(readlink -f "$c/device")")
  drv=$(basename "$(readlink -f "$c/device/driver" 2>/dev/null)" 2>/dev/null)
  seat=$(udevadm info -q property "/dev/dri/$n" 2>/dev/null | grep -oE 'ID_SEAT=[^ ]*' || echo ID_SEAT=seat0)
  echo "$n  pci=$pci  driver=$drv  $seat"
done

sec "connector status (which output has a monitor)"
for s in /sys/class/drm/card*-*/status; do echo "$(dirname "$s" | xargs basename): $(cat "$s")"; done

sec ">>> WHO HOLDS DRM MASTER (dri clients) — THE KEY QUESTION <<<"
# 'master' column: 'y' = has DRM master. On the seat's own card exactly one
# client (its compositor, or logind on its behalf) should hold it.
for d in /sys/kernel/debug/dri/*; do
  [ -d "$d" ] || continue
  echo "--- $(basename "$d")  name=$(head -1 "$d/name" 2>/dev/null) ---"
  cat "$d/clients" 2>/dev/null || echo "(no clients file)"
done

sec "framebuffer console (fbcon) bindings — a console fb can hold master"
echo "/proc/fb:"
cat /proc/fb 2>/dev/null
for v in /sys/class/vtconsole/*/; do
  [ -e "$v/name" ] && echo "$(basename "$v"): name=$(cat "$v/name")  bind=$(cat "$v/bind")"
done

sec "who has /dev/dri/card* open"
for n in /dev/dri/card*; do
  echo "--- $n ---"
  fuser -v "$n" 2>&1 | head
done

sec "logind seats + sessions"
loginctl list-seats
echo
loginctl list-sessions

sec "per-seat detail (all non-seat0 seats)"
for seat in $(loginctl list-seats --no-legend 2>/dev/null | awk '{print $1}' | grep -v '^seat0$'); do
  echo "--- seat-status $seat ---"
  loginctl seat-status "$seat" 2>&1
done

sec "greeter sessions (detail — Active? on which seat?)"
echo "greeter sessions: ${greeter_sessions:-<none>}   greeter uid(s): ${greeter_uids:-<none>}"
for s in $greeter_sessions; do
  echo "--- session $s ---"
  loginctl show-session "$s" -p Name -p Seat -p State -p Active -p Class -p Type -p TTY 2>&1
done

sec "greeter compositor processes"
for uid in $greeter_uids; do
  echo "--- uid $uid ($(id -nu "$uid" 2>/dev/null)) ---"
  ps -u "$uid" -o pid,ppid,args 2>/dev/null | grep -iE 'kwin|gnome-shell|weston|startplasma|wayland' | grep -v grep || echo "(none running)"
done

sec "greeter journal — compositor/DRM/atomic/master (this boot)"
for uid in $greeter_uids; do
  echo "--- uid $uid ---"
  journalctl -b _UID="$uid" --no-pager 2>&1 \
    | grep -iE 'kwin|gnome-shell|drm|atomic|master|permission|EGL|GBM|nvidia|no outputs|backend|Failed|render|fatal|abort' \
    | grep -viE 'cursor theme|thread priority' | tail -70
done

sec "display-manager.service journal (this boot, last 40)"
journalctl -b -u display-manager.service --no-pager 2>&1 | tail -40

sec "drm_info (if available)"
command -v drm_info >/dev/null && drm_info 2>&1 | head -80 || echo "(drm_info not installed)"

chmod 644 "$out" 2>/dev/null
sec "DONE — full output at $out"
