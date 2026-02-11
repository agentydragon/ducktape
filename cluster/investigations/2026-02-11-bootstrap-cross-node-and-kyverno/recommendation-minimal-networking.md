# Recommendation: Strip Cilium to Talos-Recommended Defaults

**Date**: 2026-02-11
**Status**: Proposed, untested

## Executive Summary

The current Cilium config has 10+ non-default options. Talos docs and maintainers
explicitly warn that KubeSpan only works with "default Cilium configuration." The
DNS failure (`169.254.116.108` unreachable) is a **known consequence** of non-default
Cilium settings — not an inherent incompatibility. The fix is to remove the
non-default options, not to work around their effects.

## The Full Network Stack: Every Layer, Every Failure Point

### Path: Pod A (node X) → Pod B (node Y), cross-region (VPS ↔ Proxmox)

```text
Pod A (10.244.1.x)
  │
  ├─ [1] Pod veth ──────────────── MTU = 1370 (proposed)
  │   Failure: pod can't send. Diagnose: ip link show inside pod
  │
  ├─ [2] Cilium eBPF datapath ──── Policy, service lookup, masquerade
  │   Failure: packet dropped by policy or BPF program crash
  │   Diagnose: cilium monitor --type drop; cilium bpf policy list
  │
  ├─ [3] Cilium VXLAN encap ────── Adds 50-byte header (outer UDP:8472)
  │   Inner packet ≤ 1370 → outer packet ≤ 1420
  │   Failure: VXLAN interface down, wrong peer mapping
  │   Diagnose: cilium bpf tunnel list; ip -d link show cilium_vxlan
  │
  ├─ [4] Host network stack ────── Outer packet addressed to node Y's IP
  │   (iptables masquerade, NOT eBPF — no bpf.masquerade in config)
  │   Failure: iptables rules wrong, masquerade broken
  │   Diagnose: iptables -t nat -L; conntrack -L
  │
  ├─ [5] KubeSpan nftables ─────── Intercepts packet destined for peer node IP
  │   Redirects into WireGuard interface (kubespan0)
  │   Failure: peer not in AllowedIPs (phantom peer issue), nftables rules wrong
  │   Diagnose: talosctl get kubespanpeerspecs; talosctl get kubespanpeerstatuses
  │            nft list ruleset | grep kubespan
  │
  ├─ [6] WireGuard encap ──────── Adds 80-byte header (outer UDP:51820)
  │   Inner packet ≤ 1420 → outer packet ≤ 1500
  │   Failure: handshake not completed, wrong endpoint, key mismatch
  │   Diagnose: talosctl get kubespanpeerstatuses (State, Endpoint, LastHandshakeTime)
  │            wg show (from talosctl)
  │
  ├─ [7] Physical eth0 ──────────── MTU = 1500, outer WireGuard packet
  │   VPS: Hetzner public IP (5.78.x.x)
  │   PVE: Proxmox bridge vmbr4 (10.2.x.x)
  │   Failure: interface down, firewall blocking UDP 51820
  │   Diagnose: ip link show eth0; hcloud firewall list; ping between nodes
  │
  ├─ [8] Internet / Home router ── VPS→PVE: internet transit + home NAT
  │   PVE→VPS: home router → internet
  │   VPS→VPS: Hetzner internal switch (same DC)
  │   PVE→PVE: Proxmox bridge (direct L2)
  │   Failure: ISP blocking UDP, NAT timeout, home router down
  │   Diagnose: traceroute; check home router port forwarding for UDP 51820
  │
  │   === Arriving at node Y ===
  │
  ├─ [9] WireGuard decap ──────── Strips 80-byte header
  ├─ [10] VXLAN decap ─────────── Strips 50-byte header, delivers to Pod B
  └─ [11] Pod B receives packet

Total overhead: 130 bytes (50 VXLAN + 80 WireGuard)
Effective pod MTU: 1500 - 130 = 1370
```

### Path: Pod → External DNS (e.g., 1.1.1.1)

