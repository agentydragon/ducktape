#!/usr/bin/env bash
# wyrm2 IO error phase 2: ZFS DIO bug vs hardware investigation
# Run on atlas as root. Does NOT resume the VM.
set -uo pipefail

OUTDIR="/tmp/wyrm2-diag-phase2-$(date +%Y%m%d-%H%M%S)"
NVME_PCI="02:00.0"
NVME_DEV="/dev/disk/by-id/nvme-Sabrent_SB-RKT5-4TB_48836385700861"
TESTDIR="/var/lib/vz/images/9999"

mkdir -p "$OUTDIR"
echo "=== Phase 2 diagnostics: $OUTDIR ==="

# ============================================================
# 1. ZFS version and DIO configuration
# ============================================================
echo "[1/9] ZFS version and DIO config..."
{
  echo "--- zfs version ---"
  zfs version
  echo ""
  echo "--- module version ---"
  cat /sys/module/zfs/version
  echo ""
  echo "--- module parameters (dio related) ---"
  for p in /sys/module/zfs/parameters/*dio*; do
    [ -e "$p" ] && echo "$(basename "$p") = $(cat "$p")"
  done
  echo ""
  echo "--- module parameters (all) ---"
  for p in /sys/module/zfs/parameters/*; do
    [ -e "$p" ] && echo "$(basename "$p") = $(cat "$p" 2>/dev/null)"
  done
  echo ""
  echo "--- direct property on affected dataset ---"
  zfs get direct rpool/var-lib-vz
  echo ""
  echo "--- direct property on all datasets ---"
  zfs get direct -r rpool
} >"$OUTDIR/01_zfs_version_dio.txt" 2>&1

# ============================================================
# 2. PCIe link status and error counters
# ============================================================
echo "[2/9] PCIe link and error counters..."
{
  echo "--- lspci -vvv $NVME_PCI ---"
  lspci -vvv -s "$NVME_PCI"
  echo ""
  echo "--- full PCIe topology ---"
  lspci -tv
  echo ""
  echo "--- AER counters (sysfs) ---"
  AER_PATH="/sys/bus/pci/devices/0000:$NVME_PCI"
  for f in "$AER_PATH"/aer_*; do
    [ -e "$f" ] && echo "$(basename "$f"): $(cat "$f")"
  done
  echo ""
  echo "--- PCIe device link info ---"
  cat "$AER_PATH/current_link_speed" 2>/dev/null && echo ""
  cat "$AER_PATH/current_link_width" 2>/dev/null && echo ""
  cat "$AER_PATH/max_link_speed" 2>/dev/null && echo ""
  cat "$AER_PATH/max_link_width" 2>/dev/null && echo ""
  echo ""
  echo "--- upstream bridge/root port PCIe errors ---"
  # Walk up to the root port and check its AER too
  PARENT=$(basename "$(readlink -f "$AER_PATH/..")")
  echo "Parent: $PARENT"
  lspci -vvv -s "$PARENT" 2>/dev/null
  echo ""
  # Check root port AER
  for dev in /sys/bus/pci/devices/0000:00:*/; do
    if lspci -s "$(basename "$dev" | sed 's/0000://')" 2>/dev/null | grep -qi "root port\|pci bridge"; then
      echo "=== Root port: $(basename "$dev") ==="
      lspci -vvv -s "$(basename "$dev" | sed 's/0000://')" 2>/dev/null | grep -iE "LnkSta|UESta|CESta|DevSta|AERCap|Correctable|Uncorrectable"
      echo ""
    fi
  done
} >"$OUTDIR/02_pcie.txt" 2>&1

# ============================================================
# 3. NVMe detailed health and error log
# ============================================================
echo "[3/9] NVMe SMART and error logs..."
{
  echo "--- smartctl -x (all info) ---"
  smartctl -x "$NVME_DEV"
  echo ""
  echo "--- nvme smart-log ---"
  nvme smart-log "${NVME_DEV}n1" 2>/dev/null || nvme smart-log /dev/nvme0n1 2>/dev/null
  echo ""
  echo "--- nvme error-log ---"
  nvme error-log "${NVME_DEV}n1" 2>/dev/null || nvme error-log /dev/nvme0n1 2>/dev/null
  echo ""
  echo "--- nvme id-ctrl ---"
  nvme id-ctrl "${NVME_DEV}n1" 2>/dev/null || nvme id-ctrl /dev/nvme0n1 2>/dev/null
  echo ""
  echo "--- nvme fw-log ---"
  nvme fw-log "${NVME_DEV}n1" 2>/dev/null || nvme fw-log /dev/nvme0n1 2>/dev/null
} >"$OUTDIR/03_nvme_health.txt" 2>&1

