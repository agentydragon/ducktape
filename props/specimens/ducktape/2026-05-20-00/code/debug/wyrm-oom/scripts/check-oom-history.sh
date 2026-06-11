#!/usr/bin/env bash
# Extract and analyze OOM kill events from dmesg

set -euo pipefail

HOST="root@atlas"

echo "=== Recent OOM Kill Events ==="
if ssh "$HOST" 'dmesg -T | grep -i "killed process"' | tail -20; then
  echo ""
else
  echo "✓ No OOM kills found in current dmesg buffer"
fi

echo "=== OOM Kill Statistics ==="
oom_count=$(ssh "$HOST" 'dmesg -T | grep -c "killed process"' || echo "0")
echo "Total OOM kills in dmesg: $oom_count"

if [ "$oom_count" -gt 0 ]; then
  echo -e "\n=== Most Recently Killed Processes ==="
  ssh "$HOST" 'dmesg -T | grep "killed process" | tail -10' \
    | sed 's/.*process \([0-9]*\) (\([^)]*\)).*/Process: \2 (PID: \1)/' | uniq -c

  echo -e "\n=== Last OOM Event Details ==="
  last_oom_time=$(ssh "$HOST" 'dmesg -T | grep "killed process" | tail -1' | awk '{print $1, $2, $3}')
  echo "Last OOM kill: $last_oom_time"

  echo -e "\n=== Memory State at Last OOM ==="
  # Get the line number of the last OOM kill
  ssh "$HOST" 'dmesg -T | grep -n "killed process" | tail -1 | cut -d: -f1' \
    | {
      read line_num
      # Show context around that OOM event (memory stats, etc.)
      ssh "$HOST" "dmesg -T | sed -n '$(($line_num - 50)),$(($line_num + 10))p' | grep -E 'Out of memory|oom_kill|total_vm|anon-rss|file-rss|shmem-rss|pgtables|killed process'"
    }
fi

echo -e "\n=== VM Uptime (to correlate with OOM events) ==="
ssh "$HOST" 'qm status 100 | grep -E "status|uptime"'

echo -e "\n=== Host Uptime ==="
ssh "$HOST" 'uptime'
