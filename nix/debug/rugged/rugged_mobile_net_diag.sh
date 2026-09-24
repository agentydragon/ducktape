#!/bin/bash
# Cellular network diagnostics for Dell Rugged / Foxconn DW5934e on Google Fi.
#
# Phase 1: runs with both WiFi and wwan0 up, binding all cellular tests to wwan0
# explicitly so the script can be invoked over SSH without disrupting connectivity.
# Phase 2: brief WiFi-off end-to-end validation (Chrome-equivalent path), then
# WiFi is restored automatically.
#
# Output: debug/rugged-mobile-net-diag/YYYYMMDD-HHMMSS/diag.txt + quic_probe.pcap

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTDIR="$SCRIPT_DIR/rugged-mobile-net-diag/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUTDIR"
exec > >(tee "$OUTDIR/diag.txt") 2>&1

echo "Output directory: $OUTDIR"
echo ""
echo "====== TIMESTAMP ======"
date
uname -a

# ── cellular state ────────────────────────────────────────────────────────────
WWAN_IP=$(ip -4 addr show wwan0 2>/dev/null | grep -oP 'inet \K[\d.]+' | head -1 || true)
WWAN_GW=$(ip -4 route show dev wwan0 2>/dev/null | awk '/^default/{print $3; exit}' || true)
CELLULAR_DNS=$(resolvectl dns wwan0 2>/dev/null | grep -oP '\d+\.\d+\.\d+\.\d+' | head -1 || true)

echo ""
if [ -n "${WWAN_IP:-}" ]; then
  echo "Cellular: wwan0 IP=$WWAN_IP  GW=${WWAN_GW:-?}  DNS=${CELLULAR_DNS:-?}"
else
  echo "WARNING: wwan0 has no IPv4 — cellular not connected. Phase 1 tests will be incomplete."
fi

# ── WiFi state (save for phase 2 restore) ─────────────────────────────────────
WIFI_WAS_ON=false
WIFI_CONN=""
if nmcli radio wifi 2>/dev/null | grep -q enabled; then
  WIFI_WAS_ON=true
  # nmcli -t escapes colons in names as \: — works for typical WiFi SSIDs
  WIFI_CONN=$(nmcli -t -f NAME,TYPE,STATE connection show --active 2>/dev/null \
    | awk -F: '$2=="wifi" && $3=="activated"{print $1; exit}' || true)
  echo "WiFi: on (active connection: ${WIFI_CONN:-none})"
else
  echo "WiFi: already off — phase 2 will be skipped"
fi

restore_wifi() {
  echo ""
  echo "====== RESTORING WIFI ======"
  nmcli radio wifi on 2>/dev/null || true
  if [ -n "${WIFI_CONN:-}" ]; then
    echo "Reconnecting to: $WIFI_CONN"
    sleep 3 # give radio time to scan
    nmcli connection up "$WIFI_CONN" 2>/dev/null || true
    for i in $(seq 1 30); do
      sleep 1
      if nmcli -t -f NAME,STATE connection show --active 2>/dev/null \
        | grep -q "^${WIFI_CONN}:activated"; then
        echo "WiFi reconnected after ${i}s."
        break
      fi
    done
  fi
}

if $WIFI_WAS_ON; then
  trap restore_wifi EXIT
fi

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1: interface dumps and wwan0-bound tests
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "====== INTERFACES (ip addr) ======"
ip addr

echo ""
echo "====== INTERFACES BRIEF ======"
ip -brief addr

echo ""
echo "====== LINK STATE ======"
ip link

echo ""
echo "====== IPv4 ROUTES ======"
ip -4 route show table all

echo ""
echo "====== IPv6 ROUTES ======"
ip -6 route show table all

echo ""
echo "====== ROUTING RULES (ip rule) ======"
ip rule show

echo ""
echo "====== ROUTING RULES IPv6 ======"
ip -6 rule show

echo ""
echo "====== ARP TABLE ======"
ip neigh

