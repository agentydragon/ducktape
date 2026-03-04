#!/usr/bin/env bash
# QEMU-based KubeSpan integration test runner.
# Boots two QEMU VMs running kubespand, connected via a virtual L2 network,
# with a discovery service on the host. Verifies WireGuard tunnel connectivity.
#
# Usage: run_integration_test.sh <vmlinuz> <initramfs> <discovery-tarball>
#
# Requires qemu-system-x86_64 on PATH (apt install qemu-system-x86).
#
# Architecture:
#   VM-A (192.168.50.1) ──── socket mcast ──── VM-B (192.168.50.2)
#          │                                           │
#          └──── user NIC (10.0.2.15) ────┐            └──── user NIC ────┐
#                                         ▼                               ▼
#                              discovery service (host:3000, --network=host)
#
# Exit codes:
#   0 = connectivity test passed
#   1 = test failed or QEMU exited abnormally
set -euo pipefail

VMLINUZ="$1"
INITRAMFS="$2"
DISCOVERY_TARBALL="$3"
QEMU="qemu-system-x86_64"

for f in "$VMLINUZ" "$INITRAMFS"; do
  if [ ! -f "$f" ]; then
    echo "ERROR: file not found: $f" >&2
    exit 1
  fi
done
if ! command -v "$QEMU" &>/dev/null; then
  echo "ERROR: $QEMU not found on PATH (apt install qemu-system-x86)" >&2
  exit 1
fi
if [ ! -f "$DISCOVERY_TARBALL" ]; then
  echo "ERROR: discovery service tarball not found: $DISCOVERY_TARBALL" >&2
  exit 1
fi

# Generate random cluster parameters.
CLUSTER_ID=$(head -c 32 /dev/urandom | base64 -w0)
SHARED_SECRET=$(head -c 32 /dev/urandom | base64 -w0)
TEST_ID=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')
MCAST_PORT=$((10000 + (RANDOM % 50000)))

# Discovery service port (fixed — runs with --network=host on default port).
DISC_PORT=3000

echo "=== KubeSpan QEMU Integration Test ==="
echo "test_id=$TEST_ID"
echo "discovery_port=$DISC_PORT"
echo "mcast_port=$MCAST_PORT"
echo ""

TMPDIR_BASE=$(mktemp -d)
trap 'cleanup' EXIT

VM_A_PID=""
VM_B_PID=""
DISC_CONTAINER=""

cleanup() {
  echo ""
  echo "=== Cleanup ==="
  # Kill QEMU processes.
  for pid in $VM_A_PID $VM_B_PID; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  # Remove discovery service container.
  if [ -n "$DISC_CONTAINER" ]; then
    docker rm -f "$DISC_CONTAINER" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMPDIR_BASE"
}

# ─ Start discovery service via Docker ─────────────────────────────────────────
echo "Loading discovery service image..."
docker load <"$DISCOVERY_TARBALL" >/dev/null 2>&1

DISC_CONTAINER="kubespan-disc-$TEST_ID"
echo "Starting discovery service (--network=host, port $DISC_PORT)..."
# Use --network=host to avoid Docker port mapping (which requires ip_forward=1,
# not available in gVisor sandboxes). The discovery service listens on :3000.
docker run -d --name "$DISC_CONTAINER" \
  --network=host \
  "ghcr.io/siderolabs/discovery-service:latest" \
  -debug >/dev/null 2>&1

# Wait for discovery service to be ready (check port instead of docker exec,
# since the image is FROM scratch and has no shell).
echo "Waiting for discovery service..."
for i in $(seq 1 30); do
  if curl -sf --connect-timeout 1 "http://localhost:$DISC_PORT/" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
echo "Discovery service started (container=$DISC_CONTAINER)"

# ─ Build QEMU command lines ──────────────────────────────────────────────────
# Each VM gets:
#   NIC 0: user networking (host access at 10.0.2.2)
#   NIC 1: socket mcast (VM-to-VM L2 network at 192.168.50.0/24)

