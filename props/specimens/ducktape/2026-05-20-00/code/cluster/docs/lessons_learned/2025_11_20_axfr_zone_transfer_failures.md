# AXFR Zone Transfer Failures: Cilium DNS Proxy, Docker Networking, PMTUD

**Date**: 2025-11-20 (resolved 2025-11-22)
**Status**: Resolved

## Root Cause

Three independent issues prevented AXFR zone transfers from cluster PowerDNS (primary)
to VPS PowerDNS (secondary):

### Issue 1: Cilium DNS Proxy Interference

Cilium's transparent DNS proxy (`dnsproxy-enable-transparent-mode: true`) intercepts ALL
port 53 traffic, including **ingress** to authoritative DNS servers. A 10-second socket
linger timeout (`dnsproxy-socket-linger-timeout: 10`) killed long-running AXFR transfers.

**Fix**: Pod annotation `io.cilium.proxy.denylist: "53/TCP,53/UDP"` to bypass Cilium
DNS proxy for PowerDNS. Commit 334c702.

### Issue 2: VPS Docker Bridge Networking

VPS PowerDNS container used Docker bridge networking (172.19.0.0/16) instead of host
networking. This caused different routing paths: manual `dig` via host Tailscale interface
worked, but daemon AXFR through Docker bridge/NAT failed.

**Fix**: `network_mode: host` in Docker Compose + bind to public IP only. Commits 8579bd6,
aa0bbf2, 8987a7a.

### Issue 3: PMTUD Blackhole

After fixing the above, daemon AXFR still timed out at 30 seconds. TCP packet capture
revealed: packets >1240 bytes silently dropped by intermediate router without ICMP
"fragmentation needed" feedback.

- Pod MTU: 1500, Tailscale MTU: 1280, effective MSS: 1240 bytes
- Manual `dig` used smaller MSS (1220) — worked
- PowerDNS daemon assumed MSS 1460 (standard 1500 MTU) — packets dropped

**Fix**: Enable TCP MTU probing (`net.ipv4.tcp_mtu_probing: 1`) as unsafe sysctl on
PowerDNS pod. Required: Talos kubelet `allowed-unsafe-sysctls`, namespace PodSecurity
`privileged`, pod `securityContext.sysctls`. Commits 3bacb8a, 653e01d, 3825401.

## Key Symptoms

- AXFR transfers send only SOA record, then timeout after exactly 10 seconds (issue 1)
- Manual `dig` AXFR works but daemon `pdns_control retrieve` fails (issues 2, 3)
- TCP packet capture shows second AXFR packet (1228 bytes) never arrives, SACK
  retransmissions fail, 30-second timeout (issue 3)

## Key Lessons

1. **Cilium DNS proxy intercepts ingress too** — designed for egress DNS policy, but
   `transparent-mode` hijacks ALL port 53. Authoritative DNS servers need the denylist
   annotation.
2. **Docker bridge ≠ host networking for VPN traffic** — containers behind Docker NAT
   route differently than the host. Use `network_mode: host` when Tailscale/WireGuard
   routing matters.
3. **TCP MTU probing solves PMTUD blackholes** — `net.ipv4.tcp_mtu_probing: 1` (RFC 4821)
   is the correct fix when ICMP "fragmentation needed" is blocked. Conservative mode:
   only activates on retransmission failure.
4. **Check effective MTU across all tunnel layers** — pod MTU, overlay MTU, WireGuard MTU,
   and Tailscale MTU may all differ. Silent packet loss on oversized packets is the symptom.