echo ""
echo "====== DNS: resolvectl status ======"
resolvectl status

echo ""
echo "====== DNS: resolvectl dns ======"
resolvectl dns

echo ""
echo "====== DNS: /etc/resolv.conf ======"
cat /etc/resolv.conf

echo ""
echo "====== DNS: /etc/nsswitch.conf ======"
cat /etc/nsswitch.conf

echo ""
echo "====== NETWORKMANAGER: active connections ======"
nmcli connection show --active

echo ""
echo "====== NETWORKMANAGER: all connections ======"
nmcli connection show

echo ""
echo "====== NETWORKMANAGER: device status ======"
nmcli device status

echo ""
echo "====== NETWORKMANAGER: device show all ======"
nmcli device show

echo ""
echo "====== NETWORKMANAGER: Google Fi connection full detail ======"
nmcli -f all connection show "Google Fi" 2>/dev/null || echo "Google Fi profile not found"

echo ""
echo "====== NETWORKMANAGER: active connection details ======"
nmcli -f all connection show --active 2>/dev/null

echo ""
echo "====== NETWORKMANAGER: wwan0 device detail (full) ======"
nmcli -f all device show wwan0 2>/dev/null || echo "wwan0 not found"

echo ""
echo "====== MODEMMANAGER: modem list ======"
mmcli -L

MODEM_ID=$(mmcli -L 2>/dev/null | grep -oP '/Modem/\K[0-9]+' | head -1 || true)

echo ""
echo "====== MODEMMANAGER: modem detail ======"
if [ -n "${MODEM_ID:-}" ]; then
  mmcli -m "$MODEM_ID"
  echo ""
  echo "====== MODEMMANAGER: modem detail (key-value) ======"
  mmcli -m "$MODEM_ID" --output-keyvalue 2>/dev/null || true
  echo ""
  echo "====== MODEMMANAGER: all bearers ======"
  mmcli -m "$MODEM_ID" --list-bearers 2>/dev/null || true
  echo ""
  echo "====== MODEMMANAGER: bearer detail ======"
  BEARER_IDS=$(mmcli -m "$MODEM_ID" 2>/dev/null | grep -oP '/Bearer/\K[0-9]+' || true)
  for BEARER_ID in $BEARER_IDS; do
    echo "--- Bearer $BEARER_ID ---"
    mmcli -b "$BEARER_ID"
    echo ""
    echo "--- Bearer $BEARER_ID (key-value) ---"
    mmcli -b "$BEARER_ID" --output-keyvalue 2>/dev/null || true
  done
  echo ""
  echo "====== MODEMMANAGER: signal quality (polled) ======"
  # Enable 5-second polling, wait, then read
  mmcli -m "$MODEM_ID" --signal-setup=5 2>/dev/null || true
  sleep 6
  mmcli -m "$MODEM_ID" --get-signal 2>/dev/null || true
else
  echo "No modem found"
fi

echo ""
echo "====== FOXCONN RF: /opt/foxconn/data/ ======"
ls -la /opt/foxconn/data/ 2>/dev/null || echo "/opt/foxconn/data/ not found"

echo ""
echo "====== SYSTEMD: ModemManager journal (last 100 lines) ======"
journalctl -u ModemManager --no-pager -n 100

echo ""
echo "====== SYSTEMD: NetworkManager journal (last 100 lines) ======"
journalctl -u NetworkManager --no-pager -n 100

echo ""
echo "====== SYSTEMD: systemd-resolved journal (last 50 lines) ======"
journalctl -u systemd-resolved --no-pager -n 50

echo ""
echo "====== SYSCTL: net.ipv4 ======"
sysctl -a 2>/dev/null | grep '^net\.ipv4'

echo ""
echo "====== SYSCTL: net.ipv6 ======"
sysctl -a 2>/dev/null | grep '^net\.ipv6'

echo ""
echo "====== INTERFACE MTU ======"
ip link | grep mtu

