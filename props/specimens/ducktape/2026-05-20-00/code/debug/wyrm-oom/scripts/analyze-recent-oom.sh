#!/usr/bin/env bash
# Analyze the most recent OOM kill event in detail

set -euo pipefail

HOST="root@atlas"

echo "=== Analyzing Recent OOM Event ==="
echo ""

# Get current time and VM start time to determine when OOM happened
echo "Current host time:"
ssh "$HOST" 'date'

echo -e "\nVM 100 status:"
ssh "$HOST" 'qm status 100'

echo -e "\nHost uptime:"
ssh "$HOST" 'uptime'

echo -e "\n=== Recent OOM Kills ==="
if ! ssh "$HOST" 'dmesg -T | grep "killed process"' | tail -20; then
  echo "No OOM kills found in dmesg"
  exit 0
fi

echo -e "\n=== Last OOM Event Details ==="

# Find the last OOM kill
last_oom_line=$(ssh "$HOST" 'dmesg -T | grep -n "killed process" | tail -1')
if [ -z "$last_oom_line" ]; then
  echo "No OOM events found"
  exit 0
fi

line_num=$(echo "$last_oom_line" | cut -d: -f1)
last_oom_time=$(echo "$last_oom_line" | sed 's/.*\[/\[/' | cut -d']' -f1 | tr -d '[]')
killed_process=$(echo "$last_oom_line" | sed 's/.*killed process //' | cut -d'(' -f1)
killed_pid=$(echo "$last_oom_line" | grep -o 'process [0-9]*' | grep -o '[0-9]*')

echo "Time: $last_oom_time"
echo "Killed process: $killed_process (PID: $killed_pid)"

echo -e "\n=== Memory State Leading to OOM ==="
# Show context before the OOM kill (memory stats, pressure, etc.)
ssh "$HOST" "dmesg -T | sed -n '$(($line_num - 100)),${line_num}p'" \
  | grep -E 'Out of memory|Mem-Info|Node [0-9]|Normal:|MemTotal|MemFree|MemAvailable|total_vm|rss|oom_score_adj' \
  | tail -50

echo -e "\n=== Processes at Time of OOM (by memory) ==="
# Show what was consuming memory
ssh "$HOST" "dmesg -T | sed -n '$(($line_num - 100)),${line_num}p'" \
  | grep -E '\[ *[0-9]+\].*total_vm' \
  | tail -20

echo -e "\n=== virtiofsd Processes at OOM ==="
ssh "$HOST" "dmesg -T | sed -n '$(($line_num - 100)),${line_num}p'" \
  | grep -E 'virtiofsd' \
  | tail -10

echo -e "\n=== System Memory Totals at OOM ==="
ssh "$HOST" "dmesg -T | sed -n '$(($line_num - 50)),${line_num}p'" \
  | grep -E 'MemTotal:|MemFree:|MemAvailable:' \
  | tail -10

echo -e "\n=== Current virtiofsd State (After Restart) ==="
echo "virtiofsd processes:"
for pid in $(ssh "$HOST" 'pgrep virtiofsd'); do
  cmd=$(ssh "$HOST" "ps -p $pid -o cmd= | head -c 80")
  fd_count=$(ssh "$HOST" "ls /proc/$pid/fd 2>/dev/null | wc -l" || echo "0")
  mem_kb=$(ssh "$HOST" "ps -p $pid -o rss= 2>/dev/null" || echo "0")
  mem_mb=$(echo "scale=2; $mem_kb / 1024" | bc)

  echo "PID $pid: $fd_count FDs, ${mem_mb} MB"
  echo "  $cmd"
done

echo -e "\n=== Current System Memory ==="
ssh "$HOST" 'free -h'

echo -e "\n=== VM Config ==="
ssh "$HOST" 'qm config 100 | grep -E "memory:|cores:|virtiofs"'

echo -e "\n=== Summary ==="
echo "Last OOM occurred at: $last_oom_time"
echo "Killed: $killed_process (PID $killed_pid)"
echo ""
echo "Check lines above for memory usage of virtiofsd and other processes at time of OOM"
