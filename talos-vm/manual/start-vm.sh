#!/bin/bash
# Talos VM startup script

set -e

VM_DIR="/home/user/ducktape/talos-vm"
ISO_PATH="$VM_DIR/talos-amd64.iso"
DISK_PATH="$VM_DIR/talos-disk.qcow2"

# VM configuration
MEMORY="2048"
CPUS="2"
MAC_ADDRESS="52:54:00:12:34:56"

# Network configuration - using user mode networking with port forwarding
# Port 50000 on host -> 50000 on guest (Talos API)
# Port 6443 on host -> 6443 on guest (Kubernetes API)

echo "Starting Talos VM..."
echo "Memory: ${MEMORY}MB"
echo "CPUs: ${CPUS}"
echo "Disk: $DISK_PATH"
echo "ISO: $ISO_PATH"
echo ""
echo "Talos API will be available at: localhost:50000"
echo "Kubernetes API will be available at: localhost:6443"
echo ""

qemu-system-x86_64 \
  -name talos-vm \
  -machine type=q35 \
  -cpu qemu64 \
  -m $MEMORY \
  -smp $CPUS \
  -drive file=$DISK_PATH,if=virtio,format=qcow2 \
  -cdrom $ISO_PATH \
  -boot order=d \
  -netdev user,id=net0,hostfwd=tcp::50000-:50000,hostfwd=tcp::6443-:6443 \
  -device virtio-net-pci,netdev=net0,mac=$MAC_ADDRESS \
  -nographic \
  -serial mon:stdio
