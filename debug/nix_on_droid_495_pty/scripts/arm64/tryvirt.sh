#!/bin/bash
# Boot the same Android 14 arm64 kernel + emulator-built initrd on stock QEMU's
# `virt` machine, which unlike the emulator's arm64 `ranchu` has a working PCIe
# bridge, PSCI (so SMP comes up) and an honoured -cpu.
#
# The question this answers: is init's immediate EL0 data abort a property of
# ranchu's under-built machine/DT and its ARMv8.0 CPU, or intrinsic to running
# this userspace under TCG at all?
set -uo pipefail
S=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
IMG=$S/sdk/system-images/android-34/google_apis/arm64-v8a
LOG=$S/virt.log

pkill -f 'qemu-system-aarch64' 2>/dev/null
sleep 2
: >"$LOG"

timeout 240 qemu-system-aarch64 \
  -machine virt -cpu max -smp 4 -m 4096 -accel tcg \
  -kernel "$IMG/kernel-ranchu" \
  -initrd "$S/avd/nod14.avd/initrd" \
  -append "console=ttyAMA0 earlycon=pl011,0x9000000 printk.devkmsg=on androidboot.hardware=ranchu androidboot.selinux=enforcing bootconfig" \
  -drive file="$IMG/system.img",if=none,id=sys,format=raw,readonly=on \
  -device virtio-blk-device,drive=sys \
  -drive file="$IMG/vendor.img",if=none,id=ven,format=raw,readonly=on \
  -device virtio-blk-device,drive=ven \
  -nographic >"$LOG" 2>&1

echo "--- last 25 lines ---"
tail -25 "$LOG"
echo "--- verdict ---"
if grep -qa 'Kernel panic' "$LOG"; then
  echo "PANIC: $(grep -a 'Kernel panic' "$LOG" | tail -1)"
elif grep -qa 'init: ' "$LOG"; then
  echo "init produced output (got further than on ranchu)"
else
  echo "no panic, no init output"
fi
grep -ac 'psci' "$LOG"
