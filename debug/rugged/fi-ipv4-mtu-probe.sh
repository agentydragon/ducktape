#!/usr/bin/env bash
# Revalidate rugged's historical direct Google Fi IPv4 PMTU observation.
#
# This does NOT alter the persistent NetworkManager profile. It temporarily
# raises wwan0's runtime MTU from the declared 1200 to 1436, probes DF-ping
# sizes through that interface, and restores the original MTU on every exit.
# Keep Wi-Fi connected while running it. Expect a brief interruption to any
# traffic intentionally using Fi. This does not exercise the larger
# Cilium-over-Nebula cluster overlay; see debug/rugged/network.md.
set -euo pipefail

iface="${1:-wwan0}"
probe_mtu="${PROBE_MTU:-1436}"
original_mtu=""

die() {
  echo "error: $*" >&2
  exit 1
}

restore() {
  if [ -n "$original_mtu" ]; then
    sudo ip link set dev "$iface" mtu "$original_mtu" || true
    echo "restored $iface MTU to $original_mtu"
  fi
}

trap restore EXIT INT TERM

for command in ip ping sudo; do
  command -v "$command" >/dev/null || die "missing command: $command"
done

ip link show dev "$iface" >/dev/null 2>&1 || die "no such interface: $iface"
original_mtu="$(cat "/sys/class/net/$iface/mtu")"

cat <<EOF
This will temporarily set $iface MTU from $original_mtu to $probe_mtu and send
IPv4 ICMP echo requests with Don't Fragment set. It changes no persistent
configuration and restores the original MTU even if interrupted.
EOF
read -r -p 'Type probe to continue: ' confirmation
[ "$confirmation" = probe ] || die "not confirmed"

sudo -v
sudo ip link set dev "$iface" mtu "$probe_mtu"
ip -br link show dev "$iface"

printf '\nPayload + 28 bytes = total IPv4 packet size.\n'
for destination in 8.8.8.8 1.1.1.1; do
  printf '\n===== %s via %s =====\n' "$destination" "$iface"
  for payload in 1172 1200 1228 1252 1272 1300 1372; do
    total=$((payload + 28))
    output="$(ping -4 -I "$iface" -M do -c 2 -W 3 -s "$payload" "$destination" 2>&1 || true)"
    if grep -q '2 received' <<<"$output"; then
      printf 'total=%4s payload=%4s PASS\n' "$total" "$payload"
    else
      printf 'total=%4s payload=%4s FAIL: %s\n' "$total" "$payload" \
        "$(tail -n 2 <<<"$output" | tr '\n' ' ')"
    fi
  done
done

cat <<'EOF'

Interpretation:
- A repeatable ceiling below 1280 across both destinations confirms the IPv4
  PMTU problem is still present.
- Success at 1280 or above means the old IPv4 measurement is stale or
  destination-specific. It still does not prove native Fi IPv6 works; that
  requires a separate, temporary IPv4v6-bearer test.
EOF
