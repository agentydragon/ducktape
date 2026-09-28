#!/usr/bin/env bash
# Prove whether a smaller Rugged-side per-peer Nebula route MTU makes Google Fi
# traffic more reliable without changing the nebula1 device MTU.
#
# Run this as the normal desktop user while Google Fi is the active uplink. The
# script asks sudo only for one temporary /32 route, restores it on every exit
# path, and writes the complete transcript under /tmp.
set -euo pipefail

nebula_iface="${NEBULA_IFACE:-nebula1}"
underlay_iface="${UNDERLAY_IFACE:-wwan0}"
probe_mtu="${PROBE_MTU:-1100}"
pass_payload="$((probe_mtu - 28))"
target_peer="${TARGET_PEER:-10.42.0.20}"
control_peer="${CONTROL_PEER:-10.42.0.13}"
cluster_url="${CLUSTER_URL:-http://10.100.244.108:8080/metrics}"
log="/tmp/rugged-nebula-fi-mtu-probe.$(date +%Y%m%dT%H%M%S).log"
added_target_route=false

die() {
  echo "error: $*" >&2
  exit 1
}

restore() {
  local status=$?
  trap - EXIT INT TERM

  if $added_target_route; then
    sudo -n ip route del "$target_peer/32" dev "$nebula_iface" 2>/dev/null \
      || echo "WARNING: could not remove temporary route $target_peer/32" >&2
  fi
  echo
  echo "===== RESTORED STATE ====="
  ip -br link show dev "$nebula_iface" || true
  ip route get "$target_peer" || true
  echo "log: $log"
  exit "$status"
}

fragment_counters() {
  nstat -asz IpFragOKs IpFragFails IpFragCreates
}

run_peer_transfer() {
  local label=$1
  local peer=$2
  local max_time=${3:-12}

  echo "$label"
  curl \
    --interface "$nebula_iface" \
    --connect-timeout 3 \
    --max-time "$max_time" \
    --silent \
    --show-error \
    --output /dev/null \
    --write-out 'http=%{http_code} bytes=%{size_download} connect=%{time_connect}s total=%{time_total}s speed=%{speed_download}B/s\n' \
    "http://$peer:9100/metrics" || true
}

run_cluster_transfer() {
  local label=$1

  echo "$label"
  curl \
    --connect-timeout 3 \
    --max-time 30 \
    --silent \
    --show-error \
    --output /dev/null \
    --write-out 'http=%{http_code} bytes=%{size_download} connect=%{time_connect}s total=%{time_total}s speed=%{speed_download}B/s\n' \
    "$cluster_url" || true
}

run_http_phase() {
  local label=$1

  echo
  echo "===== $label ====="
  echo "fragment counters before:"
  fragment_counters
  sudo ip tcp_metrics delete "$target_peer" 2>/dev/null || true
  for attempt in 1 2 3; do
    run_peer_transfer "target peer attempt $attempt" "$target_peer"
  done
  run_peer_transfer "unmodified control peer" "$control_peer"
  run_cluster_transfer "large Cilium ClusterIP transfer"
  echo "fragment counters after:"
  fragment_counters
}

ssh_options=(
  -o BatchMode=yes
  -o ConnectTimeout=5
  -o ConnectionAttempts=1
  -o ServerAliveInterval=5
  -o ServerAliveCountMax=2
)
ssh_available=false

run_ssh_streams() {
  local label=$1

  echo
  echo "===== $label ====="
  if [ "$ssh_available" != true ]; then
    echo "Skipping streams: passwordless SSH to $USER@$target_peer is unavailable."
    return
  fi

  echo "Rugged -> target peer 1 MiB SSH stream (30 second limit)"
  if ! dd if=/dev/zero bs=1M count=1 status=none \
    | timeout --kill-after=2s 30s \
      ssh "${ssh_options[@]}" -o Compression=no "$USER@$target_peer" wc -c; then
    echo "upload failed or exceeded 30 seconds"
  fi
  echo "Target peer -> Rugged 1 MiB SSH stream (30 second limit)"
  if ! timeout --kill-after=2s 30s \
    ssh "${ssh_options[@]}" -o Compression=no "$USER@$target_peer" \
    dd if=/dev/zero bs=1M count=1 status=none | wc -c; then
    echo "download failed or exceeded 30 seconds"
  fi
}

