# Roaming Laptop Worker Node

**Status**: Implemented (2026-03). rugged (Dell Rugged 12) joined as roaming worker
via `nix/nixos/hosts/rugged/` + `k8s-worker.nix` + `nebula-mesh.nix`. Nebula mesh
with VPS lighthouses/relays solves the double-NAT hole-punching problem that made
KubeSpan unreliable. See `nebula-mesh-migration.md` and `kubespand-tombstone.md`
for the full history.

**TODO**: Verify double-NAT passthrough and relay with real workloads (e.g., rugged
behind a mobile hotspot or restrictive corporate NAT, running actual pods with
cross-node traffic).

## How It Works

NixOS laptop joins the Talos k8s cluster as a worker without VMs:

- **`nebula-mesh.nix`** — joins the Nebula mesh (UDP 4242). VPS lighthouses provide
  peer discovery; relay mode handles double-NAT when hole-punching fails.
- **`k8s-worker.nix`** — containerd + kubelet, haproxy on `localhost:7445`
  load-balancing across control plane Nebula IPs, sops-nix for credentials.
- **Cilium agent** runs as a DaemonSet, VXLAN encapsulated inside Nebula.

Key files:

- `nix/nixos/hosts/rugged/default.nix` — host config
- `nix/nixos/modules/k8s-worker.nix` — k8s worker module
- `nix/nixos/modules/nebula-mesh.nix` — Nebula mesh module
- `cluster/terraform/bootstrap/persistent-auth/nebula.tf` — PKI

## Networking

| Property   | Value                                                  |
| ---------- | ------------------------------------------------------ |
| Mesh       | Nebula (UDP 4242), lighthouses on VPS                  |
| Node IP    | Nebula mesh IP (e.g., `10.42.0.30` for rugged)         |
| API server | haproxy `localhost:7445` → CP Nebula IPs               |
| Pod overlay| VXLAN (UDP 8472) encapsulated in Nebula                |
| Relay      | VPS lighthouses relay when hole-punching fails         |

## Scheduling

Taint `node-role.kubernetes.io/roaming=true:NoSchedule`, labels
`topology.kubernetes.io/region=roaming`, `node.kubernetes.io/role=roaming`.

**Good fit**: BuildBuddy executors, batch ML/LLM jobs, CI runners, dev/test workloads.
**Avoid**: StatefulSets, PVCs, ingress, anything in the VPS-only resilience invariant.

## Intermittent Connectivity

- Node goes `NotReady` after ~40s (`node-monitor-grace-period`)
- Pods evicted after 5 minutes (default `tolerationSeconds`)
- Taint ensures only opt-in workloads land there
- No PVCs — stateless workloads only

## Known Gotchas

- **API endpoint**: Cilium uses `k8sServiceHost: localhost`, `k8sServicePort: 7445`.
  Talos nodes use KubePrism; NixOS workers use haproxy.
- **Cilium `SYS_MODULE`**: Pre-load `sch_ingress` etc. on non-Talos nodes.
- **Kubelet version**: Must be within 1 minor version of the cluster.
- **Kubelet label restrictions**: `node-role.kubernetes.io/*` labels rejected by
  kubelet — use `node.kubernetes.io/role` instead. Taints are fine.

## Historical Notes

This plan went through three networking iterations:
1. Tailscale/Headscale (second WireGuard mesh, required DaemonSet)
2. KubeSpan/kubespand (reimplemented Talos KubeSpan for Linux — failed on double-NAT,
   no relay capability). See `../lessons_learned/kubespand-tombstone.md`.
3. Nebula mesh (current) — lighthouses + relays solve double-NAT reliably.
