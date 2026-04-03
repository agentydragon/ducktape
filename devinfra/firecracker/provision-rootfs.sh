#!/usr/bin/env bash
# Provision the base rootfs for Firecracker VMs on wyrm2.
#
# Builds the NixOS rootfs via Nix (fetches from binary cache if available),
# then writes it to the base LVM thin LV. The kernel and initramfs are baked
# into the VM pod OCI image, not stored on LVM.
#
# Prerequisites:
#   - Run on wyrm2 (has Nix, the ducktape repo, and the LVM VG)
#   - The base LV must exist:
#       lvcreate -V 10G -T openebs-lvmvg/thin-pool -n fc-base-rootfs
#
# Usage:
#   cd ~/code/ducktape
#   ./devinfra/firecracker/provision-rootfs.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ROOTFS_LV="${ROOTFS_LV:-/dev/openebs-lvmvg/fc-base-rootfs}"

echo "Building NixOS rootfs..."
# make-ext4-fs produces a single file derivation — the ext4 image itself.
ROOTFS_FILE="$(nix build "$REPO_ROOT#fc-dev-rootfs" --print-out-paths --no-link)"

if [ ! -f "$ROOTFS_FILE" ]; then
  echo "ERROR: Nix build output is not a file: $ROOTFS_FILE" >&2
  exit 1
fi

ROOTFS_SIZE="$(stat -c%s "$ROOTFS_FILE")"
echo "Rootfs: $ROOTFS_FILE ($(numfmt --to=iec "$ROOTFS_SIZE"))"

if [ ! -b "$ROOTFS_LV" ]; then
  echo "ERROR: LV $ROOTFS_LV does not exist. Create it first:" >&2
  echo "  lvcreate -V 10G -T openebs-lvmvg/thin-pool -n fc-base-rootfs" >&2
  exit 1
fi

LV_SIZE="$(blockdev --getsize64 "$ROOTFS_LV")"
if [ "$ROOTFS_SIZE" -gt "$LV_SIZE" ]; then
  echo "ERROR: Rootfs ($ROOTFS_SIZE) is larger than LV ($LV_SIZE). Extend the LV:" >&2
  echo "  lvextend -L +$((ROOTFS_SIZE - LV_SIZE))b $ROOTFS_LV" >&2
  exit 1
fi

echo "Writing rootfs to $ROOTFS_LV..."
dd if="$ROOTFS_FILE" of="$ROOTFS_LV" bs=4M status=progress conv=fsync

echo "Done. Base rootfs written to $ROOTFS_LV"
echo "VMs will thin-snapshot this LV for their own rootfs PVCs."