# ============================================================
# 4. Start NVMe extended self-test (background, non-blocking)
# ============================================================
echo "[4/9] Starting NVMe extended self-test..."
{
  smartctl -t long "$NVME_DEV" 2>&1 || echo "Self-test start failed"
  echo ""
  echo "Check results later with: smartctl -l selftest $NVME_DEV"
} >"$OUTDIR/04_selftest_start.txt" 2>&1

# ============================================================
# 5. ZFS scrub (start, non-blocking)
# ============================================================
echo "[5/9] Starting ZFS scrub on rpool..."
{
  # Check if scrub already running
  zpool status rpool | grep -i scan
  echo ""
  zpool scrub rpool 2>&1 || echo "Scrub start failed"
  echo "Scrub started. Monitor with: zpool status rpool"
} >"$OUTDIR/05_scrub_start.txt" 2>&1

# ============================================================
# 6. fio verify test — DIO path (O_DIRECT on ZFS)
# ============================================================
echo "[6/9] fio DIO verify test (O_DIRECT, 128K bs matching recordsize)..."
{
  FIOFILE="$TESTDIR/.fio_dio_verify_test"
  echo "--- fio direct=1 verify=crc32c bs=128k size=4G ---"
  echo "This exercises the same ZFS DIO path that failed."
  echo ""
  fio --name=dio_verify \
    --filename="$FIOFILE" \
    --rw=write \
    --bs=128k \
    --size=4G \
    --direct=1 \
    --verify=crc32c \
    --verify_backlog=1024 \
    --do_verify=1 \
    --ioengine=libaio \
    --iodepth=16 \
    --numjobs=1 \
    --group_reporting \
    --output-format=normal 2>&1
  FIORC=$?
  echo ""
  echo "fio exit code: $FIORC"
  rm -f "$FIOFILE"
} >"$OUTDIR/06_fio_dio_verify.txt" 2>&1

# ============================================================
# 7. fio verify test — buffered path (non-DIO for comparison)
# ============================================================
echo "[7/9] fio buffered verify test (no O_DIRECT)..."
{
  FIOFILE="$TESTDIR/.fio_buf_verify_test"
  echo "--- fio direct=0 verify=crc32c bs=128k size=4G ---"
  echo "Comparison: same test through normal buffered path."
  echo ""
  fio --name=buf_verify \
    --filename="$FIOFILE" \
    --rw=write \
    --bs=128k \
    --size=4G \
    --direct=0 \
    --verify=crc32c \
    --verify_backlog=1024 \
    --do_verify=1 \
    --ioengine=libaio \
    --iodepth=16 \
    --numjobs=1 \
    --group_reporting \
    --output-format=normal 2>&1
  FIORC=$?
  echo ""
  echo "fio exit code: $FIORC"
  rm -f "$FIOFILE"
} >"$OUTDIR/07_fio_buf_verify.txt" 2>&1

# ============================================================
# 8. fio verify — multiple passes to increase chance of catching intermittent
# ============================================================
echo "[8/9] fio DIO verify stress (5 loops, 128K aligned, 2G)..."
{
  FIOFILE="$TESTDIR/.fio_stress_test"
  echo "--- fio direct=1 verify=crc32c loops=5 size=2G ---"
  echo ""
  fio --name=dio_stress \
    --filename="$FIOFILE" \
    --rw=randwrite \
    --bs=128k \
    --size=2G \
    --direct=1 \
    --verify=crc32c \
    --verify_backlog=256 \
    --do_verify=1 \
    --loops=5 \
    --ioengine=libaio \
    --iodepth=32 \
    --numjobs=4 \
    --group_reporting \
    --output-format=normal 2>&1
  FIORC=$?
  echo ""
  echo "fio exit code: $FIORC"
  rm -f "$FIOFILE"
} >"$OUTDIR/08_fio_stress.txt" 2>&1

# ============================================================
# 9. Check for new ZFS events after tests
# ============================================================
echo "[9/9] Post-test ZFS events and scrub progress..."
{
  echo "--- zpool events -v (new events since start) ---"
  zpool events -v 2>&1
  echo ""
  echo "--- scrub progress ---"
  zpool status rpool | grep -A5 scan
  echo ""
  echo "--- zpool status -v ---"
  zpool status -v rpool
} >"$OUTDIR/09_post_test.txt" 2>&1

echo ""
echo "=== Done. Results in: $OUTDIR ==="
ls -lh "$OUTDIR"/
echo ""
echo "Pending background tasks:"
echo "  - NVMe self-test: smartctl -l selftest $NVME_DEV"
echo "  - ZFS scrub: zpool status rpool | grep scan"
