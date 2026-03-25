# Nix Remote Builders on Kubernetes

Options for offloading `nix build` to the cluster, surveyed 2026-03-25.

## Background

The existing Nix setup in this repo:

- **Attic binary cache** at `cache.allegedly.works` (see <nix-cache.md>) — serves built
  derivations to NixOS hosts and CI, but doesn't do the building.
- **GitHub Actions CI** builds Nix packages on GitHub-hosted runners and pushes to Attic.
- **Local builds** run on `agentydragon` (ThinkPad) and `wyrm2` (NixOS GPU worker joined
  to the cluster).

A remote builder would let `nix build` on any host offload derivations to in-cluster
compute, useful when the local machine is slow (especially for cross-compilation or
large builds) and `wyrm2` isn't available.

## Landscape Summary

No mature, production-ready Helm chart or kustomize config exists specifically for
Nix remote builders in Kubernetes (as of 2026-03-25). The space is dominated by one
widely-used container image and a few experimental k8s-specific projects.

### `lnl7/nix:ssh` — The Standard Builder Image

The de facto container image for SSH-based Nix remote building. A minimal image
(built from scratch, not Alpine) containing `nix`, `bash`, `coreutils`, and `openssh`.
The `:ssh` tag runs `sshd` and is designed for use as a remote builder.

Not Kubernetes-native, but works as a Deployment/StatefulSet with an SSH Service.
Widely referenced; somewhat stale (~2020 activity).

**Usage pattern** (no existing Helm chart — must write manifests):

1. Deploy as a StatefulSet with a PVC for `/nix/store` (re-using the store across builds
   is important for performance)
2. Mount `authorized_keys` from a Secret
3. Mount a `nix.conf` ConfigMap with `trusted-users = root` (required for remote builds)
4. Expose port 22 via a ClusterIP Service
5. Configure the client:

   ```
   builders = ssh-ng://root@nix-builder.nix-builder.svc.cluster.local x86_64-linux - 4 1 nixos-test,big-parallel,benchmark,kvm
   ```

**Considerations for this cluster**:

- Storage on `proxmox-csi-retain` (Proxmox-only; tolerates downtime by design — see
  <plan.md> "Proxmox-dependent services").
- Should integrate with the existing Attic cache — build outputs can be pushed to
  `cache.allegedly.works` after building so subsequent builds are served from cache.
- `wyrm2` is already a NixOS node with Nix installed — it could serve as a builder
  directly rather than adding a dedicated pod.

### `omarjatoi/nix-remote-build-controller` — CRD-based

A Go controller that watches `NixBuildRequest` CRDs and creates ephemeral builder pods
per build. An SSH proxy accepts Nix connections and routes them to pods.

Architecture is sound (ephemeral pod-per-build scales better than a single StatefulSet),
but explicitly documented as incomplete and non-functional as of September 2025.

### `garnix-io/yensid` — SSH Load Balancer for Builder Pools

An SSH proxy with least-connections load balancing across a pool of builders, backed by
a certificate authority so builders can rotate without reconfiguring clients. Well-designed
and actively maintained by Garnix (a Nix CI company).

No Kubernetes support — deploys as NixOS modules only. Relevant if adding multiple
`wyrm2`-style NixOS workers.

### `lillecarl/nix-csi` — CSI Driver Mounting `/nix` into Pods

A Kubernetes CSI driver that mounts `/nix/store` paths into pods as ephemeral volumes,
evaluating flake references on-demand at pod schedule time. The adjacent use case:
instead of building and publishing container images, pods declare Nix derivations as
their runtime environment.

Active development (v0.4.3, February 2026). Experimental but the most actively maintained
k8s+Nix project currently. Not a remote builder per se.

### `pdtpartners/nix-snapshotter` — Nix-Native containerd Snapshotter

A containerd plugin letting Kubernetes pull "images" directly from the Nix store or
a binary cache, bypassing Docker registries. Useful for teams fully committed to NixOS
nodes.

## Recommendation

**Use `wyrm2` as a remote builder directly.** `wyrm2` is already a NixOS worker in the
cluster with Nix installed, on the Nebula mesh. No additional k8s manifests needed:

1. Configure SSH access to `wyrm2` from build clients (authorized key from Vault/ESO
   or SealedSecret)
2. Add `wyrm2` to `nix.settings.trusted-users` (already in `k8s-worker.nix`)
3. Configure clients:

   ```nix
   nix.settings.builders = "ssh-ng://user@wyrm2 x86_64-linux aarch64-linux - 8 1";
   ```

If `wyrm2` availability isn't sufficient and a dedicated in-cluster builder is needed,
write a StatefulSet using `lnl7/nix:ssh` (or a custom nixpkgs-based image) with a
`proxmox-csi-retain` PVC. Integrate with Attic by running `attic push main` after each
build.

The `omarjatoi/nix-remote-build-controller` is worth revisiting once it matures —
ephemeral pod-per-build is the right architecture for scaling.

## References

- [LnL7/nix-docker](https://github.com/LnL7/nix-docker)
- [omarjatoi/nix-remote-build-controller](https://github.com/omarjatoi/nix-remote-build-controller)
- [garnix-io/yensid](https://github.com/garnix-io/yensid)
- [lillecarl/nix-csi](https://github.com/lillecarl/nix-csi)
- [pdtpartners/nix-snapshotter](https://github.com/pdtpartners/nix-snapshotter)
- [NixOS Wiki — Distributed build](https://wiki.nixos.org/wiki/Distributed_build)
