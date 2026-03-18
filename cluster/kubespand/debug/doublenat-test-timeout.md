# Double-NAT QEMU Test Timeout Analysis

**Date**: 2026-03-18
**Status**: Diagnosed, not yet fixed

## Symptom

Both `doublenat_kubespand_test` and `doublenat_talos_test` TIMEOUT at 900s (Bazel
`timeout = "long"`). Test logs show only `mkfs.fat` output from CIDATA creation — no
VM console output, no PASS. This is expected: Go's `t.Log` is buffered and lost when
SIGKILL terminates the test on timeout.

The flat topology test (same code structure, same mesh convergence criteria) passes in
~113s.

## Network Topology

```text
[NAT1 192.168.60.2] --[LAN-A 192.168.60.0/24]-- [Router-A 192.168.50.1] --+
                                                                             |
                                                                        [Internet 192.168.50.0/24]
                                                                             |
[NAT2 192.168.70.2] --[LAN-B 192.168.70.0/24]-- [Router-B 192.168.50.3] --+
                                                                             |
                                                                        [VPS 192.168.50.2]
                                                                      [Discovery 192.168.50.254]
```

6 QEMU VMs total: 3 infrastructure (discovery, router-a, router-b) + 1 VPS (Talos CP)
\+ 2 workers (Talos or kubespand depending on variant). All under TCG software
emulation (no KVM), `-machine accel=tcg`, 4 host CPUs (`cpu:4` tag).

Routers use nftables masquerade with `Persistent: true` (`NF_NAT_RANGE_PERSISTENT`),
which provides **endpoint-independent mapping** (same source port for a given
`(src-ip, src-port)` regardless of destination).

## Root Cause: Multi-Stage Convergence Exceeds 900s Under TCG

The double-NAT mesh convergence requires a **multi-stage process** that doesn't exist
in the flat topology. Each stage adds latency, and TCG software emulation amplifies
everything ~2-3x. The combined latency exceeds the 900s Bazel timeout.

### Stage 1: VM Boot (~180-300s)

| Component                         | Talos variant | kubespand variant |
| --------------------------------- | ------------- | ----------------- |
| 3 Alpine VMs (discovery, routers) | ~10-20s       | ~10-20s           |
| VPS Talos CP (1536MB, 2 SMP)      | ~60-90s       | ~60-90s           |
| 2 Talos workers (1536MB each)     | ~90-180s      | —                 |
| 2 Alpine kubespand workers        | —             | ~10-20s           |
| **Total (sequential bottleneck)** | **~180-300s** | **~90-120s**      |

Under TCG with 6 VMs on 4 CPUs, boot times are dominated by CPU contention. Talos
VMs are especially slow — full Linux boot with containerd, kubelet, and machined
services.

The flat test has only 4 VMs (1 Alpine + 3 Talos, or 3 Alpine + 1 Talos), which means
less CPU contention and faster overall boot.

### Stage 2: Hub Establishment (~30-60s)

Before NAT'd nodes can discover each other's post-NAT endpoints, each must establish
a WireGuard tunnel with the VPS (the only publicly-reachable node):

1. NAT1 discovers VPS via discovery service → learns VPS endpoint `192.168.50.2:51820`
2. NAT1 initiates WG handshake to VPS through Router-A masquerade → succeeds
3. NAT2 does the same through Router-B

Each step involves: discovery service polling + COSI controller reconciliation +
WG handshake + retry timing. Under TCG: ~15-30s per direction after boot.

### Stage 3: Endpoint Harvesting + Re-announcement (~10-30s)

Once VPS has WG tunnels to both NATs:

1. VPS's `EndpointController` observes NAT1's source address (Router-A's internet IP
   - mapped port) from the `PeerStatus` resource
2. VPS produces a `kubespan.Endpoint` resource for NAT1
3. VPS's `DiscoveryController` re-announces NAT1's harvested endpoint via discovery
4. NAT2 picks up the updated endpoint for NAT1 from discovery
5. Same process for NAT2's endpoint → NAT1

This chain runs through multiple COSI reconciliation loops and a discovery service
round-trip. Under TCG: ~10-30s.

**This stage doesn't exist in the flat test** — nodes on the same subnet publish
their real IPs directly.

### Stage 4: Wrong-Endpoint Cycling (~30-90s)

Each NAT'd node discovers multiple endpoints for its peer:

| Endpoint                          | Source                         | Reachable?               |
| --------------------------------- | ------------------------------ | ------------------------ |
| `192.168.60.2:51820` (private IP) | Peer's self-reported affiliate | No — private, behind NAT |
| `10.0.2.15:51820` (management IP) | Peer's self-reported affiliate | No — per-VM QEMU network |
| `192.168.50.1:X` (harvested)      | VPS's re-announced endpoint    | Yes — post-NAT address   |

