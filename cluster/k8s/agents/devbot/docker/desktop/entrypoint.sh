#!/bin/bash
set -e

# Set VNC password from environment variable or use default
if [ -n "$VNC_PASSWORD" ]; then
  echo "$VNC_PASSWORD" | vncpasswd -f >/home/devbot/.vnc/passwd
else
  echo "devbot" | vncpasswd -f >/home/devbot/.vnc/passwd
fi
chmod 600 /home/devbot/.vnc/passwd

# Set resolution
RESOLUTION=${RESOLUTION:-1920x1080}

# Start VNC server
exec vncserver :0 \
  -geometry "$RESOLUTION" \
  -depth 24 \
  -localhost no \
  -SecurityTypes VncAuth \
  -fg
