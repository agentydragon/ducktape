# Atlas Network Cheatsheet

Quick reference for debugging ethernet on atlas without internet access.

## Check Current State

```bash
# What interfaces exist and their state (UP/DOWN/NO-CARRIER)
ip link show

# IP addresses
ip addr show

# Routing table
ip route show

# Is there a link? (quick check)
cat /sys/class/net/enp12s0/carrier    # 1 = link, 0 = no link

# Detailed NIC status (speed, duplex, link)
ethtool enp12s0
```

## Bring Interfaces Up/Down

```bash
# Bring a NIC up/down
ip link set enp12s0 up
ip link set enp12s0 down

# Restart all networking (re-reads /etc/network/interfaces)
systemctl restart networking

# Or the Proxmox way (preferred, non-disruptive reload)
ifreload -a
```

## Get an IP Address

```bash
# DHCP on a raw interface (not bridged)
dhclient -v enp12s0

# DHCP on the bridge (normal path)
dhclient -v vmbr0

# Release DHCP lease
dhclient -r vmbr0

# Manual static IP (temporary, lost on reboot)
ip addr add 10.0.182.102/16 dev vmbr0
ip route add default via 10.0.182.1
```

## Switch Which NIC is Active

To switch `vmbr0` from Aquantia (`enp12s0`) to Intel (`enp11s0`):

```bash
# 1. Edit config
nano /etc/network/interfaces
#    Change: bridge-ports enp12s0  ->  bridge-ports enp11s0
#    Also change enp12s0.4 -> enp11s0.4 in vmbr4 section

# 2. Apply
ifreload -a
# or: systemctl restart networking
```

To temporarily test the other port without editing config:

```bash
# Remove current port from bridge, add the other
ip link set enp12s0 nomaster
ip link set enp11s0 up
ip link set enp11s0 master vmbr0
dhclient -v vmbr0
```

## USB Tethering Fallback

```bash
# Plug phone in, enable USB tethering
ip link set enxe23d47d0e16d up
dhclient -v enxe23d47d0e16d

# NAT so VMs can reach internet through phone
echo 1 > /proc/sys/net/ipv4/ip_forward
iptables -t nat -A POSTROUTING -o enxe23d47d0e16d -j MASQUERADE
```

## Driver Reload (Reset NIC Without Reboot)

```bash
# Reload atlantic driver (Aquantia 10G)
modprobe -r atlantic && modprobe atlantic

# Reload igc driver (Intel I226-V 2.5G)
modprobe -r igc && modprobe igc

# Then bring the interface back up
ip link set enp12s0 up
# and restart networking
ifreload -a
```

## Monitor Link Changes Live

```bash
# Watch kernel messages for NIC events
journalctl -f -k | grep -iE 'atlantic|igc|enp|link|carrier'

# Watch interface state changes
ip monitor link
```

## DNS (When Network is Up But DNS Broken)

```bash
# Check current DNS
cat /etc/resolv.conf

# Temporary manual DNS
echo "nameserver 8.8.8.8" > /etc/resolv.conf

# Test
ping -c1 8.8.8.8      # raw IP (no DNS needed)
ping -c1 google.com    # needs DNS
```

## Collect Full Diagnostics

```bash
sudo bash debug/atlas/ethernet_recurring/collect.sh
```