KubeSpan's `ManagerController` tries each endpoint for **15 seconds**
(`EndpointConnectionTimeout`) before cycling to the next. If the harvested endpoint is
third in the list, each node wastes **30 seconds** on unreachable endpoints before
trying the correct one.

Both NAT1 and NAT2 must be trying each other's **correct** (harvested) endpoints
**simultaneously** for the WireGuard hole-punch to work. If they're cycling through
different wrong endpoints at different phases, the simultaneous-open window is missed
and they must wait for the next cycle.

**This stage doesn't exist in the flat test** — all endpoints are directly reachable.

### Stage 5: NAT Traversal Simultaneous Open (~5-15s)

Once both sides are sending WG handshakes to each other's correct post-NAT endpoints:

1. NAT1 sends WG InitiatorHello to `Router-B:Y` → Router-A creates SNAT conntrack
   entry (reply direction: `Router-B:Y → Router-A:X`)
2. NAT2 sends WG InitiatorHello to `Router-A:X` → Router-B creates SNAT conntrack
   entry (reply direction: `Router-A:X → Router-B:Y`)
3. NAT2's packet arrives at Router-A:X → matches reply direction from step 1 →
   un-SNAT → delivered to NAT1
4. NAT1's packet arrives at Router-B:Y → matches reply direction from step 2 →
   un-SNAT → delivered to NAT2
5. WG handshake completes (one side becomes responder)

This requires both outbound packets to exist within the UDP conntrack timeout (30s).
With WG's 5s retry interval, convergence within ~10-15s once both have the correct
endpoint.

### Timeline Summary

| Stage                      | Flat test            | Double-NAT (TCG) |
| -------------------------- | -------------------- | ---------------- |
| Boot                       | ~60-90s              | ~180-300s        |
| Hub establishment          | N/A (same subnet)    | ~30-60s          |
| Endpoint harvesting        | N/A                  | ~10-30s          |
| Wrong-endpoint cycling     | N/A                  | ~30-90s          |
| NAT traversal              | N/A (direct)         | ~5-15s           |
| **Total**                  | **~60-90s**          | **~255-495s**    |
| **With 2-3x TCG slowdown** | **~113s** (observed) | **~510-1485s**   |

The upper range (1485s) comfortably exceeds the 900s timeout, explaining why both
variants fail.

## Why Both Variants Fail

The kubespand variant (1 Talos + 5 Alpine) has faster boot (stage 1) but identical
stages 2-5. The endpoint harvesting, wrong-endpoint cycling, and NAT traversal timing
are independent of node type — they depend on COSI controller reconciliation speed
and WG retry intervals, both of which are slowed equally by TCG.

## Why the Flat Test Passes

The flat test eliminates stages 2-5 entirely:

- All nodes are on the same L2 segment
- Self-reported endpoints (real IPs) work directly
- No endpoint harvesting needed
- No endpoint cycling through wrong addresses
- No NAT traversal

Only stage 1 (boot) matters, and with 4 VMs instead of 6, it completes in ~113s.

## Note on `Persistent: true` Masquerade

The router code comment claims `Persistent` creates "full-cone NAT." This is
**incorrect**. `NF_NAT_RANGE_PERSISTENT` provides:

- **Endpoint-independent mapping (EIM)**: same mapped port for a given `(src-ip,
src-port)` regardless of destination ✓
- **Endpoint-independent filtering (EIF)**: any external host can send to the mapped
  port ✗

Linux masquerade (even with Persistent) uses **endpoint-dependent filtering** — only
packets matching an existing conntrack entry's reply direction are accepted. This is
**Port Restricted Cone NAT** (RFC 4787), not full-cone.

However, this does NOT prevent NAT traversal. The simultaneous-open technique works
with Port Restricted Cone NAT: each side's outbound packet creates a conntrack entry
whose reply direction matches the other side's inbound packet. The requirement is that
both sides send within the conntrack timeout window of each other — which WG's 5s
retry interval ensures within a few cycles.

## Fix Options

1. **Increase Bazel timeout** to 1800s or 2700s — simplest, but masks slowness
2. **Add endpoint filters** to skip management IPs (`10.0.2.0/24`) — eliminates one
   15s wrong-endpoint cycle per peer
3. **Pre-seed harvested endpoints** via test configuration — eliminates stages 2-3
4. **Reduce VM count** — use a single router instead of two (breaks the "different
   NATs" property but tests NAT traversal itself)
5. **Use KVM when available** — `accel=kvm:tcg` falls back to TCG gracefully; on
   hardware with KVM, VMs run at near-native speed

The most impactful fix is likely combining options 2 and 5, plus a modest timeout
increase.
