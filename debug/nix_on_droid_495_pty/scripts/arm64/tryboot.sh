#!/bin/bash
# Launch the AVD with extra flags, give it a bounded window, and print how far
# the guest kernel got. With -show-kernel in the launcher, a failed boot is
# visible in seconds instead of looking like a slow one for half an hour.
set -uo pipefail
S=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
WAIT=${WAIT:-150}
LOG=$S/try.log

pkill -f qemu-system-aarch64 2>/dev/null
sleep 3
rm -f "$S"/avd/nod14.avd/*.lock
: >"$LOG"
setsid bash "$S/sdk/launch.sh" "$@" >"$LOG" 2>&1 </dev/null &
sleep "$WAIT"

echo "=== flags: $* ==="
echo "--- cpu model actually used ---"
grep -aoE '"\-cpu" *$|argv\[[0-9]+\] = "[a-z0-9-]*a57[a-z0-9-]*"|Hardware name: [a-z]*' "$LOG" | tail -3
echo "--- last 12 kernel lines ---"
grep -aE '^\[' "$LOG" | tail -12
echo "--- verdict ---"
if grep -qa 'Kernel panic' "$LOG"; then
  echo "PANIC: $(grep -a 'Kernel panic' "$LOG" | tail -1)"
elif grep -qa 'Run /init as init process' "$LOG"; then
  echo "init started, no panic yet (last kernel timestamp above)"
else
  echo "kernel did not reach init"
fi
