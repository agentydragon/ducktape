#!/usr/bin/env bash
# Check current memory state of atlas host and VMs

set -euo pipefail

HOST="root@atlas"

echo "=== Host Memory Status ==="
ssh "$HOST" 'free -h'

echo -e "\n=== ZFS ARC Status ==="
ssh "$HOST" 'cat /proc/spl/kstat/zfs/arcstats | grep -E "^size|^c_max" | awk "{printf \"%-20s %s\\n\", \$1\":\", \$3}"'

echo -e "\n=== Swap Status ==="
if ssh "$HOST" 'swapon --show' 2>/dev/null; then
  echo "✓ Swap is configured"
else
  echo "✗ WARNING: No swap configured!"
fi

echo -e "\n=== VM Memory Allocations ==="
ssh "$HOST" 'qm list' | awk 'NR==1 || $1 ~ /^[0-9]/ {printf "%-8s %-15s %-10s %s\n", $1, $2, $3, $4}'

echo -e "\n=== Top Memory Consumers (Host) ==="
ssh "$HOST" 'ps aux --sort=-%mem | head -11' | awk 'NR==1 {print $0} NR>1 {printf "%-8s %6s %6s %-60s\n", $2, $4, $6, substr($0, index($0,$11))}'

echo -e "\n=== VM 100 (wyrm) Details ==="
ssh "$HOST" 'qm config 100 | grep -E "memory:|cores:"'

echo -e "\n=== Total Allocation Estimate ==="
VM_MEM=$(ssh "$HOST" "qm list | awk 'NR>1 {sum+=\$4} END {print sum/1024}'")
ARC_GB=$(ssh "$HOST" "cat /sys/module/zfs/parameters/zfs_arc_max | awk '{print \$1/1024/1024/1024}'")
echo "VMs: ${VM_MEM} GB"
echo "ZFS ARC max: ${ARC_GB} GB"
echo "Estimated overhead: ~4 GB"
echo "Total: ~$(echo "$VM_MEM + $ARC_GB + 4" | bc) GB"
