# Cluster Network Architecture

Single source of truth for the cluster's network layering, encapsulation, and MTU
model. Other docs (README, troubleshooting, plan, debug notes) should point here
rather than restating the math.

## Layers (innermost → outermost)

A cross-node pod packet is encapsulated twice, nested:

| Layer        | Carries                      | Adds          | Device MTU |
| ------------ | ---------------------------- | ------------- | ---------- |
| pod          | application traffic          | —             | 1370       |
| Cilium VXLAN | the pod packet               | +50           | —          |
| `nebula1`    | the VXLAN packet (encrypted) | +60 (AES-GCM) | 1420       |
| `eno1`       | the Nebula packet (UDP 4242) | —             | 1500       |

Cilium VXLAN tunnels pod traffic to each remote node's **InternalIP**, which is its
**Nebula `10.42.x` address** — so VXLAN rides _inside_ Nebula, which rides on `eno1`.
The encapsulations **stack** (nested); they do not run in parallel. OVH same-`/24`
peers have no L2 adjacency, so `eno1` traffic to a neighbour hairpins through the
`.254` gateway (see below).

## Routing: OVH same-subnet hairpin

OVH dedicated servers have **no L2 adjacency** to other servers in the same public
`/24` — the gateway (`.254`) sits outside the host's subnet and all peer traffic is
routed (hairpinned) through it. `kimsufi_eno1_peer_routes` (in `ovh-nodes.tf`) adds a
static `/32` route to each same-`/24` peer via `.254`. This underlay carries the
Nebula mesh. vRack (real private L2) is **not** available on Kimsufi/Eco lines.

## MTU stack

| Layer           | MTU      | Note                                                    |
| --------------- | -------- | ------------------------------------------------------- |
| `eno1` underlay | **1500** | Measured ceiling; no jumbo (vRack-only, not on Kimsufi) |
| `nebula1` tun   | **1420** | `nebula.tf` `tun.mtu`; `1420 + 60 = 1480` (20 B margin) |
| Cilium `MTU`    | **1420** | = the underlay (`nebula1`) MTU — see gotcha below       |
| cross-node pod  | **1370** | Cilium derives `1420 − 50` (VXLAN); `1370 + 50 = 1420`  |

Full-size cross-node pod packet, end to end:
`pod 1370 + VXLAN 50 = 1420` (fits `nebula1` exactly) `+ Nebula 60 = 1480` (fits
`eno1` 1500, 20-byte margin). **Zero fragmentation.**

### ⚠️ Cilium `MTU` is the UNDERLAY MTU, not the pod MTU

Cilium's `MTU` Helm value sets the underlay-device MTU (the `cilium_*`
interfaces) and subtracts the 50-byte VXLAN overhead itself for the pod route
MTU. So:

- Set `MTU` to the **device Cilium tunnels over** = `nebula1` = **1420** —
  pods get `1420 − 50 = 1370`.
- Setting `MTU: 1370` (the desired pod MTU) is **wrong** — it yields a **1320** pod
  MTU (measured 2026-06-08). This mistake cost a wrong rollout; see the debug note.

### Encapsulation overheads (measured)

- **Nebula = 60 bytes** (16 header + 16 AES-GCM tag + 8 UDP + 20 IPv4). Confirmed by
  tcpdump: a 1300-byte inner packet → 1360 on the wire.
- **Cilium VXLAN = 50 bytes** (Cilium's `EncapOverhead` for VXLAN).

Hard max for this stack: `nebula1 = 1440`, `MTU = 1440` → pod 1390 (exact-fit,
`1440 + 60 = 1500`). We keep a 20-byte underlay margin at `nebula1 = 1420`.

## Config locations

| What                          | File                                        |
| ----------------------------- | ------------------------------------------- |
| Cilium `MTU`, tunnel          | `cluster/terraform/main/cilium-values.yaml` |
| Nebula `tun.mtu`, PKI         | `cluster/terraform/main/nebula.tf`          |
| OVH peer `/32` routes         | `cluster/terraform/main/ovh-nodes.tf`       |
| Per-destination MTU policy    | `nebula-mesh.json` (repo root)              |
| NixOS peer-route consumer     | `nix/nixos/modules/nebula.nix`              |
| Talos peer-route consumer     | `cluster/terraform/main/nebula.tf`          |
| Ansible peer-route consumer   | `ansible/roles/nebula`                      |
| Mesh roster and policy schema | `cluster/scripts/nebula_mesh.py`            |

The cluster-wide Cilium/Nebula settings are managed by OpenTofu, Talos machine
config, and Helm. Host-specific route MTUs originate in the mesh roster and are
rendered by each host manager. None are Flux resources.

Gotcha: Nebula's `lighthouse.local_allow_list` (in `nebula.tf` and
`nix/nixos/modules/nebula.nix`) must keep excluding `cilium*`/`lxc*` interfaces
from endpoint advertisement — otherwise overlay pod IPs get advertised as
Nebula endpoints and you get a tunnel-in-tunnel amplification loop. Incident:
<lessons_learned/2026_04_07_nebula_cilium_vxlan_loop.md>.

