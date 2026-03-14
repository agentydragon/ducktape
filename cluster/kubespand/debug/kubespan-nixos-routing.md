# KubeSpan NixOS Worker Routing Debug Log

## Problem

k8s-worker-test (NixOS, 10.0.243.53 on Proxmox LAN) cannot route traffic
through KubeSpan WireGuard tunnel to VPS control plane nodes.

## Current state

**ICMP through KubeSpan works** (after flushing iptables).
**TCP through KubeSpan fails** (HAProxy can't connect to VPS:6443).

## Observations (chronological)

### Phase 1: Initial diagnosis

- WireGuard handshakes work (all peers "up", recent handshakes)
- nftables chains (`kubespan_outgoing`, `kubespan_prerouting`) installed correctly
  with VPS IPs in destination sets
- ip rule `32500: fwmark 0x40/0x60 lookup 180` present
- Table 180: `default dev kubespan mtu 1420` present
- `ip route get 5.78.43.147 mark 0x40` → `dev kubespan table 180` (correct)
- WireGuard transfer counters increase both directions (data flows)
- tcpdump on kubespan shows ICMP echo replies AND TCP SYN-ACKs arriving

### Phase 2: What doesn't drop packets

- **rpfilter**: DROP counter stayed at 0 after ping
- **NixOS firewall**: flushed to accept-all, still failed
- **`networking.firewall.enable = false`**: rebuilt image, still failed
- **iptables counter forensics**: all 225 packets passed through entire INPUT chain
  (CILIUM_INPUT → KUBE-FIREWALL → nixos-fw) with zero drops anywhere
- **Cilium TC BPF**: `tc filter show dev kubespan ingress` — empty (no eBPF on kubespan)

### Phase 3: Nuclear iptables flush

- Flushed ALL iptables (filter, mangle, raw, nat), set all policies ACCEPT
- Flushed nftables talos table
- **Ping to VPS worked via ens18 (internet, not KubeSpan)** — proves direct connectivity OK
- Restarted kubespand (reinstalls nftables chains only, iptables stays flushed)
- **ICMP ping to VPS through KubeSpan: WORKS** (2/2, ~32ms)
- **ICMP ping to Proxmox CP (10.2.1.1) through KubeSpan: FAILS**
- **TCP to VPS:6443 through KubeSpan: FAILS** (HAProxy Layer4 timeout)

### Phase 4: Current state summary

After iptables flush + kubespand restart:

- ICMP to VPS IPs: **works** (goes through KubeSpan based on fwmark)
- TCP to VPS IPs: **fails**
- ICMP to Proxmox IPs (10.2.x.x): **fails**

## Hypotheses

### H1: conntrack interaction with fwmark (HIGH probability)

TCP requires conntrack to match SYN-ACK to original SYN. ICMP echo/reply
matching is simpler. With iptables flushed but conntrack module still loaded,
conntrack may be confusing the connection state because:

- Outgoing SYN gets mark 0x40 via nftables OUTPUT chain
- SYN is sent through kubespan (WireGuard encrypts, mark becomes 0x20)
- WireGuard's encrypted UDP packet exits via ens18 with mark 0x20
- Reply arrives as encrypted UDP on ens18, WireGuard decrypts
- Decrypted SYN-ACK arrives on kubespan with mark 0x00
- conntrack may not associate the SYN-ACK with the original SYN because
  the marks differ, or because the packet arrived on a different interface

Evidence: conntrack showed all TCP entries stuck in SYN_RECV (SYN sent,
SYN-ACK arrived but not matched to complete handshake).

### H2: Proxmox ping failure is a different bug

10.2.1.1 is reachable via the VLAN (Proxmox LAN), not via internet. The
KubeSpan tunnel to Proxmox nodes goes through the local mcast network.
This failing could be rpfilter on the Proxmox side, or a WireGuard
AllowedIPs issue.

### H3: `type route` nftables OUTPUT chain marks but re-routing fails for TCP

The `kubespan_outgoing` chain has `type route` (not `type filter`). This
triggers a routing re-lookup after the mark is set. For ICMP, re-routing
works. For TCP, the re-lookup may interact with conntrack's route cache
or socket binding.

### H4: The VPS Hetzner firewall drops TCP from unexpected source IPs

The ping works but TCP doesn't. The Hetzner firewall may have rules that
allow ICMP but restrict TCP to known source IPs. The packet arrives at
the VPS with src=10.0.243.53 (a private IP), which may be dropped by
Hetzner's firewall for TCP but allowed for ICMP.

BUT: this doesn't explain why conntrack entries show SYN_RECV (which
means SYN-ACK was sent back). If Hetzner dropped the TCP SYN, there
would be no SYN-ACK at all.