[ "$EUID" -ne 0 ] || die "run this script as your normal user, not through sudo"
for command in curl ip nstat ping sudo timeout; do
  command -v "$command" >/dev/null || die "missing command: $command"
done

exec > >(tee "$log") 2>&1
trap restore EXIT INT TERM

ip link show dev "$underlay_iface" >/dev/null 2>&1 || die "no such underlay interface: $underlay_iface"
ip link show dev "$nebula_iface" >/dev/null 2>&1 || die "no such Nebula interface: $nebula_iface"
default_route="$(ip -4 route show default | head -n 1)"
[[ "$default_route" == *" dev $underlay_iface "* ]] \
  || die "$underlay_iface is not the active IPv4 default route: $default_route"
[ -z "$(ip -4 route show exact "$target_peer/32")" ] \
  || die "an exact route for $target_peer/32 already exists; refusing to overwrite it"

original_nebula_mtu="$(cat "/sys/class/net/$nebula_iface/mtu")"
nebula_ip="$(ip -4 -o addr show dev "$nebula_iface" | awk 'NR == 1 { sub(/\/.*/, "", $4); print $4 }')"
[ -n "$nebula_ip" ] || die "could not determine the IPv4 address of $nebula_iface"

cat <<EOF
===== INITIAL STATE =====
time: $(date --iso-8601=seconds)
default route: $default_route
$underlay_iface MTU: $(cat "/sys/class/net/$underlay_iface/mtu")
$nebula_iface address: $nebula_ip
$nebula_iface MTU: $original_nebula_mtu
temporary probe MTU: $probe_mtu
target peer: $target_peer
unmodified control peer: $control_peer
large ClusterIP URL: $cluster_url
EOF
ip route get "$target_peer"

sudo -v
if command -v ssh >/dev/null \
  && timeout --kill-after=2s 15s ssh "${ssh_options[@]}" "$USER@$target_peer" true; then
  ssh_available=true
fi

run_http_phase "BASELINE WITH ORDINARY /16 ROUTE"
run_ssh_streams "BASELINE SSH STREAMS"

echo
echo "===== APPLY TEMPORARY ROUTE MTU ====="
sudo -v
added_target_route=true
sudo ip -4 route add table main "$target_peer/32" dev "$nebula_iface" \
  scope link proto static mtu "$probe_mtu" advmss "$((probe_mtu - 40))"
ip route get "$target_peer"

run_http_phase "WITH TARGET /32 MTU $probe_mtu"
run_ssh_streams "SSH STREAMS WITH TARGET /32 MTU $probe_mtu"

echo
echo "===== TARGET DF-PING BOUNDARY ====="
echo "Payload + 28 bytes = inner IPv4 packet size. MTU $probe_mtu should pass"
echo "payload $pass_payload exactly and reject $((pass_payload + 8)) locally."
for payload in "$((pass_payload - 72))" "$pass_payload" "$((pass_payload + 8))"; do
  echo "payload=$payload total=$((payload + 28))"
  ping -4 -I "$nebula_iface" -M do -c 2 -W 3 -s "$payload" "$target_peer" || true
done

echo
echo "===== OPTIONAL REVERSE-DIRECTION TESTS ====="
if [ "$ssh_available" = true ]; then
  echo "Remote peer is reachable over SSH; testing packets sent toward Rugged."
  for payload in 1000 1072 1200 1392; do
    echo "remote payload=$payload total=$((payload + 28))"
    timeout --kill-after=2s 20s \
      ssh "${ssh_options[@]}" "$USER@$target_peer" \
      ping -4 -M do -c 2 -W 3 -s "$payload" "$nebula_ip" || true
  done

else
  echo "Skipping reverse tests: passwordless SSH to $USER@$target_peer is unavailable."
fi

echo
echo "===== REMOVE TEMPORARY ROUTE ====="
sudo ip -4 route del table main "$target_peer/32" dev "$nebula_iface"
added_target_route=false
ip route get "$target_peer"

run_http_phase "RESTORED ORDINARY /16 ROUTE"

echo
echo "Probe complete; no temporary route remains."
