#!/bin/bash
# Install com.termux.nix and let it fetch its own bootstrap over the proxy.
#
# The bootstrap zip is deliberately NOT pushed from the host: having the app
# download it is what proves the guest's route to the proxy and Android's
# trust in the proxy CA actually work.
set -uo pipefail
D=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
ADB="$D/sdk/platform-tools/adb"
SER=emulator-5554
U=/data/data/com.termux.nix/files/usr

a() { "$ADB" -s "$SER" "$@"; }

echo "=== install ==="
a install -r "$D/payload/nix-on-droid.apk" 2>&1 | tail -3

echo "=== package info ==="
a shell 'dumpsys package com.termux.nix | grep -E "userId=|flags=|primaryCpuAbi" | head -5'

echo "=== launch (first run downloads + unpacks the bootstrap) ==="
a shell 'am start -n com.termux.nix/com.termux.app.TermuxActivity' 2>&1 | tail -2

echo "=== waiting for the bootstrap to land ==="
for _ in $(seq 1 120); do
  if a shell "test -x $U/bin/login && echo yes" 2>/dev/null | grep -q yes; then
    echo "BOOTSTRAP_PRESENT"
    a shell "ls -la $U/bin/ | head"
    exit 0
  fi
  sleep 15
done
echo "BOOTSTRAP_MISSING after 30min"
a shell "ls -la /data/data/com.termux.nix/files 2>&1 | head"
a logcat -d -t 80 2>&1 | grep -iE 'termux|nix|bootstrap|http|ssl|cert' | tail -30