## Changing MTUs safely (live, no re-bootstrap)

1. **Nebula** (`tun.mtu`): flows into each node's Talos machine config. Apply
   **per node**, one at a time, with health checks between — reloading nebula blips
   that node's mesh link, so do etcd voters sequentially (workers first, the
   etcd+DB-primary node last) and confirm etcd quorum + node Ready after each:
   `tofu apply -target='talos_machine_configuration_apply.kimsufi["<key>"]'`.
2. **Cilium** (`MTU`): `null_resource.cilium_bootstrap` is recreated when the
   chart version or `cilium-values.yaml` changes. Create and inspect a saved target
   plan before applying it: this resource retains bootstrap dependencies, so a root
   `-target` plan can include Talos prerequisites as well as Cilium. Do not apply that
   broader plan as an in-place Cilium update. Its Helm upgrade deliberately omits
   `--wait`/`--atomic`: a Pending DaemonSet pod on a down node, e.g. `wyrm2`, never
   reports "updated", so `--wait` times out and `--atomic` rolls back. The DaemonSet
   rolls the healthy nodes; monitor manually.
3. **Verify** with a DF-ping ladder from a `hostNetwork` pod (nebula path) and a
   normal pod-to-pod ping (overlay path). Recreate test pods after a Cilium MTU
   change — existing pods keep their old veth MTU until recreated.

## Caveat: roaming nodes

A single global Cilium `MTU` assumes a 1500 underlay. `rugged` (roaming laptop on
cellular) has a historically smaller **Cilium-over-Nebula** usable path (see
`cluster/debug/2026-06-02-tofu-apply-hangs-from-rugged-mtu.md`). That result
includes both overlays; it is not a direct-Fi or native-IPv6 measurement. The
fix for roaming nodes is a measured host-specific MSS/PMTU mitigation, not the
global MTU. In particular, a generic route-MTU clamp at `nebula1` may observe
only its inner 1420-byte TUN MTU rather than the cellular underlay limit.

Rugged therefore declares `destination_mtu: 1100` in `nebula-mesh.json` while
keeping both Cilium and the `nebula1` device at 1420. Consumers turn that one
policy into symmetric exact routes: Rugged uses 1100 toward every peer, and
every other managed Linux peer uses 1100 only toward Rugged. Thus direct and
relayed packets involving Rugged fit its 1200-byte Fi underlay without changing
the MTU between other managed Linux pairs. The mobile-wide fallback described
below is the deliberate exception.

For bulk IPv4 TCP, moving from an inner MTU of 1420 to 1100 increases wire bytes
by roughly 2–3%, although it produces about 30% more packets and per-packet
crypto work. That is a reasonable permanent tradeoff for one roaming node; it
is not a reason to lower the mesh-wide MTU. Android exposes one VPN-interface
MTU rather than Linux-style per-route MTUs, so the generated Mobile Nebula
import uses the smallest declared `destination_mtu` as its global TUN MTU. This
lowers only the mobile client's mesh traffic; regenerate and re-import its
configuration after a constraint changes.

Do not express the smaller routes as Nebula `tun.routes` while the upstream
Linux TUN initialization bug compares route MTUs against the old zero
`MaxMTU`: on startup it can lower the TUN device itself to 1100. NixOS and
Ansible deliberately install kernel routes after Nebula starts, while Talos
reconciles the same kernel route outside Nebula.

## References

- Measurements + the 2026-06-08 incident: `cluster/debug/2026-06-08-nebula-vxlan-mtu/`
- Earlier cross-node MTU loss postmortem: `cluster/docs/lessons_learned/2026_02_11_cilium_mtu_cross_node_packet_loss.md`
- Uppercase-`MTU` Helm-key gotcha: `cluster/docs/troubleshooting.md` § "MTU Case Sensitivity"
