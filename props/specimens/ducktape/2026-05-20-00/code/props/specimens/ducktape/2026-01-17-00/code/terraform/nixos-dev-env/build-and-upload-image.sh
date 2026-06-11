#!/usr/bin/env bash
set -euo pipefail

# Build and upload NixOS qcow2 cloud image to Proxmox
# Usage: ./build-and-upload-image.sh <proxmox_host> <storage>

PROXMOX_HOST="${1}"
STORAGE="${2}"
IMAGE_NAME="nixos-cloud-image"

echo "Building NixOS qcow2 cloud image..."
nix run github:nix-community/nixos-generators -- \
  --format qcow-efi \
  --configuration ./cloud-image.nix \
  -o "$IMAGE_NAME"

echo "Uploading image to Proxmox ($PROXMOX_HOST)..."
scp "$IMAGE_NAME/nixos.qcow2" "root@${PROXMOX_HOST}:/tmp/nixos-cloud.qcow2"

echo "Image uploaded successfully to /tmp/nixos-cloud.qcow2 on $PROXMOX_HOST"
echo "Image path: $IMAGE_NAME/nixos.qcow2"
