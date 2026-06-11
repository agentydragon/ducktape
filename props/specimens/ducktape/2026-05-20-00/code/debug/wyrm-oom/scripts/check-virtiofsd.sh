#!/usr/bin/env bash
# Check virtiofsd processes and their memory/FD usage

set -euo pipefail

HOST="root@atlas"

echo "=== virtiofsd Processes ==="
ssh "$HOST" 'ps aux | grep virtiofsd | grep -v grep' || echo "No virtiofsd processes found"

echo -e "\n=== File Descriptor Counts ==="
for pid in $(ssh "$HOST" 'pgrep virtiofsd'); do
  cmd=$(ssh "$HOST" "ps -p $pid -o cmd= | head -c 80")
  fd_count=$(ssh "$HOST" "ls /proc/$pid/fd 2>/dev/null | wc -l" || echo "0")
  mem_kb=$(ssh "$HOST" "ps -p $pid -o rss= 2>/dev/null" || echo "0")
  mem_mb=$(echo "scale=2; $mem_kb / 1024" | bc)
  mem_gb=$(echo "scale=2; $mem_kb / 1024 / 1024" | bc)

  echo "PID $pid:"
  echo "  Command: $cmd"
  echo "  FDs: $fd_count"
  echo "  Memory: ${mem_mb} MB (${mem_gb} GB)"

  if [ "$fd_count" -gt 100000 ]; then
    echo "  ⚠️  WARNING: Very high FD count (>100k) - likely memory leak!"
  elif [ "$fd_count" -gt 10000 ]; then
    echo "  ⚠️  WARNING: High FD count (>10k)"
  else
    echo "  ✓ FD count normal"
  fi

  if [ "$(echo "$mem_gb > 2" | bc)" -eq 1 ]; then
    echo "  ⚠️  WARNING: High memory usage (>2GB)"
  else
    echo "  ✓ Memory usage acceptable"
  fi

  echo ""
done

echo "=== VM 100 virtiofs Configuration ==="
ssh "$HOST" 'qm config 100 | grep virtiofs'

echo -e "\n=== Checking if cache policy is active ==="
code_has_cache=$(ssh "$HOST" 'ps aux | grep "virtiofsd.*code" | grep -o -- "--cache[^ ]*"' || echo "")
if [ -z "$code_has_cache" ]; then
  echo "✗ WARNING: /code virtiofsd has NO --cache flag (using default 'auto')"
  echo "   Fix: qm set 100 --virtiofs1 code,cache=metadata && qm shutdown 100 && qm start 100"
else
  echo "✓ /code virtiofsd cache policy: $code_has_cache"
fi

share_has_cache=$(ssh "$HOST" 'ps aux | grep "virtiofsd.*tankshare" | grep -o -- "--cache[^ ]*"' || echo "")
if [ -z "$share_has_cache" ]; then
  echo "✗ WARNING: /tankshare virtiofsd has NO --cache flag (using default 'auto')"
  echo "   Fix: qm set 100 --virtiofs0 tankshare,cache=metadata && qm shutdown 100 && qm start 100"
else
  echo "✓ /tankshare virtiofsd cache policy: $share_has_cache"
fi
