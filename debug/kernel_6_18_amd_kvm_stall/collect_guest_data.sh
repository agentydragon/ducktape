#!/bin/bash
# Collect diagnostic data from a KVM guest for the stall test matrix.
# Usage: collect_guest_data.sh [output_dir]
set -euo pipefail

OUT=${1:-/tmp/test-results}
mkdir -p "$OUT"

echo "Collecting data to $OUT at $(date -u)..."

dmesg >"$OUT/dmesg.txt"
journalctl --no-pager -b >"$OUT/journal.txt" 2>/dev/null || true
journalctl -k --no-pager -b >"$OUT/journal_kernel.txt" 2>/dev/null || true
cat /proc/interrupts >"$OUT/interrupts.txt"
cat /proc/cmdline >"$OUT/cmdline.txt"
cat /proc/cpuinfo >"$OUT/cpuinfo.txt"
uname -a >"$OUT/version.txt"
vmstat 1 10 >"$OUT/vmstat.txt" 2>/dev/null || true
cat /proc/softirqs >"$OUT/softirqs.txt"
: >"$OUT/cpu_bugs.txt"
for f in /sys/devices/system/cpu/vulnerabilities/*; do
  echo "$(basename "$f"): $(cat "$f")" >>"$OUT/cpu_bugs.txt"
done
lsmod >"$OUT/modules.txt" 2>/dev/null || true
uptime >"$OUT/uptime.txt"
cat /proc/meminfo >"$OUT/meminfo.txt"
cat /proc/stat >"$OUT/stat.txt"

# Summary for quick inspection
echo "=== Quick Summary ===" >"$OUT/summary.txt"
uname -a >>"$OUT/summary.txt"
echo "NMI counts:" >>"$OUT/summary.txt"
grep NMI /proc/interrupts >>"$OUT/summary.txt"
echo "RCU stalls: $(dmesg | grep -c 'rcu.*stall\|RCU.*stall' || echo 0)" >>"$OUT/summary.txt"
echo "NMI messages: $(dmesg | grep -ci nmi || echo 0)" >>"$OUT/summary.txt"
echo "Uptime: $(uptime)" >>"$OUT/summary.txt"

echo "Collection done at $(date -u)"
