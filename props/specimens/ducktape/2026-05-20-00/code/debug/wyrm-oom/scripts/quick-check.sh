#!/usr/bin/env bash
# Quick health check - run this first

set -euo pipefail

HOST="root@atlas"

echo "🔍 Quick Health Check for atlas/wyrm"
echo "======================================"

# Memory
mem_avail=$(ssh "$HOST" "free | awk 'NR==2 {print \$7}'")
mem_total=$(ssh "$HOST" "free | awk 'NR==2 {print \$2}'")
mem_pct=$(echo "scale=1; 100 - ($mem_avail * 100 / $mem_total)" | bc)
echo "📊 Memory Usage: ${mem_pct}%"
if [ "$(echo "$mem_pct > 90" | bc)" -eq 1 ]; then
  echo "   ⚠️  CRITICAL: Memory usage over 90%"
elif [ "$(echo "$mem_pct > 80" | bc)" -eq 1 ]; then
  echo "   ⚠️  WARNING: Memory usage over 80%"
else
  echo "   ✓ Memory usage acceptable"
fi

# Swap
if ssh "$HOST" 'swapon --show' >/dev/null 2>&1; then
  swap_used=$(ssh "$HOST" "free | awk 'NR==3 {print \$3}'")
  swap_total=$(ssh "$HOST" "free | awk 'NR==3 {print \$2}'")
  if [ "$swap_total" -gt 0 ]; then
    swap_pct=$(echo "scale=1; $swap_used * 100 / $swap_total" | bc)
    echo "💾 Swap: ${swap_pct}% used"
  fi
else
  echo "💾 Swap: ✗ NOT CONFIGURED"
  echo "   ⚠️  WARNING: No swap! System has no OOM buffer"
fi

# virtiofsd status
virtiofsd_count=$(ssh "$HOST" 'pgrep -c virtiofsd' 2>/dev/null || echo "0")
echo "🗂️  virtiofsd processes: $virtiofsd_count"

if [ "$virtiofsd_count" -gt 0 ]; then
  max_fds=0
  max_mem=0

  for pid in $(ssh "$HOST" 'pgrep virtiofsd'); do
    fd_count=$(ssh "$HOST" "ls /proc/$pid/fd 2>/dev/null | wc -l" || echo "0")
    mem_kb=$(ssh "$HOST" "ps -p $pid -o rss= 2>/dev/null" || echo "0")
    mem_gb=$(echo "scale=2; $mem_kb / 1024 / 1024" | bc)

    if [ "$fd_count" -gt "$max_fds" ]; then
      max_fds=$fd_count
    fi

    if [ "$(echo "$mem_gb > $max_mem" | bc)" -eq 1 ]; then
      max_mem=$mem_gb
    fi
  done

  echo "   Max FDs: $max_fds"
  if [ "$max_fds" -gt 100000 ]; then
    echo "   ⚠️  CRITICAL: Very high FD count - memory leak likely!"
  elif [ "$max_fds" -gt 10000 ]; then
    echo "   ⚠️  WARNING: High FD count"
  else
    echo "   ✓ FD count normal"
  fi

  echo "   Max Memory: ${max_mem} GB"
  if [ "$(echo "$max_mem > 5" | bc)" -eq 1 ]; then
    echo "   ⚠️  CRITICAL: Very high memory usage"
  elif [ "$(echo "$max_mem > 2" | bc)" -eq 1 ]; then
    echo "   ⚠️  WARNING: High memory usage"
  else
    echo "   ✓ Memory usage acceptable"
  fi

  # Check cache policy
  if ssh "$HOST" 'ps aux | grep virtiofsd | grep -q -- "--cache"'; then
    echo "   ✓ Cache policy configured"
  else
    echo "   ✗ No cache policy (using default 'auto')"
    echo "   ⚠️  WARNING: This causes memory leaks!"
  fi
fi

# Recent OOM events
oom_count=$(ssh "$HOST" 'dmesg -T | grep -c "killed process"' 2>/dev/null || echo "0")
echo "🔪 OOM kills in dmesg: $oom_count"
if [ "$oom_count" -gt 0 ]; then
  last_oom=$(ssh "$HOST" 'dmesg -T | grep "killed process" | tail -1' | awk '{print $1, $2, $3}')
  echo "   Last OOM: $last_oom"
fi

# VM status
vm_status=$(ssh "$HOST" 'qm status 100 | grep "status:"' | awk '{print $2}')
echo "🖥️  VM 100 (wyrm): $vm_status"

echo ""
echo "======================================"
echo "For detailed diagnostics, run:"
echo "  ./check-memory.sh      # Memory details"
echo "  ./check-virtiofsd.sh   # virtiofsd analysis"
echo "  ./check-oom-history.sh # OOM history"
