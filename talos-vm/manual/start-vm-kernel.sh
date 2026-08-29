#!/bin/bash
# Start Talos VM using kernel and initramfs (no ISO)

set -e

VM_DIR="/home/user/ducktape/talos-vm"
KERNEL="$VM_DIR/_out/vmlinuz-amd64"
INITRD="$VM_DIR/_out/initramfs-amd64.xz"
DISK="$VM_DIR/talos-disk.qcow2"

echo "Starting Talos VM with kernel boot..."
echo "Kernel: $KERNEL"
echo "Initramfs: $INITRD"
echo "Disk: $DISK"
echo ""
echo "VM will be accessible at 10.0.2.15 inside QEMU"
echo "Talos API: localhost:50000"
echo "Kubernetes API: localhost:6443"
echo ""

exec qemu-system-x86_64 \
  -name talos-kernel \
  -machine type=q35 \
  -cpu Nehalem \
  -m 2048 \
  -smp 2 \
  -drive file=$DISK,if=virtio,format=qcow2 \
  -kernel $KERNEL \
  -initrd $INITRD \
  -append "console=ttyS0 talos.platform=metal slab_nomerge pti=on" \
  -netdev user,id=net0,hostfwd=tcp::50000-:50000,hostfwd=tcp::6443-:6443,dns=8.8.8.8 \
  -device virtio-net-pci,netdev=net0 \
  -rtc base=utc,clock=host \
  -nographic
