#!/usr/bin/env bash
# Run on wyrm2 to diagnose UI freezes
# Comprehensive dump — GPU, memory, I/O, processes, kernel state
set -uo pipefail

echo "========================================"
echo "wyrm2 diagnostic dump $(date)"
echo "========================================"

echo ""
echo "=== GPU: nvidia-smi ==="
nvidia-smi 2>&1 || echo "FAILED"

echo ""
echo "=== GPU: nvidia-smi detailed ==="
nvidia-smi -q 2>&1 || echo "FAILED"

echo ""
echo "=== GPU: driver version ==="
cat /proc/driver/nvidia/version 2>/dev/null || echo "n/a"
modinfo nvidia 2>/dev/null | head -20 || echo "n/a"

echo ""
echo "=== GPU: PCIe devices ==="
lspci 2>/dev/null || echo "n/a"

echo ""
echo "=== GPU: PCIe link speed/width ==="
nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.width.current,pcie.link.gen.max,pcie.link.width.max --format=csv 2>/dev/null || echo "n/a"

echo ""
echo "=== GPU: TTM errors ==="
dmesg -T 2>/dev/null | grep -i 'TTM' | tail -50

echo ""
echo "=== GPU: NVIDIA/Xid errors ==="
dmesg -T 2>/dev/null | grep -iE 'nvidia|nvrm|gpu.*lock|gpu.*reset|Xid|vfio' | tail -50

echo ""
echo "=== GPU: DRM errors ==="
dmesg -T 2>/dev/null | grep -iE 'drm|nouveau' | tail -30

echo ""
echo "=== Memory ==="
free -h
echo ""
cat /proc/meminfo

echo ""
echo "=== Swap ==="
cat /proc/swaps 2>/dev/null || echo "no swap"

echo ""
echo "=== Load ==="
cat /proc/loadavg
uptime

echo ""
echo "=== PSI ==="
echo "--- memory ---"
cat /proc/pressure/memory
echo "--- io ---"
cat /proc/pressure/io
echo "--- cpu ---"
cat /proc/pressure/cpu

echo ""
echo "=== vmstat (5 samples, 1s apart) ==="
vmstat 1 5

echo ""
echo "=== Top 20 by RSS ==="
ps -eo pid,rss,%mem,%cpu,stat,comm --sort=-rss | head -21

echo ""
echo "=== Top 20 by CPU ==="
ps -eo pid,rss,%mem,%cpu,stat,comm --sort=-%cpu | head -21

echo ""
echo "=== Blocked processes (D state) ==="
ps -eo pid,stat,wchan:30,comm | grep ' D' || echo "none"

echo ""
echo "=== All processes in uninterruptible sleep ==="
for pid in $(ps -eo pid,stat | awk '$2~/D/{print $1}'); do
  echo "--- PID $pid ---"
  cat /proc/$pid/status 2>/dev/null | grep -E 'Name|State|Vm|Threads'
  cat /proc/$pid/stack 2>/dev/null || echo "no stack"
  echo ""
done

echo ""
echo "=== Disk I/O ==="
iostat -xm 1 3 2>/dev/null || cat /proc/diskstats | awk '$4+$8>0{print $3,$4,$8,$7,$11}'

echo ""
echo "=== Mount points ==="
mount | grep -vE 'cgroup|proc|sys|tmpfs|devpts|securityfs|debugfs|fusectl|configfs|pstore|efivarfs|bpf|tracefs|hugetlbfs|mqueue|nsfs'

echo ""
echo "=== virtiofs mounts ==="
mount | grep virtiofs

echo ""
echo "=== Filesystem usage ==="
df -h | grep -vE 'tmpfs|devtmpfs|overlay'

echo ""
echo "=== Kernel cmdline ==="
cat /proc/cmdline

echo ""
echo "=== Kernel version ==="
uname -a

echo ""
echo "=== IOMMU ==="
dmesg 2>/dev/null | grep -iE 'iommu|dmar|amd.vi' | head -20

echo ""
echo "=== Interrupts (top 20) ==="
sort -t: -k2 -rn /proc/interrupts 2>/dev/null | head -20 || cat /proc/interrupts | head -20

echo ""
echo "=== Softirqs ==="
cat /proc/softirqs 2>/dev/null | head -15

echo ""
echo "=== Network interfaces ==="
ip addr show 2>/dev/null | grep -E 'inet |state' || ifconfig 2>/dev/null

echo ""
echo "=== dmesg full errors/warnings (last 100) ==="
dmesg -T 2>/dev/null | grep -iE "error|warn|oom|fault|hang|stall|blocked|watchdog|TTM|Xid|fail|panic|bug|reset" | tail -100

echo ""
echo "=== dmesg last 50 lines ==="
dmesg -T 2>/dev/null | tail -50

echo ""
echo "=== systemd failed units ==="
systemctl --failed 2>/dev/null || echo "n/a"

echo ""
echo "=== journalctl errors last hour ==="
journalctl -p err --since "1 hour ago" --no-pager 2>/dev/null | tail -50 || echo "n/a"

echo ""
echo "========================================"
echo "dump complete $(date)"
echo "========================================"
