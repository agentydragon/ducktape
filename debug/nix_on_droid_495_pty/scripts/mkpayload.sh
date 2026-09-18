#!/bin/bash
# Load files into the guest's payload disk (/dev/block/vdb).
#
# This kernel has no loop or ext4 module -- it is a minimal Firecracker
# kernel, and mount(8) fails with "unknown filesystem type" -- so the image is
# populated with debugfs, which writes into an ext4 image directly without
# mounting it.
set -euo pipefail
S=/tmp/claude-0/-home-user-ducktape/17570302-6f29-5e29-9145-7d878c0711db/scratchpad
IMG="$S/vm/payload.img"

for f in "$@"; do
  name=$(basename "$f")
  debugfs -w -R "rm /$name" "$IMG" >/dev/null 2>&1 || true
  debugfs -w -R "write $f $name" "$IMG" 2>&1 | grep -v '^debugfs ' || true
  debugfs -w -R "sif /$name mode 0100755" "$IMG" >/dev/null 2>&1 || true
done
echo "--- payload contents ---"
debugfs -R "ls -l /" "$IMG" 2>/dev/null
