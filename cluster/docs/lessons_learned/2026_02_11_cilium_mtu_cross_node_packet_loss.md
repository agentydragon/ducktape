# MTU Misconfiguration: Cross-Node Packet Loss During Bootstrap

**Date**: 2026-02-11
**Status**: Resolved

## Root Cause

The Cilium Helm chart defines the MTU parameter as **uppercase** `MTU`. Our config used
lowercase `mtu: 1370`, which was silently ignored, leaving pod interfaces at the default
MTU of 1500.

With VXLAN (50 bytes) + KubeSpan WireGuard (80 bytes) double encapsulation, the effective
maximum payload without fragmentation is `1500 - 130 = 1370` bytes. At pod MTU 1500,
VXLAN-encapsulated packets exceeded the WireGuard interface MTU (1420), forcing kernel
fragmentation. UDP fragments traversing NAT/middleboxes between Hetzner VPS and home
Proxmox were intermittently dropped.

**Fix**: `MTU: 1370` (uppercase) in `cilium-values.yaml`, plus stripping Cilium config
to Talos-recommended defaults only (removing `endpointRoutes`, `hostServices`,
`bpf.hostLegacyRouting`, and other non-default options that Talos docs warn against
with KubeSpan).

## Key Symptoms

- Bootstrap stalled at ~18/64 Ready kustomizations for >20 minutes
- Kyverno webhook TLS handshake timeouts on cross-node API server calls:
  `TLS handshake error from 10.244.0.75: EOF`
- Three HelmReleases (tofu-controller, trust-manager, ingress-nginx) entered permanent
  `RetriesExceeded` state — default `install.remediation.retries: 0` meant a single
  transient failure was terminal
- 10-30% TCP connection failure rate between VPS and Proxmox nodes
- IP fragmentation counters (`ReasmFails` in `/proc/net/snmp`) showed hundreds of
  reassembly failures

## What We Tried (Failed)

These approaches masked symptoms but didn't fix the root cause:

1. **`endpointRoutes.enabled: true`** — Non-default Cilium option. Talos/KubeSpan docs
   warn against it ("asymmetric routing").
2. **`hostServices.enabled: true`** — Redundant with `kubeProxyReplacement: true`,
   potential KubeSpan interaction.
3. **`bpf.hostLegacyRouting: true`** — Workaround for a problem caused by `bpf.masquerade`,
   which we don't enable. Added complexity without fixing the underlying MTU issue.
4. **DNS workarounds** — `forwardKubeDNSToHost: false`, explicit `nameservers: [1.1.1.1, 8.8.8.8]`,
   hardcoded CoreDNS upstream. These bypassed HostDNS but the real DNS failures were
   caused by cross-node packet loss, not DNS configuration.
5. **Adding `dependsOn: kyverno`** to affected kustomizations — Ensured ordering but
   didn't prevent webhook timeouts since the networking issue was intermittent, not
   a race condition.

## What Fixed It

1. **`MTU: 1370` (uppercase)** — Eliminated IP fragmentation at the WireGuard interface.
   Zero fragmentation: pod (1370) + VXLAN (50) = 1420 fits WireGuard MTU, + WireGuard (80) = 1500 fits eth0.
2. **Stripped Cilium to Talos-recommended defaults** — Removed all non-default options
   except required ones (ipam, kubeProxyReplacement, securityContext, cgroup, hubble).
3. **Reverted DNS to defaults** — `forwardKubeDNSToHost: true` (default), no explicit
   nameservers, CoreDNS using `/etc/resolv.conf`. Works correctly with default Cilium
   (no eBPF host routing).

## Additional Mitigations Applied

These don't fix the root cause but provide defense in depth:

- **HelmRelease install retries** — `install.remediation.retries: 3` on all 27 HelmReleases.
  Prevents a single transient failure from permanently blocking the dependency chain.
- **Kyverno HA** — 3 replicas on control plane nodes with topology spread constraints.
  Ensures every API server has a local Kyverno pod, eliminating cross-node webhook calls.
- **ClusterIP readiness gate** — `verify_clusterip_routing()` in `bootstrap.py` creates
  a busybox pod on each node and runs `nslookup kubernetes.default.svc` to verify Cilium
  BPF service maps are populated before deploying Flux.

## Diagnostic Checklist

### Network Stack (Pod A on node X → Pod B on node Y, cross-region)

```text
Pod A (10.244.x.x)
  ├─ [1] Pod veth ── MTU = 1370
  ├─ [2] Cilium eBPF datapath ── Policy, service lookup, masquerade
  ├─ [3] Cilium VXLAN encap ── +50 bytes (outer UDP:8472)
  ├─ [4] Host network stack ── iptables masquerade
  ├─ [5] KubeSpan nftables ── Redirects to WireGuard (kubespan0)
  ├─ [6] WireGuard encap ── +80 bytes (outer UDP:51820)
  ├─ [7] Physical eth0 ── MTU = 1500
  ├─ [8] Internet / Home router
  ├─ [9] WireGuard decap
  ├─ [10] VXLAN decap
  └─ [11] Pod B receives packet
```

### Per-Layer Diagnostic Commands

**1. KubeSpan mesh health**:

```bash
talosctl -n $NODE get kubespanpeerstatuses
# Expect: (N-1) peers, all State=Up, non-zero RxBytes/TxBytes
```

**2. Cilium cross-node connectivity**:

```bash
for pod in $(kubectl get pods -n kube-system -l k8s-app=cilium -o name); do
  echo "=== $pod ==="
  kubectl exec -n kube-system $pod -- cilium-health status -o json 2>/dev/null | \
    jq '.nodes[] | {name: .name, host_icmp: .host.primary_address.icmp.status,
        endpoint_http: .endpoint.primary_address.http.status}'
done
# All status fields must be "" (empty = success)
```

**3. MTU verification**:

```bash
# Check Cilium MTU config
kubectl get configmap cilium-config -n kube-system -o yaml | grep -i mtu

# Check actual interface MTU
kubectl exec -n kube-system ds/cilium -- ip link show cilium_vxlan
# Should show mtu 1370

# Large packet test (1342 + 28 IP/ICMP header = 1370)
kubectl exec -n kube-system $CILIUM_POD_VPS -- ping -c 3 -s 1342 -M do $POD_IP_ON_PVE
```

**4. IP fragmentation counters** (high numbers = problem):

```bash
talosctl -n $NODE read /proc/net/snmp | grep -E "^Ip:"
# Look for: FragOKs > 0, ReasmFails > 0
```

**5. DNS resolution**:

```bash
kubectl run dnstest --image=docker.io/library/busybox --rm -it --restart=Never -- \
  nslookup google.com
```

## Key Lessons

1. **Helm values are case-sensitive** — always verify the exact key name with
   `helm show values <chart> | grep -i <key>` when setting MTU or similar parameters.
2. **Cilium + KubeSpan: stick to defaults** — Talos docs explicitly warn that non-default
   Cilium options cause "asymmetric routing" with KubeSpan.
3. **Flux default `retries: 0` is dangerous for bootstrap** — any transient failure
   becomes permanent. Always set `install.remediation.retries: 3`.
4. **Cilium health != ClusterIP readiness** — Agent-to-agent health probes pass before
   BPF service maps are populated. Verify actual ClusterIP routing before deploying workloads.
5. **IP fragmentation is silent** — No errors in logs, just intermittent connection failures.
   Check `/proc/net/snmp` fragmentation counters when debugging flaky cross-node connectivity.