echo ""
echo "====== ROUTE TO 8.8.8.8 via wwan0 ======"
if [ -n "${WWAN_IP:-}" ]; then
  ip route get 8.8.8.8 from "$WWAN_IP"
else
  ip route get 8.8.8.8
fi

echo ""
echo "====== ROUTE TO 2001:4860:4860::8888 ======"
ip route get 2001:4860:4860::8888 2>&1

echo ""
echo "====== MTU PROBING: ping DF-bit via wwan0 to 8.8.8.8 ======"
# Sizes above the interface MTU will fail locally (EMSGSIZE) — shown as FAIL.
# The largest OK size reveals the true outgoing path MTU.
if [ -n "${WWAN_IP:-}" ]; then
  for size in 1472 1450 1420 1400 1380 1350 1300 1250 1228 1200 1172 1150 1100; do
    result=$(ping -I wwan0 -c 1 -M do -s $size -W 3 8.8.8.8 2>&1)
    if echo "$result" | grep -q "1 received"; then
      echo "  size=$((size + 28))B: OK"
    else
      echo "  size=$((size + 28))B: FAIL -- $(echo "$result" | tail -2)"
    fi
  done
else
  echo "wwan0 not connected — skipping"
fi

echo ""
echo "====== MTU PROBING: ping DF-bit via wwan0 to 2001:4860:4860::8888 (IPv6) ======"
if [ -n "${WWAN_IP:-}" ]; then
  for size in 1452 1400 1350 1300 1250 1200 1152; do
    result=$(ping -6 -I wwan0 -c 1 -M do -s $size -W 3 2001:4860:4860::8888 2>&1)
    if echo "$result" | grep -q "1 received"; then
      echo "  size=$((size + 48))B: OK"
    else
      echo "  size=$((size + 48))B: FAIL -- $(echo "$result" | tail -2)"
    fi
  done
else
  echo "wwan0 not connected — skipping"
fi

echo ""
echo "====== DNS RESOLUTION: via cellular DNS (${CELLULAR_DNS:-none}) ======"
if [ -n "${CELLULAR_DNS:-}" ]; then
  dig @"$CELLULAR_DNS" google.com A +time=5
  echo "---"
  dig @"$CELLULAR_DNS" reddit.com A +time=5
  echo "---"
  dig @"$CELLULAR_DNS" reddit.com AAAA +time=5
else
  echo "No cellular DNS — skipping"
fi

echo ""
echo "====== DNS RESOLUTION: 8.8.8.8 UDP bound to wwan0 ======"
if [ -n "${WWAN_IP:-}" ]; then
  dig @8.8.8.8 -b "$WWAN_IP" reddit.com A +time=5 +tries=2 2>&1
  echo "---"
  dig @8.8.8.8 -b "$WWAN_IP" reddit.com A +tcp +time=5 +tries=2 2>&1
else
  echo "wwan0 not connected — skipping"
fi

echo ""
echo "====== PING: IPv4 google via wwan0 ======"
ping -I wwan0 -4 -c 5 google.com 2>&1 || true

echo ""
echo "====== PING: IPv4 reddit via wwan0 ======"
ping -I wwan0 -4 -c 5 reddit.com 2>&1 || true

echo ""
echo "====== PING: IPv6 google via wwan0 ======"
ping -I wwan0 -6 -c 5 google.com 2>&1 || true

echo ""
echo "====== PING: IPv6 reddit via wwan0 ======"
ping -I wwan0 -6 -c 5 reddit.com 2>&1 || true

echo ""
echo "====== TRACEROUTE: IPv4 reddit via wwan0 ======"
if [ -n "${WWAN_IP:-}" ]; then
  traceroute -4 -n -s "$WWAN_IP" reddit.com 2>&1 || tracepath -4 -n reddit.com 2>&1
else
  echo "wwan0 not connected — skipping"
fi