```text
Pod
  │
  ├─ [1] Pod resolv.conf ───── Points to kube-dns ClusterIP (10.96.0.10)
  │   Failure: resolv.conf wrong. Diagnose: cat /etc/resolv.conf in pod
  │
  ├─ [2] Cilium service LB ── Translates ClusterIP → CoreDNS pod IP
  │   Failure: service endpoints empty, CoreDNS not running
  │   Diagnose: kubectl get endpoints -n kube-system kube-dns
  │
  ├─ [3] CoreDNS ──────────── Receives query, looks up zone
  │   For cluster.local: answered from etcd/k8s API
  │   For allegedly.works: forward to PowerDNS (10.96.53.53)
  │   For everything else: forward to upstream
  │   Failure: CoreDNS crashloop, wrong Corefile
  │   Diagnose: kubectl logs -n kube-system -l k8s-app=kube-dns
  │
  ├─ [3a] With forwardKubeDNSToHost=true (DEFAULT):
  │   CoreDNS → /etc/resolv.conf → 169.254.116.108 (Talos HostDNS)
  │   HostDNS → Hetzner DHCP DNS / explicit nameservers → internet
  │   Failure point: 169.254.116.108 unreachable from CoreDNS pod
  │   This ONLY breaks when Cilium eBPF host routing is active
  │   (bpf.masquerade=true auto-enables it). With default Cilium, iptables
  │   routing handles it correctly.
  │   Diagnose from CoreDNS pod: nslookup google.com 169.254.116.108
  │
  ├─ [3b] With forwardKubeDNSToHost=false (CURRENT):
  │   CoreDNS → hardcoded 1.1.1.1 / 8.8.8.8 → internet
  │   Bypasses HostDNS entirely. Always works.
  │   Diagnose: dig @1.1.1.1 google.com from CoreDNS pod
  │
  └─ [4] Response flows back the same path
```

### Path: Host process (containerd) → External DNS

```text
containerd
  │
  ├─ [1] Host resolv.conf ── Points to 127.0.0.53 (Talos HostDNS)
  │   Failure: HostDNS process not running
  │   Diagnose: talosctl dmesg | grep dns; talosctl read /etc/resolv.conf
  │
  ├─ [2] Talos HostDNS ───── Caching resolver at 127.0.0.53:53
  │   Upstream from: DHCP-provided nameservers OR explicit machine.network.nameservers
  │   Failure: no upstream nameservers, upstream unreachable
  │   Diagnose: talosctl read /system/resolved/resolv.conf (shows upstream)
  │            talosctl dmesg | grep -i dns
  │
  ├─ [3] Upstream DNS ────── Hetzner: 213.133.100.{100,99,98}; or explicit 1.1.1.1
  │   For VPS: goes out eth0 to internet (direct)
  │   For PVE: goes out eth0 → home router → internet
  │   Failure: upstream DNS unreachable, firewall blocking
  │   Diagnose: talosctl -n NODE exec -- dig @1.1.1.1 google.com (if available)
  │
  └─ [4] Response flows back
```

### Path: Pod → Kubernetes API (webhook calls, etc.)

```text
Pod (e.g., kube-apiserver calling kyverno webhook)
  │
  ├─ [1] kube-apiserver → webhook service ClusterIP
  │   Translated to kyverno pod IP by Cilium/kube-proxy replacement
  │
  ├─ [2] If same node: delivered locally via Cilium
  │   If cross-node: VXLAN + KubeSpan (full stack above)
  │   Failure: cross-node VXLAN tunnel not ready during bootstrap
  │   Diagnose: cilium-health status -o json (from ALL cilium pods)
  │
  └─ [3] TLS handshake to webhook
      Failure: "TLS handshake error: EOF" = tunnel dropped mid-handshake
      Diagnose: kyverno logs for TLS errors; cilium-health for cross-node status
```

## MTU Analysis: The Silent Fragmentation Problem

**Current state**: No explicit MTU set in Cilium config.

Cilium auto-detects eth0 MTU (1500), sets pod MTU to 1450 (1500 - 50 VXLAN).
But VXLAN packets (up to 1500 bytes outer) enter KubeSpan WireGuard (MTU 1420).
Result: **every pod packet larger than 1370 bytes causes IP fragmentation** at
the WireGuard interface.

```text
Pod sends 1450-byte packet (Cilium's pod MTU)
  → VXLAN encap: 1450 + 50 = 1500 byte outer packet
  → KubeSpan WireGuard MTU: 1420
  → 1500 > 1420 → kernel fragments into 2 IP fragments
  → Each fragment + WireGuard header → 2 packets on the wire
  → Receiving node: WireGuard decap → IP reassembly → VXLAN decap
```

This causes:

- Performance degradation (fragmentation/reassembly overhead on every large packet)
- Potential failures with middleboxes that drop fragments
- Intermittent TCP issues under load (reassembly buffer exhaustion)

**Fix**: Set `mtu: 1370` in Cilium Helm values.

```text
Pod MTU = 1370
  → VXLAN encap: 1370 + 50 = 1420 (fits WireGuard MTU exactly)
  → WireGuard encap: 1420 + 80 = 1500 (fits eth0 MTU exactly)
  → Zero fragmentation
```

## Audit of Every Non-Default Cilium Option

