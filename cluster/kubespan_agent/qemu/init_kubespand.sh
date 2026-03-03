#!/bin/busybox sh
# Init script for QEMU kubespand integration test VM.
# Boots into a minimal environment, loads kernel modules (WireGuard + nftables),
# configures networking, runs kubespand, and verifies tunnel connectivity.
#
# Kernel command-line parameters:
#   role=a|b                - VM role (determines IP addresses and ports)
#   cluster_id=BASE64       - KubeSpan cluster ID
#   shared_secret=BASE64    - KubeSpan shared secret
#   discovery=HOST:PORT     - Discovery service endpoint

# Mount essential filesystems.
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sys /sys
/bin/busybox mount -t devtmpfs dev /dev
/bin/busybox mkdir -p /tmp /var/lib/kubespan /etc/kubespan /run

# Suppress kernel messages on console (keep output clean for test parsing).
/bin/busybox dmesg -n 1

# Parse kernel command line.
ROLE=""
CLUSTER_ID=""
SHARED_SECRET=""
DISCOVERY=""
for arg in $(/bin/busybox cat /proc/cmdline); do
  case "$arg" in
    role=*) ROLE="${arg#role=}" ;;
    cluster_id=*) CLUSTER_ID="${arg#cluster_id=}" ;;
    shared_secret=*) SHARED_SECRET="${arg#shared_secret=}" ;;
    discovery=*) DISCOVERY="${arg#discovery=}" ;;
  esac
done

if [ -z "$ROLE" ] || [ -z "$CLUSTER_ID" ] || [ -z "$SHARED_SECRET" ] || [ -z "$DISCOVERY" ]; then
  echo "QEMU_TEST: ERROR missing kernel cmdline params (role=$ROLE cluster_id=$CLUSTER_ID discovery=$DISCOVERY)"
  echo "o" >/proc/sysrq-trigger
  /bin/busybox sleep 5
  exit 1
fi

echo "QEMU_TEST: role=$ROLE"

# Assign addresses based on role.
case "$ROLE" in
  a)
    LINK_IP="192.168.50.1"
    LISTEN_PORT=51820
    ;;
  b)
    LINK_IP="192.168.50.2"
    LISTEN_PORT=51821
    ;;
  *)
    echo "QEMU_TEST: ERROR unknown role=$ROLE"
    echo "o" >/proc/sysrq-trigger
    /bin/busybox sleep 5
    exit 1
    ;;
esac

# Load kernel modules using modprobe (full modules tree with modules.dep).
KVER=$(/bin/busybox ls /lib/modules/ | /bin/busybox head -1)
if [ -z "$KVER" ]; then
  echo "QEMU_TEST: ERROR no kernel modules found in /lib/modules/"
  echo "o" >/proc/sysrq-trigger
  /bin/busybox sleep 5
  exit 1
fi
echo "QEMU_TEST: kernel modules version=$KVER"

# Load nftables module chain.
# crc32c_generic must be loaded before libcrc32c (provides the CRC32C algorithm).
# The dependency isn't always in modules.dep, so load explicitly.
echo "QEMU_TEST: loading nftables modules..."
/bin/busybox modprobe crc32c_generic 2>/dev/null || true
/bin/busybox modprobe nf_tables
if [ $? -ne 0 ]; then
  echo "QEMU_TEST: WARN modprobe nf_tables failed"
fi

# Load WireGuard module (modprobe resolves dependency chain).
echo "QEMU_TEST: loading wireguard..."
/bin/busybox modprobe wireguard
if [ $? -ne 0 ]; then
  echo "QEMU_TEST: WARN modprobe wireguard failed"
fi

# Load virtio_net for network interfaces.
/bin/busybox modprobe virtio_net 2>/dev/null || true

echo "QEMU_TEST: modules loaded"

# List loaded modules for diagnostics.
/bin/busybox lsmod 2>/dev/null || true

# Configure networking.
# eth0 = user NIC (QEMU slirp, gateway at 10.0.2.2)
# eth1 = socket NIC (static IP for VM-to-VM communication)

# Wait for network interfaces to appear.
echo "QEMU_TEST: waiting for network interfaces..."
TRIES=0
while [ ! -e /sys/class/net/eth0 ] && [ "$TRIES" -lt 50 ]; do
  /bin/busybox sleep 0.2
  TRIES=$((TRIES + 1))
done

if [ ! -e /sys/class/net/eth0 ]; then
  echo "QEMU_TEST: ERROR eth0 not found after 10s"
  /bin/busybox ls /sys/class/net/ 2>/dev/null
  echo "o" >/proc/sysrq-trigger
  /bin/busybox sleep 5
  exit 1
fi

/bin/busybox ip link set lo up
/bin/busybox ip link set eth0 up
/bin/busybox ip addr add 10.0.2.15/24 dev eth0
/bin/busybox ip route add default via 10.0.2.2

# Wait for eth1 (socket NIC).
TRIES=0
while [ ! -e /sys/class/net/eth1 ] && [ "$TRIES" -lt 50 ]; do
  /bin/busybox sleep 0.2
  TRIES=$((TRIES + 1))
done