## QEMU test results (before the /32 fix)

All 4 probes pass in the QEMU environment:

- IPv6 ULA ICMP ✓
- IPv4 peer eth1 ICMP ✓
- IPv6 ULA TCP ✓
- IPv4 peer eth1 TCP ✓

The QEMU VMs have no iptables, no Cilium, no conntrack complexity.

## Root cause found (NixOS worker)

**kubespand does not add the node's own IP to the kubespan interface.**

When a reply packet arrives on `kubespan` with `dst=10.0.243.53`, the kernel
needs to find this as a "local" address reachable via the receiving interface.
Without it, the kernel returns "Invalid cross-device link" and drops the packet.

ICMP worked because ICMP echo reply is handled by `icmp_rcv()` which has different
routing validation than TCP's socket lookup path.

**Fix:** `sudo ip addr add 10.0.243.53/32 dev kubespan` — after this, ALL traffic
works (ICMP, TCP, all peers, API server accessible).

## Fix implemented (commit 731dd1c)

In `controllers/kubespan/manager.go:277-306`, after writing the ULA address,
kubespand now adds the node's non-ULA routed addresses (from
`discovery.RoutedNodeAddresses()` at `discovery/discovery.go:235`) to the
kubespan interface as secondary `/32` or `/128` addresses. This ensures the
kernel accepts reply packets arriving on kubespan.

The QEMU test enables `ip_forward=1` and `rp_filter=2` (matching real NixOS
environment) in `qemu_tests/vms/kubespanlib/kubespanlib.go:45-47`.

## Phase 5: Verification after manual fix

After `ip addr add 10.0.243.53/32 dev kubespan`:

- Ping all 4 peers: ✓ (VPS ~25-31ms, Proxmox ~1ms)
- TCP to VPS:6443: ✓ (CONNECTED)
- API healthz via HAProxy: ✓ (returns 401 = auth needed, but TCP works)
- HAProxy backends: UP (2/3, cp3 doesn't exist)

## Upstream Talos comparison (2026-03-14 analysis)

### Talos does NOT add node IPs to the kubespan interface

**The claim in the original "Fix implemented" section that "Talos's KubeSpan
manager writes an AddressSpec that adds the node's routed addresses to the
kubespan interface" is incorrect.**

Verified at:

- **Upstream Talos** `internal/app/machined/pkg/controllers/kubespan/manager.go:461-480`:
  writes a single `AddressSpec` for the ULA IPv6 address
  (`localSpec.Address` with `Family: FamilyInet6`). No loop over node addresses.
  No equivalent of kubespand's `RoutedNodeAddresses()`.
- **kubespand** `controllers/kubespan/manager.go:256-275`: matches upstream
  (writes ULA address).
- **kubespand** `controllers/kubespan/manager.go:277-306`: kubespand-specific
  code — loops over `discovery.RoutedNodeAddresses()` and writes each as a
  `/32` or `/128` `AddressSpec` on the kubespan interface. **This does not exist
  in upstream Talos.**

### How Talos handles reply-packet routing instead

Talos's `PeerSpecController` (`internal/app/machined/pkg/controllers/kubespan/peer_spec.go:116-118`)
adds each peer's physical node IPs (`spec.Addresses`) to that peer's WireGuard
AllowedIPs set:

```go
for _, ip := range spec.Addresses {
    builder.Add(ip)
}
```

This means WireGuard's cryptokey routing accepts reply packets arriving on the
kubespan interface with the peer's physical IP as the source. The kernel's
network stack sees these as valid WireGuard-validated packets, bypassing the
need for the `/32` address workaround.

kubespand uses the upstream `PeerSpecController` (via `@talos_internal`), so
this mechanism should already be present. The NixOS worker issue was likely
caused by a different factor (see `rp_filter` below).

### Why Talos doesn't need the /32 fix (rp_filter)

Talos also avoids `rp_filter` issues because it does not set `rp_filter` on
any interface. Verified at:

- **Talos kernel param defaults**
  (`internal/app/machined/pkg/controllers/runtime/kernel_param_defaults.go:77-91`):
  sets `ip_forward=1`, `icmp_ignore_bogus_error_responses=1`,
  `icmp_echo_ignore_broadcasts=1`. Does NOT set `rp_filter`.
- **GitHub code search** for `rp_filter` in `siderolabs/talos`: 0 results.
- **Linux kernel default** for `rp_filter` is 0 (disabled). Since Talos is a
  minimal OS that doesn't run systemd's `sysctl.d` overrides, the kernel
  default applies.

On NixOS (and most other distros), systemd sets `rp_filter=2` (loose mode) or
`rp_filter=1` (strict mode) via `/usr/lib/sysctl.d/`. When a TCP SYN-ACK reply
arrives on the kubespan interface with `dst=<node-eth0-IP>`, the kernel performs
a reverse path check: "would I route a packet to the source of this reply through
kubespan?" If the source is a remote peer whose route goes through eth0 (the
physical interface), `rp_filter >= 1` drops the packet. Adding the node's IP as
`/32` on kubespan makes the kernel see the address as local on that interface,
bypassing the cross-device check.

