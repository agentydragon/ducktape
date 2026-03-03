#!/usr/bin/env bash
# QEMU-based nftables smoke test runner.
# Usage: run_qemu_test.sh <vmlinuz> <initramfs> <qemu-binary> [levels]
#
# Boots a minimal Linux VM in QEMU (TCG, no KVM required), runs the testprobe
# nft-smoke at the given levels, and checks the output for PASS/FAIL markers.
#
# Exit codes:
#   0 = all levels passed
#   1 = one or more levels failed or QEMU exited abnormally
set -euo pipefail

VMLINUZ="$1"
INITRAMFS="$2"
QEMU="$3"
LEVELS="${4:-1,2,3,4,5,6}"

if [ ! -f "$VMLINUZ" ]; then
  echo "ERROR: vmlinuz not found: $VMLINUZ" >&2
  exit 1
fi
if [ ! -f "$INITRAMFS" ]; then
  echo "ERROR: initramfs not found: $INITRAMFS" >&2
  exit 1
fi
if [ ! -x "$QEMU" ]; then
  echo "ERROR: qemu binary not found or not executable: $QEMU" >&2
  exit 1
fi

TMPOUT=$(mktemp)
trap 'rm -f "$TMPOUT"' EXIT

echo "Running nft-smoke levels=$LEVELS in QEMU..."

# Run QEMU with:
#   -kernel:   direct kernel boot (no BIOS/UEFI)
#   -initrd:   our initramfs with testprobe + busybox + modules
#   -append:   kernel cmdline (serial console, levels to test)
#   -nographic: no GUI, serial on stdout
#   -no-reboot: exit on kernel panic/poweroff instead of rebooting
#   -m 256:    256MB RAM (plenty for nftables tests)
#   -machine accel=tcg: software emulation (no KVM required)
#   -cpu max:  expose all CPU features (useful for some nftables features)
timeout 120 "$QEMU" \
  -kernel "$VMLINUZ" \
  -initrd "$INITRAMFS" \
  -append "console=ttyS0 panic=-1 quiet levels=$LEVELS" \
  -nographic \
  -no-reboot \
  -m 256 \
  -machine "accel=tcg" \
  -cpu max \
  -display none \
  2>&1 | tee "$TMPOUT" || true

echo ""
echo "=== Test Results ==="

# Check for overall pass/fail markers.
if grep -q "QEMU_TEST: ALL_PASSED" "$TMPOUT"; then
  echo "All nft-smoke levels passed."
  exit 0
fi

if grep -q "QEMU_TEST: SOME_FAILED" "$TMPOUT"; then
  echo "Some nft-smoke levels FAILED:"
  grep "QEMU_TEST: FAIL" "$TMPOUT" || true
  exit 1
fi

# If we get here, QEMU didn't produce expected markers.
echo "ERROR: QEMU test did not complete normally." >&2
echo "Last 20 lines of output:" >&2
tail -20 "$TMPOUT" >&2
exit 1