echo ""
echo "====== TRACEROUTE: UDP to reddit port 443 via wwan0 ======"
if [ -n "${WWAN_IP:-}" ]; then
  traceroute -U -p 443 -n -s "$WWAN_IP" reddit.com 2>&1 || echo "traceroute UDP failed"
fi

echo ""
echo "====== TRACEROUTE: TCP SYN to reddit port 443 via wwan0 ======"
if [ -n "${WWAN_IP:-}" ]; then
  traceroute -T -p 443 -n -s "$WWAN_IP" reddit.com 2>&1 || echo "traceroute TCP failed"
fi

echo ""
echo "====== TRACEROUTE: ICMP to reddit via wwan0 ======"
if [ -n "${WWAN_IP:-}" ]; then
  traceroute -I -n -s "$WWAN_IP" reddit.com 2>&1 || echo "traceroute ICMP failed"
fi

echo ""
echo "====== CURL: HTTP/1.1 IPv4 google via wwan0 ======"
curl -v --interface wwan0 --http1.1 -4 -sm 15 http://google.com -o /dev/null 2>&1

echo ""
echo "====== CURL: HTTPS/1.1 IPv4 google via wwan0 ======"
curl -v --interface wwan0 --http1.1 -4 -sm 15 https://google.com -o /dev/null 2>&1

echo ""
echo "====== CURL: HTTPS/1.1 IPv4 reddit via wwan0 ======"
curl -v --interface wwan0 --http1.1 -4 -sm 15 https://reddit.com -o /dev/null 2>&1

echo ""
echo "====== CURL: HTTPS/2 IPv4 reddit via wwan0 ======"
curl -v --interface wwan0 --http2 -4 -sm 15 https://reddit.com -o /dev/null 2>&1

echo ""
echo "====== CURL: HTTPS/3 (QUIC) reddit via wwan0 ======"
curl -v --interface wwan0 --http3 -4 -sm 20 https://reddit.com -o /dev/null 2>&1 || echo "HTTP/3 not supported or failed"

echo ""
echo "====== CURL: HTTPS/3 (QUIC) google via wwan0 ======"
# Google Fi special-routes Google traffic — if this works but reddit doesn't,
# the issue is carrier CGNAT dropping third-party QUIC, not a local problem.
curl -v --interface wwan0 --http3 -4 -sm 20 https://www.google.com -o /dev/null 2>&1 || echo "HTTP/3 not supported or failed"

echo ""
echo "====== CURL: HTTPS/3 (QUIC) cloudflare via wwan0 ======"
curl -v --interface wwan0 --http3 -4 -sm 20 https://cloudflare.com -o /dev/null 2>&1 || echo "HTTP/3 not supported or failed"

echo ""
echo "====== CURL: HTTPS/3 (QUIC) youtube via wwan0 ======"
curl -v --interface wwan0 --http3 -4 -sm 20 https://www.youtube.com -o /dev/null 2>&1 || echo "HTTP/3 not supported or failed"

echo ""
echo "====== CURL: HTTPS/1.1 IPv6 reddit via wwan0 ======"
curl -v --interface wwan0 --http1.1 -6 -sm 15 https://reddit.com -o /dev/null 2>&1

echo ""
echo "====== CURL: HTTPS/1.1 IPv4 cloudflare via wwan0 ======"
curl -v --interface wwan0 --http1.1 -4 -sm 15 https://cloudflare.com -o /dev/null 2>&1

echo ""
echo "====== CURL: timing breakdown reddit via wwan0 ======"
curl --interface wwan0 -4 -sm 30 https://reddit.com -o /dev/null -w "
  namelookup:    %{time_namelookup}s
  connect:       %{time_connect}s
  appconnect:    %{time_appconnect}s
  pretransfer:   %{time_pretransfer}s
  starttransfer: %{time_starttransfer}s
  total:         %{time_total}s
  http_code:     %{http_code}
  size_download: %{size_download}
" 2>&1

