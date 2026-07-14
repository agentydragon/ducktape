#!/usr/bin/env bash
# Verify the deployed Rugged route policy on a Google Fi-only underlay.
# This does not change routes or interfaces; sudo is used only for tcpdump.
set -euo pipefail

underlay_iface="${UNDERLAY_IFACE:-wwan0}"
nebula_iface="${NEBULA_IFACE:-nebula1}"
capture_seconds="${CAPTURE_SECONDS:-25}"
dump_capture="${DUMP_CAPTURE:-0}"
timestamp="$(date +%Y%m%dT%H%M%S)"
pcap="/tmp/rugged-nebula-fi-mtu-verify.$timestamp.pcap"
log="/tmp/rugged-nebula-fi-mtu-verify.$timestamp.log"
capture_pid=""

die() {
  echo "error: $*" >&2
  exit 1
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [ -n "$capture_pid" ] && kill -0 "$capture_pid" 2>/dev/null; then
    sudo -n kill -TERM "$capture_pid" 2>/dev/null || true
    wait "$capture_pid" 2>/dev/null || true
  fi
  exit "$status"
}

[ "$EUID" -ne 0 ] || die "run this script as your normal user, not through sudo"
for command in curl ip ping sudo tcpdump timeout; do
  command -v "$command" >/dev/null || die "missing command: $command"
done

exec > >(tee "$log") 2>&1
trap cleanup EXIT INT TERM

default_route="$(ip -4 route show default | head -n 1)"
[[ "$default_route" == *" dev $underlay_iface "* ]] \
  || die "$underlay_iface is not the active default route: $default_route"
underlay_mtu="$(cat "/sys/class/net/$underlay_iface/mtu")"
[ "$underlay_mtu" = 1200 ] \
  || die "$underlay_iface is not MTU 1200"
[ "$(cat "/sys/class/net/$nebula_iface/mtu")" = 1420 ] \
  || die "$nebula_iface is not MTU 1420"

route_count="$(ip -4 route show table main | awk '$1 ~ /^10\.42\.0\./ && /mtu 1100/ && /advmss 1060/ { count++ } END { print count + 0 }')"
[ "$route_count" -eq 9 ] || die "expected 9 conservative peer routes, found $route_count"

underlay_ip="$(ip -4 -o addr show dev "$underlay_iface" | awk 'NR == 1 { sub(/\/.*/, "", $4); print $4 }')"
[ -n "$underlay_ip" ] || die "could not determine $underlay_iface IPv4 address"

endpoint_filter='(dst host 147.135.37.175 or dst host 147.135.39.162 or dst host 147.135.39.176 or dst host 147.135.104.5 or dst host 147.135.104.16)'
capture_filter="src host $underlay_ip and $endpoint_filter and (udp port 4242 or (ip[6:2] & 0x1fff != 0))"

cat <<EOF
===== LIVE STATE =====
time: $(date --iso-8601=seconds)
default route: $default_route
$underlay_iface address: $underlay_ip
$underlay_iface MTU: $underlay_mtu
$nebula_iface MTU: 1420
conservative peer routes: $route_count
capture: $pcap
EOF

sudo -v
sudo timeout --signal=INT --kill-after=2s "${capture_seconds}s" \
  tcpdump -ni "$underlay_iface" -nn -s 128 -w "$pcap" "$capture_filter" &
capture_pid=$!
sleep 1

echo
echo "===== GENERATE DIRECT AND RELAY TRAFFIC ====="
ping -4 -I "$nebula_iface" -M do -c 3 -W 3 -s 1072 10.42.0.13
# Atlas currently uses a relayed path, exercising Nebula's larger relay header.
ping -4 -I "$nebula_iface" -M do -c 3 -W 1 -s 1072 10.42.0.5 || true
curl --interface "$nebula_iface" --connect-timeout 3 --max-time 15 \
  --silent --show-error --output /dev/null \
  --write-out 'wyrm2 http=%{http_code} bytes=%{size_download} total=%{time_total}s\n' \
  http://10.42.0.20:9100/metrics
curl --connect-timeout 3 --max-time 30 --silent --show-error --output /dev/null \
  --write-out 'cluster http=%{http_code} bytes=%{size_download} total=%{time_total}s\n' \
  http://10.100.244.108:8080/metrics

wait "$capture_pid" || true
capture_pid=""
sudo chown "$(id -u):$(id -g)" "$pcap"

echo
echo "===== OUTBOUND NEBULA CAPTURE SUMMARY ====="
read -r packet_count max_ip_length direct_max relay_max nonzero_offsets \
  more_fragments df_packets < <(
    tcpdump -nn -tt -v -r "$pcap" 2>/dev/null \
      | awk '
      /^[0-9]/ && / IP / {
        packets++
        packet_length = $NF
        gsub(/\)/, "", packet_length)
        packet_length += 0
        if (packet_length > max_length) max_length = packet_length
        if (packet_length == 1160) direct_max++
        if (packet_length == 1192) relay_max++
        if ($0 !~ /offset 0,/) nonzero_offsets++
        if ($0 ~ /flags \[\+\]/) more_fragments++
        if ($0 ~ /flags \[DF\]/) df_packets++
      }
      END {
        printf "%d %d %d %d %d %d %d\n", packets, max_length,
          direct_max, relay_max, nonzero_offsets, more_fragments, df_packets
      }
    '
  )

cat <<EOF
packets: $packet_count
maximum outer IPv4 length: $max_ip_length
1160-byte direct packets: $direct_max
1192-byte relayed packets: $relay_max
nonzero fragment offsets: $nonzero_offsets
more-fragments flags: $more_fragments
DF packets: $df_packets
EOF

[ "$packet_count" -gt 0 ] || die "capture contains no outbound Nebula packets"
[ "$max_ip_length" -le "$underlay_mtu" ] \
  || die "outer packet exceeded the $underlay_mtu-byte underlay MTU"
[ "$direct_max" -gt 0 ] || die "did not capture a full-size direct packet"
[ "$relay_max" -gt 0 ] || die "did not capture a full-size relayed packet"
[ "$nonzero_offsets" -eq 0 ] || die "capture contains non-initial fragments"
[ "$more_fragments" -eq 0 ] || die "capture contains initial fragments"
[ "$df_packets" -eq "$packet_count" ] || die "not every outer packet had DF set"

if [ "$dump_capture" = 1 ]; then
  echo
  echo "===== FULL OUTBOUND NEBULA CAPTURE ====="
  tcpdump -nn -tt -v -r "$pcap" 2>/dev/null
fi

echo
echo "Capture complete. Files:"
echo "  $log"
echo "  $pcap"
