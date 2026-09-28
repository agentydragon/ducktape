#!/usr/bin/env bash
# Diagnose Google Fi's actual egress path on rugged without changing connection
# profiles, DNS, routes, or modem state. Run as the logged-in user; it invokes
# sudo only for a tightly filtered tcpdump capture.
set -euo pipefail

iface="${1:-wwan0}"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

die() {
  echo "error: $*" >&2
  exit 1
}

section() {
  printf '\n===== %s =====\n' "$*"
}

ipv4_for() {
  getent ahostsv4 "$1" | awk 'NR == 1 { print $1 }'
}

for command in bpftool curl getent ip resolvectl ssh sudo tc tcpdump timeout; do
  command -v "$command" >/dev/null || die "missing command: $command"
done

ip link show dev "$iface" >/dev/null 2>&1 || die "no such interface: $iface"
ip_addr="$(ip -4 -o addr show dev "$iface" | awk 'NR == 1 { print $4 }' | cut -d/ -f1)"
[ -n "$ip_addr" ] || die "$iface has no IPv4 address"

# Obtain the credential before launching tcpdump in the background, so the
# capture cannot get stuck waiting for a password prompt.
sudo -v

section "Baseline"
printf 'interface=%s ipv4=%s\n' "$iface" "$ip_addr"
ip -br addr show dev "$iface"
ip route get 8.8.8.8 oif "$iface"
resolvectl --cache=no -i "$iface" -4 -t A query github.com
resolvectl --cache=no -i "$iface" -6 -t AAAA query github.com || true

section "Overlay isolation on the Fi device"
echo '-- policy routing --'
ip rule show
echo '-- BPF programs attached directly to the Fi device --'
sudo bpftool net show dev "$iface" || true
echo '-- traffic-control filters attached directly to the Fi device --'
sudo tc filter show dev "$iface" ingress || true
sudo tc filter show dev "$iface" egress || true

section "Fi-bound HTTPS"
for url in https://www.google.com/generate_204 https://github.com/; do
  printf '%s: ' "$url"
  curl -4 --interface "if!$iface" --connect-timeout 7 --max-time 15 -sS \
    -o /dev/null \
    -w 'http=%{http_code} remote=%{remote_ip} connect=%{time_connect}s total=%{time_total}s exit=%{exitcode}\n' \
    "$url" || true
done

section "Cloudflare DNS-over-HTTPS through Fi"
curl -4 --interface "if!$iface" --resolve cloudflare-dns.com:443:1.1.1.1 \
  --connect-timeout 7 --max-time 15 -sS -H 'accept: application/dns-json' \
  'https://cloudflare-dns.com/dns-query?name=github.com&type=AAAA' || true
printf '\n'

ssh_probe() {
  local host="$1"
  local port="$2"
  local address
  local capture="$tmpdir/${host//./_}-${port}.tcpdump"
  local ssh_output="$tmpdir/${host//./_}-${port}.ssh"
  local capture_pid

  address="$(ipv4_for "$host")"
  [ -n "$address" ] || die "could not resolve an IPv4 address for $host"

  section "Fi-bound SSH: $host:$port ($address)"
  ip route get "$address" oif "$iface"

  # Capture just TCP header traffic for this one endpoint. No application
  # payload is requested or stored. A SYN without a SYN-ACK proves the host is
  # waiting beyond the local TCP stack; a response makes the failure visible.
  sudo timeout --signal=INT 11 tcpdump -l -nn -i "$iface" \
    "host $address and tcp port $port" >"$capture" 2>&1 &
  capture_pid=$!
  sleep 1

  timeout 9 ssh -4 -F /dev/null -o "BindInterface=$iface" \
    -o BatchMode=yes -o ConnectTimeout=7 -o PreferredAuthentications=none \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -p "$port" -T "git@$host" >"$ssh_output" 2>&1 || true

  wait "$capture_pid" || true
  printf '%s\n' '-- ssh --'
  cat "$ssh_output"
  printf '%s\n' '-- tcpdump --'
  cat "$capture"
}

ssh_probe github.com 22
ssh_probe ssh.github.com 443

section "Interpretation"
cat <<'EOF'
- HTTPS working but SSH captures showing repeated SYNs without SYN-ACKs means
  the timeout is beyond rugged's TCP stack (carrier or upstream path).
- A SYN-ACK followed by a later failure means the cause is above TCP and the
  SSH output should identify it.
- A Cloudflare AAAA response with no Answer is expected for github.com and
  demonstrates that the Fi DNS64 AAAA is resolver-specific.
EOF
