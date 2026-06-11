#!/usr/bin/env bash
# Continuously monitor memory state and log to files
# Run this in a tmux/screen session for long-term monitoring

set -euo pipefail

HOST="root@atlas"
LOG_DIR="$(dirname "$0")/../logs"
INTERVAL=60 # seconds between checks

mkdir -p "$LOG_DIR"

echo "=== Continuous Memory Monitoring ==="
echo "Logging to: $LOG_DIR"
echo "Interval: ${INTERVAL}s"
echo "Press Ctrl+C to stop"
echo ""

while true; do
  timestamp=$(date '+%Y-%m-%d_%H-%M-%S')
  log_file="$LOG_DIR/${timestamp}_memory-snapshot.txt"

  {
    echo "=== Snapshot at $(date) ==="
    echo ""

    echo "Host Memory:"
    ssh "$HOST" 'free -h'
    echo ""

    echo "virtiofsd Processes:"
    for pid in $(ssh "$HOST" 'pgrep virtiofsd' 2>/dev/null || echo ""); do
      if [ -n "$pid" ]; then
        cmd=$(ssh "$HOST" "ps -p $pid -o cmd= | head -c 60")
        fd_count=$(ssh "$HOST" "ls /proc/$pid/fd 2>/dev/null | wc -l" || echo "0")
        mem_kb=$(ssh "$HOST" "ps -p $pid -o rss= 2>/dev/null" || echo "0")
        mem_mb=$(echo "scale=2; $mem_kb / 1024" | bc)

        echo "PID $pid: $fd_count FDs, ${mem_mb} MB - $cmd"
      fi
    done
    echo ""

    echo "Top 5 Memory Consumers:"
    ssh "$HOST" 'ps aux --sort=-%mem | head -6 | tail -5' \
      | awk '{printf "%s %6s %6s %s\n", $2, $4, $6, substr($0, index($0,$11))}'
    echo ""

    echo "ZFS ARC:"
    arc_size=$(ssh "$HOST" "cat /proc/spl/kstat/zfs/arcstats | grep '^size' | awk '{print \$3/1024/1024/1024}' | xargs printf '%.2f'")
    arc_max=$(ssh "$HOST" "cat /sys/module/zfs/parameters/zfs_arc_max | awk '{print \$1/1024/1024/1024}' | xargs printf '%.2f'")
    echo "Current: ${arc_size} GB, Max: ${arc_max} GB"
    echo ""

  } >"$log_file"

  # Print summary to terminal
  mem_avail=$(ssh "$HOST" "free | awk 'NR==2 {print \$7}'")
  mem_total=$(ssh "$HOST" "free | awk 'NR==2 {print \$2}'")
  mem_pct=$(echo "scale=1; 100 - ($mem_avail * 100 / $mem_total)" | bc)

  echo "[$timestamp] Memory: ${mem_pct}% used | Log: $(basename "$log_file")"

  # Check for concerning conditions
  if [ "$(echo "$mem_pct > 90" | bc)" -eq 1 ]; then
    echo "  ⚠️  WARNING: Memory usage over 90%"

    # Also save OOM history
    oom_log="$LOG_DIR/${timestamp}_oom-events.txt"
    ssh "$HOST" 'dmesg -T | grep "killed process" | tail -20' >"$oom_log" 2>&1 || true
  fi

  sleep "$INTERVAL"
done