echo ""
echo "====== CURL: timing breakdown google via wwan0 ======"
curl --interface wwan0 -4 -sm 30 https://www.google.com -o /dev/null -w "
  namelookup:    %{time_namelookup}s
  connect:       %{time_connect}s
  appconnect:    %{time_appconnect}s
  pretransfer:   %{time_pretransfer}s
  starttransfer: %{time_starttransfer}s
  total:         %{time_total}s
  http_code:     %{http_code}
  size_download: %{size_download}
" 2>&1

echo ""
echo "====== SPEED TEST: download via wwan0 ======"
if [ -n "${WWAN_IP:-}" ]; then
  curl --interface wwan0 -4 -o /dev/null -sm 20 -w "speed_download: %{speed_download} bytes/sec\nsize_download: %{size_download} bytes\ntime_total: %{time_total}s\n" http://speedtest.tele2.net/1MB.zip 2>&1
else
  echo "wwan0 not connected — skipping"
fi

echo ""
echo "====== OPENSSL: TLS handshake to reddit via wwan0 ======"
if [ -n "${WWAN_IP:-}" ]; then
  echo "" | timeout 10 openssl s_client -bind "${WWAN_IP}:0" \
    -connect reddit.com:443 -servername reddit.com 2>&1
else
  echo "" | timeout 10 openssl s_client -connect reddit.com:443 -servername reddit.com 2>&1
fi

echo ""
echo "====== QUIC TCPDUMP: capture on wwan0 while attempting QUIC to reddit ======"
# If we see outgoing UDP but no response → carrier dropping. No outgoing → local block.
PCAP="$OUTDIR/quic_probe.pcap"
echo "Capturing on wwan0 → $PCAP"
tcpdump -i wwan0 -w "$PCAP" 'udp port 443' &
TCPDUMP_PID=$!
sleep 1
curl --interface wwan0 --http3 -4 -sm 15 https://reddit.com -o /dev/null \
  -w "curl http_code: %{http_code}\n" 2>&1 || true
sleep 1
kill "$TCPDUMP_PID" 2>/dev/null || true
wait "$TCPDUMP_PID" 2>/dev/null || true
echo "Captured packets:"
tcpdump -r "$PCAP" -nn 2>&1 || echo "tcpdump read failed"

