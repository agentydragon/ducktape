#!/bin/bash
# Start Talos VM using kernel and initramfs with tap/bridge networking
# Provides proper DNS resolution unlike user-mode networking

set -e

VM_DIR="/home/user/ducktape/talos-vm"
KERNEL="$VM_DIR/_out/vmlinuz-amd64"
INITRD="$VM_DIR/_out/initramfs-amd64.xz"
DISK="$VM_DIR/talos-disk.qcow2"
TAP="tap-talos"

# Check if tap interface exists
if ! ip link show "$TAP" &> /dev/null; then
    echo "❌ Tap interface $TAP not found!"
    echo ""
    echo "Please run: sudo ./setup-bridge.sh"
    exit 1
fi

echo "Starting Talos VM with kernel boot (tap networking)..."
echo "Kernel: $KERNEL"
echo "Initramfs: $INITRD"
echo "Disk: $DISK"
echo "Network: $TAP -> br-talos"
echo ""
echo "VM IP: 192.168.100.10"
echo "Talos API: 192.168.100.10:50000"
echo "Kubernetes API: 192.168.100.10:6443"
echo ""
echo "From host, you can access:"
echo "  talosctl: --nodes 192.168.100.10:50000"
echo "  kubectl: via kubeconfig pointing to https://192.168.100.10:6443"
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
  -netdev tap,id=net0,ifname=$TAP,script=no,downscript=no \
  -device virtio-net-pci,netdev=net0 \
  -nographic
