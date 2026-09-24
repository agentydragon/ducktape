#!/bin/bash
# Atlas ethernet investigation — data collection
# Run as root: sudo bash collect.sh
# Output goes to ./snapshot-<timestamp>/

set -euo pipefail

ts=$(date +%Y%m%d-%H%M%S)
dir="snapshot-${ts}"
mkdir -p "$dir"
echo "Collecting to $dir/ ..."

# --- Hardware / PCI ---
lspci -vvv >"$dir/lspci-vvv.txt" 2>&1
lspci -t >"$dir/lspci-tree.txt" 2>&1

# --- Network interfaces ---
ip link show >"$dir/ip-link.txt" 2>&1
ip addr show >"$dir/ip-addr.txt" 2>&1
ip route show >"$dir/ip-route.txt" 2>&1
ip -s link show >"$dir/ip-link-stats.txt" 2>&1

# --- Ethtool on all physical NICs ---
for iface in enp11s0 enp12s0; do
  ethtool "$iface" >"$dir/ethtool-${iface}.txt" 2>&1 || true
  ethtool -i "$iface" >"$dir/ethtool-i-${iface}.txt" 2>&1 || true
  ethtool -S "$iface" >"$dir/ethtool-S-${iface}.txt" 2>&1 || true
  ethtool --phy-statistics "$iface" >"$dir/ethtool-phy-${iface}.txt" 2>&1 || true
done

# --- Network config ---
cp /etc/network/interfaces "$dir/interfaces.txt" 2>/dev/null || true
ls -la /etc/network/interfaces.d/ >"$dir/interfaces.d-ls.txt" 2>&1 || true

# --- Bridge state ---
bridge link show >"$dir/bridge-link.txt" 2>&1 || true
bridge fdb show >"$dir/bridge-fdb.txt" 2>&1 || true

# --- Kernel messages: current boot ---
journalctl -b 0 -k --no-pager >"$dir/journal-b0-kernel.txt" 2>&1
# Filtered NIC-relevant lines for quick reading
journalctl -b 0 -k --no-pager | grep -iE 'igc|atlantic|enp1[012]|eth[0-9]|link.*(up|down|change)|carrier|can.t claim|BAR|bridge window|NWAY' >"$dir/journal-b0-nic.txt" 2>&1 || true

# --- Kernel messages: previous boot ---
journalctl -b -1 -k --no-pager >"$dir/journal-b1-kernel.txt" 2>&1 || true
journalctl -b -1 -k --no-pager | grep -iE 'igc|atlantic|enp1[012]|eth[0-9]|link.*(up|down|change)|carrier|can.t claim|BAR|bridge window|NWAY' >"$dir/journal-b1-nic.txt" 2>&1 || true

# --- Full dmesg (current) ---
dmesg >"$dir/dmesg.txt" 2>&1

# --- Boot list ---
journalctl --list-boots >"$dir/boot-list.txt" 2>&1

# --- SATA (for cross-reference with chipset issues) ---
journalctl -b 0 -k --no-pager | grep -iE 'ata[0-9]|ahci|SError' >"$dir/journal-b0-sata.txt" 2>&1 || true

# --- PCIe link status for both NICs ---
for dev in 0000:0b:00.0 0000:0c:00.0; do
  lspci -vvv -s "$dev" >"$dir/lspci-${dev}.txt" 2>&1 || true
done

# --- NIC sysfs ---
for iface in enp11s0 enp12s0; do
  cat "/sys/class/net/${iface}/operstate" >"$dir/sysfs-${iface}-operstate.txt" 2>&1 || true
  cat "/sys/class/net/${iface}/carrier" >"$dir/sysfs-${iface}-carrier.txt" 2>&1 || true
  cat "/sys/class/net/${iface}/speed" >"$dir/sysfs-${iface}-speed.txt" 2>&1 || true
  readlink -f "/sys/class/net/${iface}/device" >"$dir/sysfs-${iface}-device.txt" 2>&1 || true
done

# --- Atlantic firmware version ---
cat /sys/class/net/enp12s0/device/fw_version >"$dir/atlantic-fw-version.txt" 2>&1 || true

# --- System info ---
uname -a >"$dir/uname.txt" 2>&1
cat /proc/version >"$dir/proc-version.txt" 2>&1
cat /proc/cmdline >"$dir/proc-cmdline.txt" 2>&1
dmidecode -t bios >"$dir/bios-version.txt" 2>&1 || true

echo "Done. Snapshot in $dir/"
echo "Files:"
ls -lh "$dir/"