qemu_cmd() {
  local role="$1"
  local output="$2"
  local mac0="$3"
  local mac1="$4"

  "$QEMU" \
    -kernel "$VMLINUZ" \
    -initrd "$INITRAMFS" \
    -append "console=ttyS0 panic=-1 quiet mode=kubespan role=$role cluster_id=$CLUSTER_ID shared_secret=$SHARED_SECRET discovery=10.0.2.2:$DISC_PORT" \
    -nographic \
    -no-reboot \
    -m 512 \
    -machine "accel=tcg" \
    -cpu max \
    -display none \
    -netdev "user,id=net0" \
    -device "virtio-net-pci,netdev=net0,mac=$mac0" \
    -netdev "socket,id=net1,mcast=230.0.0.1:$MCAST_PORT" \
    -device "virtio-net-pci,netdev=net1,mac=$mac1" \
    >"$output" 2>&1
}

VM_A_OUT="$TMPDIR_BASE/vm-a.log"
VM_B_OUT="$TMPDIR_BASE/vm-b.log"

# ─ Boot VM-B first (it just waits for probes) ────────────────────────────────
# Each VM needs unique MAC addresses so kubespand generates distinct identities
# (the KubeSpan address is derived from the MAC via EUI-64).
echo "Booting VM-B (role=b)..."
qemu_cmd "b" "$VM_B_OUT" "52:54:00:b0:00:01" "52:54:00:b1:00:01" &
VM_B_PID=$!

# Small delay to avoid mcast race.
sleep 1

# ─ Boot VM-A (runs probe after discovering peer) ─────────────────────────────
echo "Booting VM-A (role=a)..."
qemu_cmd "a" "$VM_A_OUT" "52:54:00:a0:00:01" "52:54:00:a1:00:01" &
VM_A_PID=$!

echo "Waiting for VM-A to complete (up to 300s)..."

# Wait for VM-A to finish (it runs the connectivity probe and powers off).
DEADLINE=$((SECONDS + 300))
VM_A_DONE=false
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  if ! kill -0 "$VM_A_PID" 2>/dev/null; then
    VM_A_DONE=true
    break
  fi
  # Print progress every 30s.
  if [ $((SECONDS % 30)) -eq 0 ] && [ -f "$VM_A_OUT" ]; then
    LAST_TEST_LINE=$(grep "QEMU_TEST:" "$VM_A_OUT" 2>/dev/null | tail -1 || true)
    if [ -n "$LAST_TEST_LINE" ]; then
      echo "  [${SECONDS}s] VM-A: $LAST_TEST_LINE"
    fi
  fi
  sleep 2
done

# Kill VM-B (it runs indefinitely waiting for probes).
if kill -0 "$VM_B_PID" 2>/dev/null; then
  kill "$VM_B_PID" 2>/dev/null || true
  wait "$VM_B_PID" 2>/dev/null || true
fi

wait "$VM_A_PID" 2>/dev/null || true

echo ""
echo "=== VM-A Output ==="
cat "$VM_A_OUT"

echo ""
echo "=== VM-B Output ==="
cat "$VM_B_OUT"

# Dump discovery service logs on failure.
dump_discovery_logs() {
  echo ""
  echo "=== Discovery Service Logs ==="
  docker logs "$DISC_CONTAINER" 2>&1 | tail -50 || true
}

echo ""
echo "=== Test Results ==="

if ! $VM_A_DONE; then
  echo "FAILED: VM-A did not complete within timeout"
  dump_discovery_logs
  exit 1
fi

# Check VM-A output for test markers.
if grep -q "QEMU_TEST: PASS connectivity" "$VM_A_OUT"; then
  echo "KubeSpan QEMU integration test PASSED"
  exit 0
fi

if grep -q "QEMU_TEST: FAIL" "$VM_A_OUT"; then
  echo "KubeSpan QEMU integration test FAILED"
  dump_discovery_logs
  exit 1
fi

echo "ERROR: VM-A did not produce expected test markers"
dump_discovery_logs
exit 1
