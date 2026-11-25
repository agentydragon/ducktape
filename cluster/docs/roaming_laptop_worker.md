# Roaming laptop workers

[`rugged`](../../nix/nixos/hosts/rugged/default.nix) and
[`iguana`](../../nix/nixos/hosts/iguana/default.nix) join the Talos cluster
through the NixOS [worker module](../../nix/nixos/modules/k8s-worker.nix).
It runs containerd and kubelet, takes the bootstrap kubeconfig from sops-nix,
and serves the local API endpoint through HAProxy. Cilium uses the same
`localhost:7445` endpoint on Talos through KubePrism.
[Cilium values](../terraform/main/cilium-values.yaml).

[Nebula membership](mesh_membership.md) owns discovery and relay setup.
[Cluster networking](network.md) owns the VXLAN-over-Nebula path.
The [mesh migration record](lessons_learned/nebula_mesh_migration_tombstone.md)
explains the double-NAT failure that led to replacing KubeSpan.

## Scheduling and recovery

- The [roaming taint](mesh_membership.md#roaming-k8s-nodes) limits placement to
  opt-in work such as CI executors, batch jobs, local inference and development.
  Keep ingress and services required for OVH-only operation on stable nodes.
- Prefer disposable workloads. For intentional node-local data, use the
  retaining `piraeus-ssd-local` class. [Piraeus storage classes](piraeus.md#storage-classes).
  Its claim stays bound to the laptop while the laptop is offline.
- The [roaming node controller](../roaming_node_controller/README.md) owns
  tainting, eviction and return behavior.

## Host compatibility

- The shared Cilium chart omits `SYS_MODULE` for Talos. Supply required kernel
  features on NixOS, including ingress traffic control. Load modular features
  on the host before Cilium starts. [Cilium kernel requirements](https://docs.cilium.io/en/stable/operations/system_requirements/).
- Check the kubelet against every API server during upgrades.
  [Kubernetes version skew policy](https://kubernetes.io/releases/version-skew-policy/#kubelet).
- Use `node.kubernetes.io/role` for the kubelet-set role label.
  `node-role.kubernetes.io/roaming` is the taint key.
