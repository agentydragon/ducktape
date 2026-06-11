#!/bin/bash
set -e

RESOLUTION="${RESOLUTION:-1280x800x24}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-tana}"
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

# Start dbus session bus (required by Chromium/Electron)
eval "$(dbus-launch --sh-syntax)"

# Start virtual framebuffer
Xvfb :99 -screen 0 "$RESOLUTION" &
XVFB_PID=$!
sleep 1

# Start a lightweight window manager so spawned windows can be moved/focused in
# noVNC instead of landing on a bare X root window.
openbox-session &

# Start VNC server (listens on localhost only — noVNC proxies it).
# -noipv6 avoids "listen6: bind: Address already in use" since this container
# doesn't have IPv6 loopback wired up.
x11vnc -display :99 -forever -nopw -listen 127.0.0.1 -rfbport 5900 -noipv6 &

# Start noVNC web client
websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 &

# Start the localhost-only reseed receiver. The firebase_resigner sidecar
# POSTs tana://auth?... URLs here when it needs to re-establish the
# Firebase session inside the running Electron renderer.
python3 /reseed_receiver.py &

# Start Tana Desktop (--no-sandbox required in containers)
/opt/tana/Tana --no-sandbox --disable-gpu &
TANA_PID=$!

# Wait for Tana to exit (or for the container to be stopped)
wait "$TANA_PID"
