#!/usr/bin/env bash
# seatphysical greeter DRM-master diagnostics.
#   run:  sudo bash ~/seat-drm-diag.sh
# Collects why the plasmalogin greeter's kwin_wayland can't become DRM master on
# the NVIDIA seat. Writes to ~/seat-drm-diag.out (world-readable) + stdout.
out=/home/agentydragon/seat-drm-diag.out
exec > >(tee "$out") 2>&1
sec() { printf '\n========== %s ==========\n' "$1"; }

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
lsmod | grep -iE '^nvidia' || echo "(no nvidia modules?!)"

sec "modprobe.d nvidia config (does it set fbdev/modeset?)"
grep -rInE 'nvidia' /etc/modprobe.d/ 2>/dev/null || echo "(none)"

sec "DRM node -> PCI / driver / seat map"
for c in /sys/class/drm/card[0-9]*; do
  [ -e "$c/device" ] || continue
  n=$(basename "$c")
  pci=$(basename "$(readlink -f "$c/device")")
  drv=$(basename "$(readlink -f "$c/device/driver" 2>/dev/null)" 2>/dev/null)
  seat=$(udevadm info -q property "/dev/dri/$n" 2>/dev/null | grep -oE 'ID_SEAT=[^ ]*' || echo ID_SEAT=seat0)
  echo "$n  pci=$pci  driver=$drv  $seat"
done

sec ">>> WHO HOLDS DRM MASTER (dri clients) — THE KEY QUESTION <<<"
# 'master' column: 'y' = has DRM master. Look at the NVIDIA card's node.
for d in /sys/kernel/debug/dri/*; do
  [ -d "$d" ] || continue
  echo "--- $(basename "$d")  name=$(head -1 "$d/name" 2>/dev/null) ---"
  cat "$d/clients" 2>/dev/null || echo "(no clients file)"
done

sec "framebuffer console (fbcon) bindings — a console fb can hold master"
echo "/proc/fb:"
cat /proc/fb 2>/dev/null
for v in /sys/class/vtconsole/*/; do
  [ -e "$v/name" ] && echo "$(basename "$v"): name=$(cat "$v/name" 2>/dev/null)  bind=$(cat "$v/bind" 2>/dev/null)"
done

sec "who has /dev/dri/card* open"
for n in /dev/dri/card*; do
  echo "--- $n ---"
  fuser -v "$n" 2>&1 | head
done
command -v lsof >/dev/null && {
  echo "--- lsof /dev/dri ---"
  lsof /dev/dri/* 2>/dev/null | head -40
}

sec "logind seats + sessions"
loginctl list-seats
echo
loginctl list-sessions
echo
echo "--- seat-status seatphysical ---"
loginctl seat-status seatphysical 2>&1

sec "the seatphysical greeter session detail (Active? Master grant?)"
gs=$(loginctl list-sessions --no-legend 2>/dev/null | awk '/seatphysical/{print $1; exit}')
echo "greeter session on seatphysical: ${gs:-<none>}"
[ -n "$gs" ] && loginctl show-session "$gs" 2>&1

sec "plasmalogin greeter compositor processes"
ps -u plasmalogin -o pid,ppid,args 2>/dev/null | grep -iE 'kwin|startplasma|wayland' | grep -v grep || echo "(none running)"

sec "greeter (UID 989) journal — kwin/DRM/atomic/master (this boot)"
journalctl -b _UID=989 --no-pager 2>&1 \
  | grep -iE 'kwin|drm|atomic|master|permission|EGL|GBM|nvidia|no outputs|backend|Failed|render|fatal|abort' \
  | grep -viE 'cursor theme' | tail -70

sec "plasmalogin.service journal (this boot, last 40)"
journalctl -b -u plasmalogin.service --no-pager 2>&1 | tail -40

sec "kwin greeter user unit (plasma-login-kwin_wayland)"
journalctl -b _SYSTEMD_USER_UNIT=plasma-login-kwin_wayland.service --no-pager 2>&1 | tail -40 || true

sec "drm_info (if available)"
if command -v drm_info >/dev/null; then drm_info 2>&1 | head -80; else echo "(drm_info not installed)"; fi

chmod 644 "$out" 2>/dev/null
sec "DONE — full output at $out"
