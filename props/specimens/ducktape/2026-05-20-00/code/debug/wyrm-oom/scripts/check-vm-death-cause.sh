#!/usr/bin/env bash
# Check what caused the VM to restart - OOM, manual, crash, etc.

set -euo pipefail

HOST="root@atlas"

echo "=== Investigating VM 100 (wyrm) Restart Cause ==="
echo ""

echo "Current time:"
ssh "$HOST" 'date'

echo -e "\nHost uptime:"
ssh "$HOST" 'uptime'

echo -e "\nVM 100 current status:"
ssh "$HOST" 'qm status 100'

echo -e "\n=== When did VM processes start? ==="
echo "KVM process for VM 100:"
kvm_start=$(ssh "$HOST" "ps -p \$(pgrep -f 'kvm.*-id 100') -o lstart=" 2>/dev/null || echo "Not running")
echo "Started: $kvm_start"

echo -e "\nvirtiofsd processes:"
for pid in $(ssh "$HOST" 'pgrep virtiofsd' 2>/dev/null || echo ""); do
  if [ -n "$pid" ]; then
    start=$(ssh "$HOST" "ps -p $pid -o lstart= 2>/dev/null" || echo "unknown")
    cmd=$(ssh "$HOST" "ps -p $pid -o cmd= | head -c 60" || echo "unknown")
    echo "PID $pid started: $start"
    echo "  $cmd"
  fi
done

echo -e "\n=== Check for OOM kills in dmesg (last hour) ==="
echo "Checking dmesg for OOM events..."
if ssh "$HOST" 'dmesg -T | grep "Out of memory\|killed process\|oom-kill" | tail -20' 2>/dev/null; then
  echo "Found OOM events above"
else
  echo "No OOM events in dmesg"
fi

echo -e "\n=== Check systemd journal for VM 100 events (last 2 hours) ==="
ssh "$HOST" 'journalctl -u qmeventd@100 --since "2 hours ago" --no-pager' 2>/dev/null \
  || ssh "$HOST" 'journalctl | grep "vm.*100\|wyrm" | tail -30' 2>/dev/null \
  || echo "No journal entries found for VM 100"

echo -e "\n=== Check for manual qm commands (last 2 hours) ==="
ssh "$HOST" 'journalctl --since "2 hours ago" | grep -E "qm (start|stop|shutdown|reboot)" | grep "100\|wyrm" | tail -20' 2>/dev/null \
  || echo "No qm commands found in journal"

echo -e "\n=== Check Proxmox task log ==="
ssh "$HOST" 'cat /var/log/pve/tasks/active 2>/dev/null | grep -E "100|wyrm" | tail -10' 2>/dev/null \
  || echo "No active tasks"

ssh "$HOST" 'ls -lt /var/log/pve/tasks/index | head -10' 2>/dev/null || echo "No task index"

echo -e "\n=== Check for OOM kills in systemd journal (last 3 hours) ==="
ssh "$HOST" 'journalctl --since "3 hours ago" | grep -i "out of memory\|oom\|killed" | tail -30' 2>/dev/null \
  || echo "No OOM events in journal"

echo -e "\n=== VM 100 Configuration ==="
ssh "$HOST" 'qm config 100 | grep -E "memory:|cores:|virtiofs"'

echo -e "\n=== System Memory Right Now ==="
ssh "$HOST" 'free -h'

echo -e "\n=== Estimate when VM restarted ==="
kvm_pid=$(ssh "$HOST" "pgrep -f 'kvm.*-id 100'" 2>/dev/null || echo "")
if [ -n "$kvm_pid" ]; then
  start_seconds=$(ssh "$HOST" "stat -c %Y /proc/$kvm_pid" 2>/dev/null || echo "0")
  current_seconds=$(ssh "$HOST" "date +%s")
  uptime_seconds=$((current_seconds - start_seconds))
  uptime_minutes=$((uptime_seconds / 60))
  uptime_hours=$((uptime_minutes / 60))

  echo "VM uptime: ~${uptime_hours}h ${uptime_minutes}m (${uptime_seconds}s)"

  # Calculate when it started
  ssh "$HOST" "date -d @$start_seconds"
else
  echo "Could not determine VM uptime"
fi

echo -e "\n=== Summary ==="
echo "Check above for:"
echo "- When virtiofsd/KVM processes started (shows restart time)"
echo "- Journal entries for VM 100 events"
echo "- OOM events in journal or dmesg"
echo "- Manual qm commands"
