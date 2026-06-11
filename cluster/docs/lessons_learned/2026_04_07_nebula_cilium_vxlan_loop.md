# Nebula + Cilium VXLAN Amplification Loop

**Date**: 2026-04-07
**Status**: Resolved
**Impact**: talos-pve-cp-0 partitioned for 18 hours, 272% CPU, etcd unreachable

## Root Cause

Nebula on Talos nodes was configured with `listen.host = "0.0.0.0"`. The kernel
sometimes picked Cilium pod CIDR IPs (10.244.x.x) as the UDP source address
for Nebula packets. Peers learned these pod IPs as the sender's UDP endpoint.

When the Proxmox CP node (pve-cp-0) sent Nebula return traffic to a peer's
pod IP, Cilium VXLAN-encapsulated it through the Nebula tunnel — creating a
tunnel-in-tunnel amplification loop: 48k pkt/s, 577M tun drops, etcd SYNs
starved.

VPS-to-VPS was unaffected because VXLAN between Hetzner nodes goes directly
over L2 without Nebula. Only Proxmox↔VPS traffic traverses Nebula for VXLAN,
triggering the loop.

## Fix

Added `lighthouse.local_allow_list` to all Nebula configs blocking `cilium.*`
and `lxc.*` interfaces from being advertised as endpoints:

```yaml
lighthouse:
  local_allow_list:
    interfaces:
      cilium.*: false
      lxc.*: false
```

Applied to Terraform (`nebula.tf`) and NixOS (`nebula.nix`). Talos
restarted Nebula automatically on config push — no reboot needed.

## Lessons Learned

### 1. Overlay-in-overlay is a latent bomb

Running Nebula (overlay) on nodes that also run Cilium VXLAN (overlay) creates
a risk of encapsulation loops whenever the kernel's source IP selection picks
an overlay address. This class of bug is silent until it isn't — it can work
for days because the failure only manifests on specific cross-site paths
(Proxmox↔VPS but not VPS↔VPS). The failure mode is catastrophic
(amplification loop → total node incapacitation) and self-reinforcing (more
retries → more traffic → more drops). Any time you layer overlays, explicitly
fence them from each other.

### 2. `0.0.0.0` listen addresses are dangerous on multi-interface hosts

Kubernetes nodes have many interfaces (`eth0`, `cilium_host`, `cilium_vxlan`,
`lxc*`, `nebula1`, `lo`). Binding to `0.0.0.0` lets the kernel pick any
source IP, and the routing table may prefer a surprising one. This applies to
any daemon running in the host namespace on a k8s node — not just Nebula.

### 3. Monitor interface-level drop counters

`node_network_transmit_drop_total{device="nebula1"}` would have caught this
in minutes. We went 18 hours. The tun device silently dropping 20% of packets
with no log, no alert, no dmesg — just a counter in `/proc/net/dev` — is
exactly the kind of thing that needs a Prometheus alert rule.

### 4. `nsenter` into a KVM process is NOT the same as being inside the VM

This cost significant debugging time. The host's network namespace is
completely separate from the guest's kernel. Tests run via
`nsenter -t <qemu-pid> -n` test the host, not the VM. The only way to test
from inside a Talos VM is `talosctl`. All `nc`/`ping` tests that appeared to
show connectivity were invalid — they ran on the Proxmox host, not the VM.

### 5. Collect everything before rebooting

We almost rebooted early when we thought it was a "transient Nebula tunnel
issue." If we had, we'd have fixed the symptom temporarily but never found
the `local_allow_list` root cause, and it would have recurred after the next
Nebula re-key cycle.

### 6. Follow the packet, not the hypothesis

We went through multiple wrong hypotheses (Cilium eBPF conntrack, missing
default route, port-specific filtering) because we were reasoning about what
could cause the symptoms. The breakthrough came from packet captures showing
what traffic was actually flowing — 99.8% of VXLAN was
`cilium_host:4242 → pod_ip:4242`. Then checking Nebula logs confirmed peers
were advertising pod IPs. Data over theory.

## Timeline

- **2026-04-06 10:36:07 UTC**: Nebula tunnel to vps-cp-0 marked "dead"
- **2026-04-06 10:37:50 UTC**: Tunnel re-established. Last successful kubelet heartbeat.
- **2026-04-06 10:38:48 UTC**: Kubernetes marks node `NotReady`
- **2026-04-07 ~05:00 UTC**: Investigation begins (~18h partitioned)

## Key Evidence

| Metric               | pve-cp-0 (broken)  | vps-cp-0 (healthy) |
| -------------------- | ------------------ | ------------------ |
| nebula1 TX packets   | 2,822M             | 258M               |
| nebula1 TX drops     | 577M (20%)         | 5,776              |
| nebula1 TX drop rate | 8,330/s            | ~0                 |
| Nebula CPU (26h)     | 72,590s (~0.8 CPU) | 37,919s            |
| cilium_vxlan TX rate | 34,352 pkt/s       | normal             |
| VXLAN traffic (30s)  | 99.8% single flow  | n/a                |

The single VXLAN flow was `10.244.4.81:4242 → 10.244.1.207:4242` — Nebula
UDP traffic being VXLAN-encapsulated through the Nebula tunnel (tunnel-in-tunnel).

## Resolution Metrics

After applying `local_allow_list`:

- nebula1 TX drops: 577M → **0**
- Nebula CPU: 72,590s → **13s**
- etcd: Fail → **OK**
- Node: NotReady → **Ready**

## Prevention

- [ ] Alert on `node_network_transmit_drop_total{device="nebula1"}` rate > 10/s
- [ ] Alert on etcd health check failures
