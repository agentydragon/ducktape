#!/bin/busybox sh
# Init script for QEMU nftables smoke test VM.
# Boots into a minimal environment, loads nftables kernel modules,
# runs the testprobe binary at the requested smoke levels, and powers off.
#
# Kernel command-line parameters:
#   levels=1,2,3,...  - comma-separated list of nft-smoke levels to run
#   quiet             - suppress kernel messages on console
set -e

# Mount essential filesystems.
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sys /sys
/bin/busybox mount -t devtmpfs dev /dev

# Suppress kernel messages on console (keep output clean for test parsing).
/bin/busybox dmesg -n 1

# Load nftables kernel modules in dependency order.
# crc32c_generic → libcrc32c → nfnetlink → nf_tables
for mod in \
  /modules/crc32c_generic.ko \
  /modules/nfnetlink.ko \
  /modules/libcrc32c.ko \
  /modules/nf_tables.ko; do
  if [ -f "$mod" ]; then
    /bin/busybox insmod "$mod" || echo "WARN: insmod $mod failed"
  fi
done

# Parse levels from kernel command line.
LEVELS=""
for arg in $(/bin/busybox cat /proc/cmdline); do
  case "$arg" in
    levels=*) LEVELS="${arg#levels=}" ;;
  esac
done

if [ -z "$LEVELS" ]; then
  echo "QEMU_TEST: ERROR no levels= on kernel cmdline"
  echo "o" >/proc/sysrq-trigger
  exit 1
fi

# Run each level.
FAIL=0
IFS=","
for level in $LEVELS; do
  echo "QEMU_TEST: running level $level"
  if /testprobe -ebusy-retry -nft-smoke="$level"; then
    echo "QEMU_TEST: PASS level=$level"
  else
    echo "QEMU_TEST: FAIL level=$level"
    FAIL=1
  fi
done

if [ "$FAIL" -eq 0 ]; then
  echo "QEMU_TEST: ALL_PASSED"
else
  echo "QEMU_TEST: SOME_FAILED"
fi

# Power off the VM cleanly.
echo "o" >/proc/sysrq-trigger