echo ""
echo "====== PUBLIC IP (what the internet sees, via wwan0) ======"
if [ -n "${WWAN_IP:-}" ]; then
  PUBLIC_IP=$(curl --interface wwan0 -4 -sm 10 https://api.ipify.org 2>/dev/null || true)
  echo "Public IP: ${PUBLIC_IP:-unknown}"
  echo "wwan0 local IP: $WWAN_IP"
  if [ -n "${PUBLIC_IP:-}" ] && [ "$PUBLIC_IP" != "$WWAN_IP" ]; then
    echo "CGNAT: yes (public IP differs from wwan0 IP)"
  else
    echo "CGNAT: no"
  fi
else
  echo "wwan0 not connected"
fi

echo ""
echo "====== UDP PROBE: Nebula VPN port ======"
NEBULA_PEER=$(ip route show dev nebula1 2>/dev/null | head -1 | awk '{print $1}' | cut -d/ -f1)
if [ -n "${NEBULA_PEER:-}" ]; then
  nping --udp -p 4242 -c 3 "$NEBULA_PEER" 2>&1 || true
else
  echo "No nebula1 route found"
fi
ip -s link show nebula1 2>&1 || true

echo ""
echo "====== BPF: loaded programs ======"
bpftool prog list 2>&1 || echo "bpftool not available"

echo ""
echo "====== BPF: tc filters on wwan0 ======"
tc filter show dev wwan0 ingress 2>&1 || true
tc filter show dev wwan0 egress 2>&1 || true

echo ""
echo "====== BPF: tc filters on lo ======"
tc filter show dev lo ingress 2>&1 || true
tc filter show dev lo egress 2>&1 || true

echo ""
echo "====== CONNTRACK: UDP sessions ======"
conntrack -L -p udp 2>&1 || echo "conntrack not available"

echo ""
echo "====== CILIUM: status ======"
cilium status 2>&1 || echo "cilium CLI not available"

echo ""
echo "====== SS: all sockets ======"
ss -tulpna

echo ""
echo "====== NETSTAT: routing table ======"
netstat -rn 2>/dev/null || echo "netstat not available"

echo ""
echo "====== FIREWALL: nftables ======"
nft list ruleset 2>/dev/null || echo "nft not available or no ruleset"

echo ""
echo "====== FIREWALL: iptables ======"
iptables -L -n -v 2>/dev/null || echo "iptables not available"
iptables -t nat -L -n -v 2>/dev/null

echo ""
echo "====== FIREWALL: ip6tables ======"
ip6tables -L -n -v 2>/dev/null || echo "ip6tables not available"

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2: WiFi-off end-to-end validation (Chrome-equivalent path)
# ─────────────────────────────────────────────────────────────────────────────

if $WIFI_WAS_ON; then
  echo ""
  echo "====== PHASE 2: WiFi-off end-to-end validation ======"
  echo "Turning WiFi off..."
  nmcli radio wifi off
  # Wait for wlan to stop being active
  for i in $(seq 1 15); do
    sleep 1
    if ! nmcli radio wifi 2>/dev/null | grep -q enabled; then
      echo "WiFi down after ${i}s"
      break
    fi
  done
  sleep 2 # let routing settle

  echo ""
  echo "====== PHASE 2: interfaces ======"
  ip -brief addr

  echo ""
  echo "====== PHASE 2: default route ======"
  ip -4 route show default

  echo ""
  echo "====== PHASE 2: ping google ======"
  ping -4 -c 5 google.com 2>&1 || true

  echo ""
  echo "====== PHASE 2: ping reddit ======"
  ping -4 -c 5 reddit.com 2>&1 || true

  echo ""
  echo "====== PHASE 2: CURL timing reddit ======"
  curl -4 -sm 30 https://reddit.com -o /dev/null -w "
  namelookup:    %{time_namelookup}s
  connect:       %{time_connect}s
  appconnect:    %{time_appconnect}s
  pretransfer:   %{time_pretransfer}s
  starttransfer: %{time_starttransfer}s
  total:         %{time_total}s
  http_code:     %{http_code}
  size_download: %{size_download}
" 2>&1

  echo ""
  echo "====== PHASE 2: CURL timing google ======"
  curl -4 -sm 30 https://www.google.com -o /dev/null -w "
  namelookup:    %{time_namelookup}s
  connect:       %{time_connect}s
  appconnect:    %{time_appconnect}s
  pretransfer:   %{time_pretransfer}s
  starttransfer: %{time_starttransfer}s
  total:         %{time_total}s
  http_code:     %{http_code}
  size_download: %{size_download}
" 2>&1

  echo ""
  echo "====== PHASE 2: SPEED TEST: download ======"
  curl -4 -o /dev/null -sm 20 -w "speed_download: %{speed_download} bytes/sec\nsize_download: %{size_download} bytes\ntime_total: %{time_total}s\n" http://speedtest.tele2.net/1MB.zip 2>&1

  echo ""
  echo "====== PHASE 2: OPENSSL TLS to reddit ======"
  echo "" | timeout 10 openssl s_client -connect reddit.com:443 -servername reddit.com 2>&1

  echo ""
  echo "====== PHASE 2: public IP ======"
  curl -4 -sm 10 https://api.ipify.org 2>&1 && echo ""

  # Restore WiFi (trap also calls this on EXIT, but we clear the trap here
  # so it's called exactly once)
  trap - EXIT
  restore_wifi
else
  echo ""
  echo "====== PHASE 2: skipped (WiFi was already off) ======"
fi

echo ""
echo "====== DONE ======"
date
