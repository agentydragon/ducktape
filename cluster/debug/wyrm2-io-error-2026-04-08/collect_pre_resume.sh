#!/usr/bin/env bash
# wyrm2 IO error diagnostics — collect everything BEFORE resuming.
# Run on atlas as root. Does NOT resume the VM.
set -uo pipefail
# No set -e: we want to collect everything even if individual commands fail

VMID=110
QEMU_PID=548353
SCSI4_FILE="/var/lib/vz/images/9999/vm-9999-pvc-038045aa-adc2-4d79-8942-73d50e4e5597.raw"
OUTDIR="/tmp/wyrm2-diag-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$OUTDIR"
echo "=== Diagnostics output: $OUTDIR ==="

echo "[1/9] QEMU block status..."
qm monitor "$VMID" <<<"info block" >"$OUTDIR/01_block_info.txt" 2>&1

echo "[2/9] QEMU full VM status..."
qm status "$VMID" --verbose >"$OUTDIR/02_vm_status.txt" 2>&1

echo "[3/9] File stat + extent map..."
{
  echo "--- stat ---"
  stat "$SCSI4_FILE"
  echo "--- du (apparent) ---"
  du -h --apparent-size "$SCSI4_FILE"
  echo "--- du (actual) ---"
  du -h "$SCSI4_FILE"
  echo "--- filefrag (summary) ---"
  filefrag "$SCSI4_FILE"
  echo "--- filefrag (verbose, first+last 50 extents) ---"
  filefrag -v "$SCSI4_FILE" | head -55
  echo "..."
  filefrag -v "$SCSI4_FILE" | tail -55
} >"$OUTDIR/03_file_extents.txt" 2>&1

echo "[4/9] All scsi/virtio backing files stat..."
{
  for f in /var/lib/vz/images/9999/vm-9999-pvc-*.raw; do
    echo "=== $f ==="
    stat "$f"
    du -h "$f"
    du -h --apparent-size "$f"
    echo ""
  done
  for zvol in /dev/zvol/rpool/data/vm-110-*; do
    echo "=== $zvol ==="
    ls -l "$zvol"
    echo ""
  done
} >"$OUTDIR/04_all_disks.txt" 2>&1

echo "[5/9] ZFS state..."
{
  echo "--- zfs list -r rpool ---"
  zfs list -r rpool
  echo ""
  echo "--- zfs get all rpool/var-lib-vz ---"
  zfs get all rpool/var-lib-vz
  echo ""
  echo "--- zpool status -v ---"
  zpool status -v
  echo ""
  echo "--- zpool list ---"
  zpool list
  echo ""
  echo "--- zpool iostat rpool 1 3 ---"
  zpool iostat rpool 1 3
  echo ""
  echo "--- zpool events -v (full) ---"
  zpool events -v 2>&1
} >"$OUTDIR/05_zfs_state.txt" 2>&1

echo "[6/9] Manual write test (same directory, same filesystem)..."
{
  TESTFILE="/var/lib/vz/images/9999/.diag_write_test"
  echo "--- dd 1MB direct ---"
  dd if=/dev/zero of="$TESTFILE" bs=1M count=1 oflag=direct 2>&1
  rm -f "$TESTFILE"
  echo ""
  echo "--- fallocate 1G ---"
  fallocate -l 1G "$TESTFILE" 2>&1 && echo "OK" || echo "FAILED"
  rm -f "$TESTFILE"
} >"$OUTDIR/06_write_test.txt" 2>&1

echo "[7/9] lsof on scsi4 file..."
lsof "$SCSI4_FILE" >"$OUTDIR/07_lsof.txt" 2>&1 || true

echo "[8/9] Memory / swap / process state..."
{
  echo "--- free -h ---"
  free -h
  echo ""
  echo "--- swapon -s ---"
  swapon -s
  echo ""
  echo "--- /proc/meminfo ---"
  cat /proc/meminfo
  echo ""
  echo "--- vmstat 1 3 ---"
  vmstat 1 3
  echo ""
  echo "--- /proc/spl/kstat/zfs/arcstats ---"
  cat /proc/spl/kstat/zfs/arcstats
  echo ""
  echo "--- /proc/$QEMU_PID/status ---"
  cat /proc/$QEMU_PID/status
  echo ""
  echo "--- /proc/$QEMU_PID/maps (summary) ---"
  wc -l /proc/$QEMU_PID/maps
  cat /proc/$QEMU_PID/maps
  echo ""
  echo "--- /proc/$QEMU_PID/smaps_rollup ---"
  cat /proc/$QEMU_PID/smaps_rollup 2>/dev/null || echo "(not available)"
  echo ""
  echo "--- /proc/$QEMU_PID/oom_score ---"
  cat /proc/$QEMU_PID/oom_score
  echo ""
  echo "--- /proc/$QEMU_PID/oom_score_adj ---"
  cat /proc/$QEMU_PID/oom_score_adj
  echo ""
  echo "--- /proc/$QEMU_PID/fdinfo/663 (scsi4 fd) ---"
  cat /proc/$QEMU_PID/fdinfo/663 2>/dev/null || echo "(not available)"
  echo ""
  echo "--- ps aux ---"
  ps auxf
  echo ""
  echo "--- QEMU thread stacks ---"
  ls /proc/$QEMU_PID/task/ | while read tid; do
    echo "== TID $tid =="
    echo "stat: $(cat /proc/$QEMU_PID/task/$tid/stat 2>/dev/null)"
    echo "wchan: $(cat /proc/$QEMU_PID/task/$tid/wchan 2>/dev/null)"
    cat /proc/$QEMU_PID/task/$tid/stack 2>/dev/null
    echo ""
  done
} >"$OUTDIR/08_memory_procs.txt" 2>&1

echo "[9/9] Kernel logs..."
{
  echo "--- dmesg ---"
  dmesg
  echo ""
  echo "--- journalctl last 1h ---"
  journalctl --since "1 hour ago" --no-pager 2>&1
  echo ""
  echo "--- ZFS txg status ---"
  cat /proc/spl/kstat/zfs/rpool/txgs 2>/dev/null || echo "(not available)"
  echo ""
  echo "--- /proc/spl/kstat/zfs/rpool/state ---"
  cat /proc/spl/kstat/zfs/rpool/state 2>/dev/null || echo "(not available)"
} >"$OUTDIR/09_kernel_logs.txt" 2>&1

echo ""
echo "=== Done. Results in: $OUTDIR ==="
ls -lh "$OUTDIR"/
