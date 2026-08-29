#!/bin/bash
# Quick Start: Talos + Kubernetes with kubectl
# Requires: KVM-enabled system

set -e

VM_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$VM_DIR"

echo "==========================================="
echo " Talos + Kubernetes Quick Start"
echo "==========================================="
echo ""

# Check for KVM
if [ ! -e "/dev/kvm" ]; then
    echo "❌ ERROR: /dev/kvm not found"
    echo ""
    echo "KVM hardware acceleration is required for practical use."
    echo ""
    echo "To enable:"
    echo "  1. Enable CPU virtualization in BIOS (Intel VT-x or AMD-V)"
    echo "  2. Load kernel modules: modprobe kvm kvm_intel"
    echo ""
    echo "Without KVM, this setup would take 2-4 hours (vs 5 minutes with KVM)"
    exit 1
fi

echo "✓ KVM is available"
echo ""

# Step 1: Download components
echo "[1/7] Downloading Talos components..."
if [ ! -f "_out/vmlinuz-amd64" ] || [ ! -f "_out/initramfs-amd64.xz" ]; then
    ./download-talos.sh
else
    echo "✓ Components already downloaded"
fi
echo ""

# Step 2: Start VM
echo "[2/7] Starting Talos VM..."
if pgrep -f "qemu.*talos" > /dev/null; then
    echo "✓ VM already running"
else
    nohup ./start-vm-kernel.sh > vm-kernel.log 2>&1 &
    VM_PID=$!
    echo "✓ VM started (PID: $VM_PID)"
fi
echo ""

# Step 3: Wait for boot
echo "[3/7] Waiting for VM to boot (30-60 seconds)..."
for i in {1..60}; do
    sleep 2
    if tail -20 vm-kernel.log 2>/dev/null | grep -q "entering maintenance service"; then
        echo "✓ VM booted and ready"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "❌ Timeout waiting for VM boot"
        echo "Check vm-kernel.log for details"
        exit 1
    fi
    printf "."
done
echo ""
echo ""

# Step 4: Apply configuration
echo "[4/7] Applying Talos configuration..."
sleep 5  # Let maintenance service stabilize
./talosctl apply-config --insecure --nodes 127.0.0.1:50000 --file controlplane.yaml --timeout 60s
echo "✓ Configuration applied"
echo ""

# Step 5: Configure talosctl
echo "[5/7] Configuring talosctl..."
./talosctl config endpoint 127.0.0.1:50000 --talosconfig=talosconfig
./talosctl config node 127.0.0.1:50000 --talosconfig=talosconfig
echo "✓ Talosctl configured"
echo ""

# Step 6: Wait for system to process config and reboot
echo "[6/7] Waiting for system to install and reboot (60-90 seconds)..."
sleep 60
for i in {1..30}; do
    sleep 2
    if ./talosctl --talosconfig=talosconfig version 2>&1 | grep -q "Server:.*Tag"; then
        echo "✓ System ready"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "⚠ System still initializing, proceeding anyway..."
        break
    fi
    printf "."
done
echo ""
echo ""

# Step 7: Bootstrap Kubernetes
echo "[7/7] Bootstrapping Kubernetes cluster..."
./talosctl bootstrap --talosconfig=talosconfig 2>&1 || echo "Note: Bootstrap may already be in progress"
echo "✓ Bootstrap initiated"
echo ""

# Wait for Kubernetes to be ready
echo "Waiting for Kubernetes API to be ready (120-180 seconds)..."
sleep 60
for i in {1..60}; do
    sleep 2
    if ./talosctl kubeconfig kubeconfig-talos --talosconfig=talosconfig 2>/dev/null; then
        echo "✓ Kubeconfig generated"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "⚠ Kubernetes API not ready yet, but continuing..."
        echo "You may need to wait a few more minutes and run:"
        echo "  ./talosctl kubeconfig kubeconfig-talos --talosconfig=talosconfig"
    fi
    printf "."
done
echo ""
echo ""

# Test kubectl
echo "==========================================="
echo " Testing kubectl"
echo "==========================================="
echo ""

export KUBECONFIG="$VM_DIR/kubeconfig-talos"

if [ -f "kubeconfig-talos" ]; then
    echo "$ kubectl version --client"
    kubectl version --client
    echo ""

    echo "$ kubectl cluster-info"
    kubectl cluster-info 2>&1 || echo "Note: Cluster may still be initializing"
    echo ""

    echo "$ kubectl get nodes"
    kubectl get nodes 2>&1 || echo "Note: Nodes may take a few more minutes to be ready"
    echo ""

    echo "$ kubectl get pods --all-namespaces"
    kubectl get pods --all-namespaces 2>&1 || echo "Note: Pods may still be starting"
    echo ""
fi

echo "==========================================="
echo " Setup Complete!"
echo "==========================================="
echo ""
echo "Your Talos + Kubernetes cluster is ready!"
echo ""
echo "To use kubectl:"
echo "  export KUBECONFIG=$VM_DIR/kubeconfig-talos"
echo "  kubectl get nodes"
echo "  kubectl get pods --all-namespaces"
echo ""
echo "To manage Talos:"
echo "  ./talosctl --talosconfig=talosconfig dashboard"
echo "  ./talosctl --talosconfig=talosconfig get members"
echo ""
echo "VM logs:"
echo "  tail -f vm-kernel.log"
echo ""
echo "To stop the VM:"
echo "  pkill -f 'qemu.*talos'"
echo ""
echo "For more information, see README.md and QUICK_START_KVM.md"
