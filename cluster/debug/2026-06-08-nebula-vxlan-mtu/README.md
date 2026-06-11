# Nebula/VXLAN MTU: corrected model + measurements (2026-06-08)

## TL;DR

The Cilium pod MTU was `1412`, derived from a **wrong overhead model**. Cilium VXLAN
rides **inside** the Nebula tunnel (nested, not parallel), and `nebula1`'s tun MTU was at
its default **1300** — so full-size pod packets (1412 + 50 VXLAN = 1462) exceeded 1300 and
had to fragment / PMTUD-clamp. Fixed by raising `nebula1` to 1420 and setting Cilium's `MTU`
to 1420 — Cilium's `MTU` is the **underlay** value (the device it tunnels VXLAN over), and it
subtracts the 50-byte VXLAN overhead itself to get a 1370 cross-node pod MTU.

## The stack (verified via `cilium-dbg status`)

Cilium routing mode is `Tunnel [vxlan]`; VXLAN endpoints are the node `InternalIP`s, which
are the Nebula `10.42.x` addresses. So a pod packet is VXLAN-encapsulated and the result is
routed out `nebula1`, which encapsulates again onto `eno1`:

```text
pod packet ──+50 VXLAN──> [must fit nebula1 tun MTU] ──+60 Nebula──> [must fit eno1 1500]
```

The old `cilium-values.yaml` comment computed `1500 − 50 − 38 = 1412` as if VXLAN and Nebula
added 88 **in parallel**. They don't — they **stack**, and the 38 was also wrong.

## Measurements

Ran from a `hostNetwork` netshoot pod on `ovh-ns103656` (source `eno1` = 147.135.39.162).

### Underlay path MTU (DF ping between node public IPs)

| Path                                                       | 1500-byte DF frame | Result   |
| ---------------------------------------------------------- | ------------------ | -------- |
| cross-/24 → ovh-ns104963 (147.135.104.16)                  | payload 1472       | **PASS** |
| same-/24 hairpin → ovh-ns103711 (147.135.39.176, via .254) | payload 1472       | **PASS** |

The OVH inter-node underlay carries a full **1500**, including the same-/24 `.254` hairpin.
(No jumbo above 1500 — that's vRack-only, unavailable on Kimsufi.)

### Current Nebula tun ceiling

`ping -M do` over `10.42.0.17`: payload 1272 (frame 1300) **PASS**, 1273 **FAIL** → tun MTU
is exactly 1300 (Nebula default; never set in `nebula.tf`).

### Nebula overhead (tcpdump on eno1, udp/4242)

A 1300-byte inner IP packet produced an outer **UDP payload of 1332** → + 8 (UDP) + 20 (IP)
= **1360 on the wire**. So Nebula overhead = **60 bytes** exactly:

| Component         | Bytes  |
| ----------------- | ------ |
| Nebula header     | 16     |
| AES-GCM auth tag  | 16     |
| UDP header        | 8      |
| Outer IPv4 header | 20     |
| **Total**         | **60** |

## Outcome

Set `nebula1` tun MTU **1420** and Cilium **`MTU: 1420`** → cross-node pod MTU 1370.
The canonical model, config locations, live-apply procedure, and roaming caveat now
live in <../../docs/network.md>. This note is the dated investigation record.

### Gotcha that cost a wrong rollout

First attempt set Cilium `MTU: 1370` (treating it as the pod MTU). Cilium's `MTU` is
the **underlay** value and it subtracts the 50-byte VXLAN overhead itself, so that
produced a **1320** cross-node pod MTU (measured: pod-to-pod DF ceiling 1320, not
1370). Corrected to `MTU: 1420` (= `nebula1`), which gave a verified 1370 pod ceiling.

### Live rollout notes (no re-bootstrap needed)

- **Nebula**: applied per node via `tofu apply -target=talos_machine_configuration_apply…`,
  one at a time, etcd voters sequentially (workers first, etcd+DB-primary node last),
  health-checked between each. Each reload is a brief nebula service restart, no reboot.
- **Cilium**: `null_resource.cilium_bootstrap` has no triggers, so ran the committed
  `helm upgrade` directly. **`--wait`/`--atomic` rolled back** because dead `wyrm2`'s
  Pending DaemonSet pod never reports "updated" → `--wait` timed out. Re-ran without
  `--wait`/`--atomic`; the DaemonSet rolled the 6 healthy nodes cleanly (0 NotReady).