Current `cilium-values.yaml` vs. Talos-recommended defaults:

| Option                           | Current Value     | Talos Recommended | Verdict                                                                                                               |
| -------------------------------- | ----------------- | ----------------- | --------------------------------------------------------------------------------------------------------------------- |
| `cluster.name/id`                | `talos-cluster/1` | not set           | **Remove** — only needed for Cluster Mesh                                                                             |
| `ipam.mode`                      | `kubernetes`      | `kubernetes`      | **Keep** — required                                                                                                   |
| `k8sServiceHost/Port`            | `localhost/7445`  | `localhost/7445`  | **Keep** — required for kube-proxy replacement                                                                        |
| `routingMode: tunnel`            | explicit          | (default=tunnel)  | **Remove** — already the default                                                                                      |
| `tunnelProtocol: vxlan`          | explicit          | (default=vxlan)   | **Remove** — already the default                                                                                      |
| `ipv4.enabled: true`             | explicit          | (default=true)    | **Remove** — already the default                                                                                      |
| `enableIPv4Masquerade: true`     | explicit          | (default=true)    | **Remove** — already the default                                                                                      |
| `ipv6.enabled: false`            | explicit          | (default=false)   | **Remove** — already the default                                                                                      |
| `securityContext.*`              | set               | set               | **Keep** — required by Talos                                                                                          |
| `cgroup.*`                       | set               | set               | **Keep** — required by Talos                                                                                          |
| `kubeProxyReplacement: "true"`   | set               | `true`            | **Keep** — required                                                                                                   |
| `hubble.*`                       | enabled           | not in minimal    | **Keep** — observability, harmless                                                                                    |
| `loadBalancer.algorithm: random` | explicit          | not set           | **Remove** — unnecessary, default works                                                                               |
| `l2announcements.enabled: true`  | explicit          | not set           | **Remove** — not needed; ingress/DNS use hostNetwork                                                                  |
| `bpf.hostLegacyRouting: true`    | explicit          | not set           | **Remove** — workaround for a problem caused by bpf.masquerade, which we don't enable                                 |
| `endpointRoutes.enabled: true`   | explicit          | not set           | **REMOVE** — non-default, [warned against](https://docs.siderolabs.com/talos/v1.11/networking/kubespan) with KubeSpan |
| `socketLB.enabled: true`         | explicit          | not set           | **Remove** — redundant with kubeProxyReplacement=true                                                                 |
| `hostServices.enabled: true`     | explicit          | not set           | **REMOVE** — non-default, potential KubeSpan interaction                                                              |
| `nodePort.*`                     | explicit          | not set           | **Remove** — defaults are fine                                                                                        |

**Removed options that are actively dangerous with KubeSpan:** `endpointRoutes`,
`hostServices`, `bpf.hostLegacyRouting` (the last masks problems rather than
fixing them).

## Audit of Talos Machine Config DNS Settings

| Setting                | VPS (current)           | PVE (current) | Proposed              |
| ---------------------- | ----------------------- | ------------- | --------------------- |
| `hostDNS.enabled`      | `true`                  | `true`        | Remove (default=true) |
| `forwardKubeDNSToHost` | `false`                 | `true`        | Remove (default=true) |
| `nameservers`          | `["1.1.1.1","8.8.8.8"]` | not set       | Remove (use DHCP)     |

**Rationale**: With default Cilium (no `bpf.masquerade`), eBPF host routing is NOT
auto-enabled. Packets from pods to `169.254.116.108` go through the iptables path,
which correctly routes to the host's `lo` interface. The Talos maintainer [confirmed
this](https://github.com/siderolabs/talos/issues/10002#issuecomment-2555965073):

> "with more or less defaults [...] The issue isn't there."
> "One way to trigger it is to actually keep enabling Cilium non-default settings,
> the one I found is `--set=bpf.masquerade=true`."

Since we don't set `bpf.masquerade=true`, eBPF host routing is OFF, and
`forwardKubeDNSToHost=true` should work. The `nameservers` override and CoreDNS
`forward . 1.1.1.1 8.8.8.8` were workarounds for a problem that only existed
because of the non-default Cilium options.

## Proposed Cilium Values (Complete)

```yaml
# Cilium CNI — Talos-recommended defaults + KubeSpan-compatible MTU
#
# IMPORTANT: Do not add non-default options without verifying KubeSpan compatibility.
# See: https://docs.siderolabs.com/talos/v1.11/networking/kubespan
# See: https://docs.siderolabs.com/kubernetes-guides/cni/deploying-cilium

# Required: Talos-recommended IPAM
ipam:
  mode: kubernetes

# Required: kube-proxy replacement (kube-proxy disabled in Talos config)
kubeProxyReplacement: "true"
k8sServiceHost: "localhost"
k8sServicePort: "7445"

# Required: Talos security context (no SYS_MODULE — Talos blocks kernel module loading)
securityContext:
  capabilities:
    ciliumAgent:
      [CHOWN, KILL, NET_ADMIN, NET_RAW, IPC_LOCK, SYS_ADMIN, SYS_RESOURCE, DAC_OVERRIDE, FOWNER, SETGID, SETUID]
    cleanCiliumState: [NET_ADMIN, SYS_ADMIN, SYS_RESOURCE]

# Required: Talos cgroup configuration
cgroup:
  hostRoot: /sys/fs/cgroup
  autoMount:
    enabled: false

# Required: Account for double encapsulation (VXLAN + KubeSpan WireGuard)
# eth0 MTU (1500) - VXLAN overhead (50) - WireGuard overhead (80) = 1370
# Without this, packets >1370 bytes fragment at the WireGuard interface.
mtu: 1370

# Observability (optional, not networking-critical)
hubble:
  enabled: true
  relay:
    enabled: true
  ui:
    enabled: true
```

Everything else left at Cilium defaults: tunnel/VXLAN routing, iptables masquerade,
no eBPF host routing, no endpoint routes, no L2 announcements, no explicit socketLB.

## Proposed Talos Machine Config Changes

### VPS nodes (`hetzner-nodes.tf`)

Remove:

- `nameservers = ["1.1.1.1", "8.8.8.8"]` — let DHCP provide Hetzner's DNS
- `forwardKubeDNSToHost = false` — let default (true) apply
- Entire `hostDNS` block — defaults are correct

Keep:

- `kubespan.enabled = true`
- `kubespan.allowDownPeerBypass = true` — useful during bootstrap convergence
- `kubePrism.enabled = true` with port 7445

### Proxmox nodes (`proxmox-nodes.tf`)

Remove:

- Entire `hostDNS` block — defaults are correct

Keep:

- `kubespan.enabled = true`
- `kubespan.allowDownPeerBypass = true`
- `kubePrism.enabled = true` with port 7445

### CoreDNS (`coredns-custom.yaml`)

Revert upstream from `forward . 1.1.1.1 8.8.8.8` to `forward . /etc/resolv.conf`.
The `/etc/resolv.conf` in CoreDNS pods will contain `169.254.116.108` (Talos HostDNS),
which is reachable with default Cilium (no eBPF host routing).

Keep the `allegedly.works` zone forward to PowerDNS (10.96.53.53).

## Diagnostic Checklist for Next Bootstrap

Run these checks IN ORDER after `tofu apply` and Cilium install, before Flux:

### 1. KubeSpan mesh health

```bash
# From each node: verify exactly (N-1) peers, all UP
talosctl -n $NODE get kubespanpeerstatuses
# Expected: 3 peers, all State=Up, non-zero RxBytes/TxBytes
# Red flag: >3 peers (phantoms), State=Unknown/Down, 0 bytes

# If peers show Down, check endpoint rotation
talosctl -n $NODE get kubespanpeerstatuses -o yaml
# Look at LastEndpointChange and LastHandshakeTime
```

### 2. Cilium cross-node connectivity

```bash
# From EVERY cilium pod (not just the first one!)
for pod in $(kubectl get pods -n kube-system -l k8s-app=cilium -o name); do
  echo "=== $pod ==="
  kubectl exec -n kube-system $pod -- cilium-health status -o json 2>/dev/null | \
    jq '.nodes[] | {name: .name, host_icmp: .host.primary_address.icmp.status,
        host_http: .host.primary_address.http.status,
        endpoint_icmp: .endpoint.primary_address.icmp.status,
        endpoint_http: .endpoint.primary_address.http.status}'
done
# All must show status="" (empty = success). Any non-empty = failure.
```

### 3. DNS resolution (HostDNS path)

```bash
# Verify HostDNS is listening on 169.254.116.108
talosctl -n $NODE read /system/resolved/resolv.conf
# Expected: nameserver 169.254.116.108

# Test from a non-hostNetwork pod
kubectl run dnstest --image=docker.io/library/busybox --rm -it --restart=Never -- \
  nslookup google.com 169.254.116.108
# If this fails: forwardKubeDNSToHost is broken → fall back to explicit nameservers

# Test CoreDNS end-to-end
kubectl run dnstest --image=docker.io/library/busybox --rm -it --restart=Never -- \
  nslookup google.com
# Tests the full chain: pod → kube-dns ClusterIP → CoreDNS → 169.254.116.108 → upstream
```

### 4. MTU verification

```bash
# From a pod on VPS, ping a pod on Proxmox with large packet
kubectl exec -n kube-system $CILIUM_POD_VPS -- \
  ping -c 3 -s 1342 -M do $POD_IP_ON_PVE
# 1342 + 28 (IP+ICMP header) = 1370 = pod MTU → should succeed
# If -s 1343 also succeeds, our MTU math is wrong (but conservative, so harmless)
# If -s 1342 fails with "message too long", MTU is set lower than expected
```

### 5. Cross-node service connectivity

```bash
# After CoreDNS is running, verify cross-node service access
kubectl run curltest --image=docker.io/curlimages/curl --rm -it --restart=Never -- \
  curl -sk https://kubernetes.default.svc.cluster.local/healthz
# Tests: pod → ClusterIP → kube-apiserver (may be on different node)
```

## Fallback Plan

If DNS fails at step 3 (pod cannot reach `169.254.116.108`), the root cause is
an unexpected interaction between Cilium defaults and HostDNS. Fall back to:

1. Add `forwardKubeDNSToHost = false` to ALL nodes (VPS and Proxmox)
2. Add `nameservers = ["1.1.1.1", "8.8.8.8"]` to VPS nodes
3. Change CoreDNS Corefile to `forward . 1.1.1.1 8.8.8.8`

This is the current working config. The difference from today is: we still have
the minimal Cilium values (no non-default options), which eliminates the KubeSpan
interaction issues separately from the DNS question.

## What This Does NOT Change

- KubeSpan: still enabled, still WireGuard mesh, still `allowDownPeerBypass`
- Packer snapshot boot: still eliminates phantom peers
- Bootstrap script: still checks full-mesh Cilium health from all pods
- Flux GitOps: unchanged
- `tcp_mtu_probing` sysctl: still needed for PowerDNS AXFR over WireGuard
- Discovery: still enabled
- CNI: still "none" (Cilium installed by Terraform, not Talos)
- kube-proxy: still disabled

## Risk Assessment

| Change                                   | Risk                                                  | Mitigation                                                               |
| ---------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------ |
| Remove `endpointRoutes`                  | Low — default is more tested                          | Default works for all use cases                                          |
| Remove `hostServices`                    | Low — redundant with kubeProxyReplacement             | kubeProxyReplacement already handles it                                  |
| Remove `bpf.hostLegacyRouting`           | Medium — this was the HostDNS workaround              | Not needed without bpf.masquerade; fallback plan ready                   |
| Remove `l2announcements`                 | Low — no LoadBalancer services currently depend on it | If needed later, re-add                                                  |
| Set `mtu: 1370`                          | Low — strictly more correct than auto-detect          | Prevents fragmentation that was silently occurring                       |
| Revert `forwardKubeDNSToHost` to default | Medium — previously broken                            | Previously broken DUE TO non-default Cilium; diagnostic step 3 validates |
| Remove explicit `nameservers`            | Medium — DHCP dependency                              | Hetzner DHCP is reliable; fallback plan ready                            |

## References

- [Talos Cilium deployment guide](https://docs.siderolabs.com/kubernetes-guides/cni/deploying-cilium)
- [Talos KubeSpan docs](https://docs.siderolabs.com/talos/v1.11/networking/kubespan) — "Cilium expects all inter-node traffic to flow directly over the node's primary interface"
- [Talos HostDNS docs](https://docs.siderolabs.com/talos/v1.11/networking/host-dns)
- [Talos issue #10002](https://github.com/siderolabs/talos/issues/10002) — Cilium 1.16.5 breaks DNS with forwardKubeDNSToHost (maintainer confirms: only with non-default settings)
- [Talos issue #11244](https://github.com/siderolabs/talos/issues/11244) — KubeSpan intercepts public IP traffic causing Cilium failures
- [Talos issue #11235](https://github.com/siderolabs/talos/issues/11235) — Cilium + KubeSpan + BPF Masquerade on multi-VPS
- [Cilium eBPF host routing docs](https://docs.cilium.io/en/v1.16/operations/performance/tuning/) — prerequisites: bpf.masquerade=true auto-enables host routing
- [Cilium masquerading docs](https://docs.cilium.io/en/stable/network/concepts/masquerading/) — "BPF masquerading also enables the BPF Host-Routing mode"
- [Blog: Cilium networking on Talos hybrid cluster](https://22.frenchintelligence.org/2025/09/11/comparing-cilium-networking-setups-on-a-talos-hybrid-kubernetes-cluster/) — VXLAN + KubeSpan tested, works