if [ ! -e /sys/class/net/eth1 ]; then
  echo "QEMU_TEST: ERROR eth1 not found after 10s"
  /bin/busybox ls /sys/class/net/ 2>/dev/null
  echo "o" >/proc/sysrq-trigger
  /bin/busybox sleep 5
  exit 1
fi

/bin/busybox ip link set eth1 up
/bin/busybox ip addr add "${LINK_IP}/24" dev eth1

echo "QEMU_TEST: networking configured (eth0=10.0.2.15, eth1=$LINK_IP)"
/bin/busybox ip addr show 2>/dev/null

# Write kubespand config.
# extra_endpoints advertises this VM's eth1 address so peers can reach it via
# the QEMU socket mcast network. Without this, the only endpoint discovered is
# 127.0.0.1 (from the discovery service's perspective via QEMU slirp NAT).
/bin/busybox cat >/etc/kubespan/agent.yaml <<YAML
cluster_id: "$CLUSTER_ID"
shared_secret: "$SHARED_SECRET"
discovery_endpoint: "$DISCOVERY"
insecure_discovery: true
force_routing: true
listen_port: $LISTEN_PORT
mtu: 1420
identity_file: /var/lib/kubespan/identity.yaml
machine_type: worker
extra_endpoints:
  - "${LINK_IP}:${LISTEN_PORT}"
endpoint_filters:
  - "192.168.50.0/24"
YAML

echo "QEMU_TEST: kubespand config written"
/bin/busybox cat /etc/kubespan/agent.yaml

# Start kubespand in the background.
/kubespand -config /etc/kubespan/agent.yaml -debug >/tmp/kubespand.log 2>&1 &
KUBESPAND_PID=$!
echo "QEMU_TEST: kubespand started (pid=$KUBESPAND_PID)"

# Wait for kubespand to discover and configure a peer.
PEER_ADDR=""
DEADLINE=180 # seconds
ELAPSED=0
while [ "$ELAPSED" -lt "$DEADLINE" ]; do
  # Check if kubespand is still running.
  if ! /bin/busybox kill -0 $KUBESPAND_PID 2>/dev/null; then
    echo "QEMU_TEST: ERROR kubespand exited prematurely"
    echo "QEMU_TEST: kubespand log:"
    /bin/busybox cat /tmp/kubespand.log 2>/dev/null
    echo "QEMU_TEST: FAIL role=$ROLE"
    echo "o" >/proc/sysrq-trigger
    /bin/busybox sleep 5
    exit 1
  fi

  if [ -f /tmp/kubespand.log ]; then
    # Look for "configuring peer" log line and extract the address field.
    # kubespand uses zap development format (tab-separated fields with JSON).
    # busybox grep -o has limited regex support, so use awk for extraction.
    if /bin/busybox grep -q "configuring peer" /tmp/kubespand.log 2>/dev/null; then
      # zap JSON uses "address": "value" (space after colon).
      PEER_ADDR=$(/bin/busybox grep "configuring peer" /tmp/kubespand.log 2>/dev/null | /bin/busybox tail -1 | /bin/busybox awk -F'"address": "' '{print $2}' | /bin/busybox cut -d'"' -f1)
      if [ -n "$PEER_ADDR" ]; then
        echo "QEMU_TEST: peer discovered address=$PEER_ADDR"
        break
      fi
    fi
  fi
  /bin/busybox sleep 2
  ELAPSED=$((ELAPSED + 2))

  # Print progress every 10 seconds.
  if [ $((ELAPSED % 10)) -eq 0 ]; then
    echo "QEMU_TEST: waiting for peer discovery (${ELAPSED}s/${DEADLINE}s)..."
    /bin/busybox tail -3 /tmp/kubespand.log 2>/dev/null
  fi
done

if [ -z "$PEER_ADDR" ]; then
  echo "QEMU_TEST: ERROR timed out waiting for peer discovery (${DEADLINE}s)"
  echo "QEMU_TEST: kubespand log:"
  /bin/busybox cat /tmp/kubespand.log 2>/dev/null
  echo "QEMU_TEST: FAIL role=$ROLE"
  /bin/busybox kill $KUBESPAND_PID 2>/dev/null || true
  echo "o" >/proc/sysrq-trigger
  /bin/busybox sleep 5
  exit 1
fi

# Only VM-A runs the connectivity probe. VM-B just stays alive.
if [ "$ROLE" = "a" ]; then
  echo "QEMU_TEST: probing peer at $PEER_ADDR"
  if /testprobe -ebusy-retry -timeout 60s "$PEER_ADDR"; then
    echo "QEMU_TEST: PASS connectivity to $PEER_ADDR"
  else
    echo "QEMU_TEST: FAIL connectivity to $PEER_ADDR"
    echo "QEMU_TEST: kubespand log:"
    /bin/busybox cat /tmp/kubespand.log 2>/dev/null
  fi
else
  # VM-B: stay alive until killed or for 180s (enough for VM-A to probe).
  echo "QEMU_TEST: role=b waiting for probe (180s max)"
  /bin/busybox sleep 180 &
  SLEEP_PID=$!
  wait $SLEEP_PID 2>/dev/null || true
fi

# Clean up.
/bin/busybox kill $KUBESPAND_PID 2>/dev/null || true
echo "o" >/proc/sysrq-trigger
/bin/busybox sleep 5
