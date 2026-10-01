#!/bin/bash
# Start (or restart) the Android VM plus the serial relay, both detached.
set -euo pipefail
S=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad

pkill -f 'qemu-system-x86_64.*android' 2>/dev/null || true
pkill -f 'serial.py relay' 2>/dev/null || true
sleep 1
rm -f "$S/vm/console.log" "$S/vm/cmd.fifo" "$S/vm/serial.sock"

setsid bash "$S/vm/boot.sh" >"$S/vm/qemu.stdout" 2>&1 </dev/null &
sleep 3
setsid python3 "$S/vm/serial.py" relay >"$S/vm/relay.log" 2>&1 </dev/null &
sleep 2
pgrep -af 'qemu-system-x86_64' | head -1
