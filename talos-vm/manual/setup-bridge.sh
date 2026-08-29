#!/bin/bash
# Setup bridge networking for QEMU Talos VM
# This provides proper DNS resolution unlike user-mode networking

set -e

BRIDGE="br-talos"
TAP="tap-talos"

echo "Setting up bridge networking for Talos VM..."
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "This script needs root privileges to configure networking."
    echo "Rerunning with sudo..."
    exec sudo "$0" "$@"
fi

# Create bridge if it doesn't exist
if ! ip link show "$BRIDGE" &> /dev/null; then
    echo "Creating bridge: $BRIDGE"
    ip link add name "$BRIDGE" type bridge
    ip link set dev "$BRIDGE" up
    # Assign IP to bridge (192.168.100.1/24 for Talos network)
    ip addr add 192.168.100.1/24 dev "$BRIDGE"
else
    echo "✓ Bridge $BRIDGE already exists"
fi

# Create tap interface if it doesn't exist
if ! ip link show "$TAP" &> /dev/null; then
    echo "Creating tap interface: $TAP"
    # If running as root directly (not via sudo), SUDO_USER is empty
    if [ -n "$SUDO_USER" ]; then
        ip tuntap add dev "$TAP" mode tap user "$SUDO_USER"
    else
        ip tuntap add dev "$TAP" mode tap
    fi
    ip link set dev "$TAP" up
    ip link set dev "$TAP" master "$BRIDGE"
else
    echo "✓ Tap interface $TAP already exists"
fi

# Enable IP forwarding
echo "Enabling IP forwarding..."
sysctl -w net.ipv4.ip_forward=1 > /dev/null

# Set up NAT for bridge network to access internet
if ! iptables -t nat -C POSTROUTING -s 192.168.100.0/24 -j MASQUERADE 2>/dev/null; then
    echo "Setting up NAT for bridge network..."
    iptables -t nat -A POSTROUTING -s 192.168.100.0/24 -j MASQUERADE
else
    echo "✓ NAT already configured"
fi

# Allow forwarding for bridge
if ! iptables -C FORWARD -i "$BRIDGE" -j ACCEPT 2>/dev/null; then
    echo "Allowing forwarding for bridge..."
    iptables -A FORWARD -i "$BRIDGE" -j ACCEPT
    iptables -A FORWARD -o "$BRIDGE" -j ACCEPT
else
    echo "✓ Forwarding already configured"
fi

echo ""
echo "✓ Bridge networking configured!"
echo ""
echo "Bridge:     $BRIDGE (192.168.100.1/24)"
echo "Tap:        $TAP"
echo "VM IP:      192.168.100.10 (will be assigned via Talos config)"
echo ""
echo "To remove this setup:"
echo "  sudo ip link del $TAP"
echo "  sudo ip link del $BRIDGE"
echo ""