### The /32 fix breaks QEMU tests

Adding the node's eth0 IP to the kubespan interface causes a routing problem in
QEMU test environments where the discovery service is on the same L2 subnet as
the VMs. The failure sequence (from `kubespan_test` RBE run, BuildBuddy invocation
`3ff54921-a16e-45b9-a421-d2f1225fbea7`):

1. VM eth0 gets `192.168.50.1/24` (`kubespanlib.go:43`)
2. kubespand starts, installs ip rules + nftables chains
3. kubespand assigns `192.168.50.1/32` to kubespan
   (`manager.go:282-283`, via `RoutedNodeAddresses()`)
4. kubespand assigns ULA IPv6 to kubespan, creates default routes in table 180
5. All discovery service connections to `192.168.50.254:3000` fail with
   `EHOSTUNREACH` ("no route to host")
6. After 180s, the test times out

The error from `vm-a.log`:

```text
hello failed: "rpc error: code = Unavailable desc = connection error:
  desc = \"transport: Error while dialing: dial tcp 192.168.50.254:3000:
  connect: no route to host\""
```

The `doublenat_test` shows a related symptom — `RouteSpecController` fails with
"network is down" when adding routes to the kubespan interface before it's fully
initialized.

### Correct fix approach

The `/32` on kubespan is only needed on non-Talos hosts (NixOS, etc.) where
`rp_filter >= 1`. Options:

1. **Remove the `RoutedNodeAddresses()` loop** and instead set `rp_filter=0`
   on the kubespan interface specifically:
   `echo 0 > /proc/sys/net/ipv4/conf/kubespan/rp_filter`
   This matches what Talos effectively has (kernel default `rp_filter=0`) and
   avoids the routing side effects of adding extra IPs to kubespan.

2. **Add an ip rule exemption** for locally-sourced traffic:
   `ip rule add from <eth0-ip> lookup main priority 32000`
   This ensures traffic sourced from the node's physical IP always consults the
   main routing table first (before the fwmark rule at priority 32500).

3. **Set `accept_local=1`** on the kubespan interface to allow the kernel to
   accept packets with a local source address arriving on a different interface.

Option 1 is the closest to upstream Talos behavior and the least invasive.

### Code references

| Location                                          | Description                                                         |
| ------------------------------------------------- | ------------------------------------------------------------------- |
| `controllers/kubespan/manager.go:256-275`         | ULA address on kubespan (matches upstream)                          |
| `controllers/kubespan/manager.go:277-306`         | Node IPs on kubespan (kubespand-only, causes QEMU test failure)     |
| `discovery/discovery.go:235-270`                  | `RoutedNodeAddresses()` — enumerates non-loopback, non-kubespan IPs |
| `qemu_tests/vms/kubespanlib/kubespanlib.go:45-47` | QEMU VMs set `rp_filter=2`                                          |
| `qemu_tests/vms/kubespanlib/kubespanlib.go:67`    | `ForceRouting: true` in test config                                 |
| Upstream `kubespan/manager.go:461-480`            | Only writes ULA address, no node IPs                                |
| Upstream `kubespan/peer_spec.go:116-118`          | Adds peer physical IPs to WireGuard AllowedIPs                      |
| Upstream `kubespan/routing_rules.go:50-78`        | ip rules: fwmark → table 180, priority ~32500                       |
| Upstream `runtime/kernel_param_defaults.go:77-91` | Talos sysctls: no `rp_filter` set                                   |
| Commit `731dd1c`                                  | Initial kubespand code (includes the `/32` fix from the start)      |
| Commit `cdb6b76`                                  | "announce local IPs and filter endpoints for NAT traversal"         |
| Commit `6a9bf4f`                                  | DiscoveryController reconcile loop fix                              |
