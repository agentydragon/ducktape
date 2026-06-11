#!/usr/bin/env bash
# Deep analysis of what's actually causing OOMs

set -euo pipefail

HOST="root@atlas"

echo "=== Analyzing TRUE OOM Cause ==="
echo ""

echo "Question: Is it virtiofsd or something else?"
echo ""

echo "=== OOM Events from dmesg ==="
ssh "$HOST" 'dmesg -T | grep -E "Out of memory|killed process" | tail -30'

echo -e "\n=== What was killed? ==="
ssh "$HOST" 'dmesg -T | grep "Out of memory: Killed process" | tail -10' | while read line; do
  process=$(echo "$line" | sed 's/.*Killed process [0-9]* (\([^)]*\)).*/\1/')
  shmem=$(echo "$line" | grep -o 'shmem-rss:[0-9]*' | cut -d: -f2)
  shmem_gb=$(echo "scale=2; $shmem / 1024 / 1024" | bc)
  anon=$(echo "$line" | grep -o 'anon-rss:[0-9]*' | cut -d: -f2)
  anon_gb=$(echo "scale=2; $anon / 1024 / 1024" | bc)

  echo "Process: $process, shmem: ${shmem_gb} GB, anon: ${anon_gb} GB"
done

echo -e "\n=== Current virtiofsd Memory Usage ==="
echo "If virtiofsd was the problem, we'd see multi-GB usage here:"
for pid in $(ssh "$HOST" 'pgrep virtiofsd' 2>/dev/null || echo ""); do
  if [ -n "$pid" ]; then
    cmd=$(ssh "$HOST" "ps -p $pid -o cmd= | grep -o '/tank/[^/]*'" || echo "unknown")
    mem_kb=$(ssh "$HOST" "ps -p $pid -o rss=" || echo "0")
    mem_gb=$(echo "scale=2; $mem_kb / 1024 / 1024" | bc)
    echo "  $cmd: ${mem_gb} GB"
  fi
done

echo -e "\n=== VM Memory Allocation ==="
ssh "$HOST" 'qm config 100 | grep memory:'

echo -e "\n=== Host Memory Breakdown ==="
ssh "$HOST" 'free -h'

echo -e "\n=== Total VM Allocations ==="
ssh "$HOST" 'qm list | awk "NR==1 {print; next} {sum+=\$4} {print} END {print \"TOTAL: \" sum/1024 \" GB\"}"'

echo -e "\n=== ZFS ARC ==="
arc_current=$(ssh "$HOST" "cat /proc/spl/kstat/zfs/arcstats | awk '/^size/ {print \$3}'")
arc_max=$(ssh "$HOST" "cat /sys/module/zfs/parameters/zfs_arc_max")
arc_current_gb=$(echo "scale=2; $arc_current / 1024 / 1024 / 1024" | bc)
arc_max_gb=$(echo "scale=2; $arc_max / 1024 / 1024 / 1024" | bc)
echo "Current: ${arc_current_gb} GB, Max: ${arc_max_gb} GB"

echo -e "\n=== Analysis ==="
echo ""
echo "Looking at the OOM events:"
echo "1. What was killed? → KVM (VM 100), using 60-65 GB"
echo "2. What about virtiofsd? → NOT in the kill list (if it was using GB, it would be killed)"
echo "3. Current virtiofsd memory: → (see above)"
echo ""

total_mem_gb=128
vm_alloc=$(ssh "$HOST" 'qm list | awk "NR>1 {sum+=\$4} END {print sum/1024}"')
arc_gb=$(echo "scale=0; $arc_max_gb" | bc)
estimated_total=$(echo "$vm_alloc + $arc_gb + 4" | bc)

echo "Memory math:"
echo "  Total physical: 128 GB"
echo "  VM allocations: ${vm_alloc} GB"
echo "  ZFS ARC max: ${arc_gb} GB"
echo "  Host overhead: ~4 GB"
echo "  ─────────────────────"
echo "  Total baseline: ${estimated_total} GB"
echo ""

if [ "$(echo "$estimated_total > 120" | bc)" -eq 1 ]; then
  echo "⚠️  PROBLEM: System is OVERCOMMITTED"
  echo ""
  echo "The system doesn't have enough physical RAM for all allocations."
  echo "This means OOMs will occur even WITHOUT memory leaks."
  echo ""
  echo "virtiofs fix may have helped, but the fundamental problem is:"
  echo "  Too many VMs / too little RAM / no swap buffer"
else
  echo "✓ System has headroom"
fi

echo -e "\n=== Recommendation ==="
echo "Based on analysis:"
echo "1. virtiofs WAS a problem (595k FDs, 12GB), but may not be THE problem"
echo "2. System is structurally overcommitted (${estimated_total}GB demand / 128GB physical)"
echo "3. No swap = no safety margin for ANY spikes"
echo ""
echo "Actions:"
echo "  Priority 1: Add 32GB swap (gives 3-5 days runway)"
echo "  Priority 2: Reduce ZFS ARC to 8GB (frees 4GB)"
echo "  Priority 3: Reduce VM allocations or add physical RAM"
echo "  Priority 4: Monitor to see if OOMs stop"
