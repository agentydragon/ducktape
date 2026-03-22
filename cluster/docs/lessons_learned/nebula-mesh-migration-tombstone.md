# Nebula Mesh Migration (Completed 2026-03)

KubeSpan (Talos built-in WireGuard mesh) replaced by Nebula. KubeSpan lacked relay capability — symmetric NAT nodes couldn't communicate. Nebula provides relays via lighthouses.

- KubeSpan disabled on all Talos nodes
- Nebula deployed on all nodes (Talos extension + NixOS service)
- kubespand (custom KubeSpan reimplementation) decommissioned

See `cluster/docs/lessons_learned/kubespand-tombstone.md` for the kubespand post-mortem.
Nebula PKI managed in `persistent-auth/nebula.tf`.
