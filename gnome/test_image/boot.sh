#!/bin/bash
# One-shot boot for the render test container.
#
# Runs the postinst-equivalent setup that rules_distroless skips
# (gsettings schemas, gdk-pixbuf loader cache, MIME database, dbus
# machine-id), starts Xvfb on :99, forks a long-lived session bus via
# dbus-launch, and writes its address to /tmp/dbus.env. Touches
# /tmp/boot.ready and blocks. The test driver execs into the container
# to drive everything else (start gnome-shell, poll readiness,
# screenshot, kill shell) — keeping the orchestration logic in Python
# where errors are attributable per step.
#
# Usage (called once per container by the test driver):
#   docker exec <container> /usr/local/bin/boot.sh
#
# Env (optional):
#   RENDER_WIDTH, RENDER_HEIGHT — Xvfb dims (default 1920x40).
set -euo pipefail

# Sized tall enough for the open popup menu to fit below the panel — the
# menu test driver relies on the menu landing entirely on-screen.
WIDTH="${RENDER_WIDTH:-1920}"
HEIGHT="${RENDER_HEIGHT:-500}"

glib-compile-schemas /usr/share/glib-2.0/schemas/
update-mime-database /usr/share/mime
# libgdk-pixbuf2.0-bin installs the binary under the lib dir; rules_distroless
# skips the update-alternatives postinst that would symlink it onto PATH.
/usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders --update-cache

mkdir -p /var/lib/dbus
dbus-uuidgen --ensure=/var/lib/dbus/machine-id

Xvfb :99 -screen 0 "${WIDTH}x${HEIGHT}x24" -nolisten tcp >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!

xvfb_ready=0
for _ in $(seq 1 50); do
  if [[ -S /tmp/.X11-unix/X99 ]]; then
    xvfb_ready=1
    break
  fi
  sleep 0.1
done
if [[ "$xvfb_ready" -ne 1 ]]; then
  echo "Xvfb never created /tmp/.X11-unix/X99 within 5s; xvfb.log:" >&2
  tail -50 /tmp/xvfb.log >&2 || true
  exit 1
fi

# Fork a session bus that survives across multiple gnome-shell launches.
# dbus-launch (vs dbus-run-session) prints the address and exits, leaving
# the daemon running. Subsequent exec calls source /tmp/dbus.env.
eval "$(dbus-launch --sh-syntax)"
{
  echo "export DBUS_SESSION_BUS_ADDRESS='$DBUS_SESSION_BUS_ADDRESS'"
  echo "export DBUS_SESSION_BUS_PID='$DBUS_SESSION_BUS_PID'"
} >/tmp/dbus.env

touch /tmp/boot.ready

# Block until container teardown — Xvfb and the dbus daemon stay alive.
wait "$XVFB_PID"
