#!/bin/bash
# Download Talos Linux and talosctl

set -e

TALOS_VERSION="v1.9.2"
VM_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "======================================"
echo "Talos Linux Download Script"
echo "======================================"
echo "Version: $TALOS_VERSION"
echo "Directory: $VM_DIR"
echo ""

cd "$VM_DIR"

# Download Talos ISO
if [ ! -f "talos-amd64.iso" ]; then
    echo "Downloading Talos Linux ISO..."
    curl -Lo talos-amd64.iso \
        "https://github.com/siderolabs/talos/releases/download/${TALOS_VERSION}/metal-amd64.iso"
    echo "✓ ISO downloaded"
else
    echo "✓ ISO already exists"
fi

# Download talosctl
if [ ! -f "talosctl" ]; then
    echo "Downloading talosctl CLI..."
    curl -Lo talosctl \
        "https://github.com/siderolabs/talos/releases/download/${TALOS_VERSION}/talosctl-linux-amd64"
    chmod +x talosctl
    echo "✓ talosctl downloaded"
else
    echo "✓ talosctl already exists"
fi

# Create disk image if it doesn't exist
if [ ! -f "talos-disk.qcow2" ]; then
    echo "Creating VM disk image..."
    qemu-img create -f qcow2 talos-disk.qcow2 20G
    echo "✓ Disk image created"
else
    echo "✓ Disk image already exists"
fi

# Generate Talos configuration if it doesn't exist
if [ ! -f "controlplane.yaml" ]; then
    echo "Generating Talos configuration..."
    ./talosctl gen config talos-k8s https://localhost:6443 --output-dir .
    echo "✓ Configuration generated"
    echo ""
    echo "IMPORTANT: The generated configuration files contain sensitive"
    echo "certificates and keys. Keep them secure!"
else
    echo "✓ Configuration already exists"
fi

echo ""
echo "======================================"
echo "Setup Complete!"
echo "======================================"
echo ""
echo "Next steps:"
echo "  1. Start the VM:       ./setup-talos.sh start"
echo "  2. Apply config:       ./setup-talos.sh config"
echo "  3. Bootstrap K8s:      ./setup-talos.sh bootstrap"
echo "  4. Get kubeconfig:     ./setup-talos.sh kubeconfig"
echo ""
echo "See README.md for detailed instructions."
