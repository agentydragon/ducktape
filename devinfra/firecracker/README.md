# Firecracker Dev VMs

Warm Firecracker microVMs on wyrm2 for Claude Code development. See
<DESIGN.md> for architecture, prior art, and decisions.

## Components

| Component          | Path                                             | Purpose                            |
| ------------------ | ------------------------------------------------ | ---------------------------------- |
| Design doc         | `DESIGN.md`                                      | Architecture, goals, prior art     |
| VM pod             | `vm_pod/`                                        | Entrypoint for Firecracker VM pods |
| Manager service    | `manager/`                                       | FastAPI VMM, creates VM pods       |
| NixOS rootfs       | `nix/nixos/hosts/fc_dev/`                        | NixOS config for guest rootfs      |
| Rootfs provisioner | `provision-rootfs.sh`                            | Build rootfs via Nix, dd to LV     |
| K8s manifests      | `cluster/k8s/agents/claude-sandbox-firecracker/` | Flux-managed deployment            |
| KVM plugin         | `cluster/k8s/kvm-device-plugin/`                 | Device plugin for `/dev/kvm`       |

## Quick Start

```bash
# Provision base rootfs on wyrm2 (requires Nix + LVM thin pool)
./devinfra/firecracker/provision-rootfs.sh

# Create a VM (from a Claude Code session)
kubectl port-forward -n claude-sandbox svc/firecracker-manager 8080:8080 &
TOKEN="..."
curl -H "Authorization: Bearer $TOKEN" localhost:8080/vms -XPOST \
  -d '{"cpus": 2, "mem_mib": 4096}'
# Boot it
curl -H "Authorization: Bearer $TOKEN" localhost:8080/vms/<id>/boot -XPOST
```

## VM Guest Contents

The NixOS rootfs includes (via `bazel-dev.nix`):

- Bazel 8 + Bazelisk
- Python 3.13, GCC, Clang, Git
- nix-ld (dynamically-linked Bazel toolchains)
- envfs (`/bin/bash` for Bazel sandbox)
- openssh-server
- Dev headers (libssl, libcairo, libdbus)

## Snapshot/Restore

Firecracker supports snapshotting VM memory + CPU state to disk and
restoring from it in ~28ms. A "warm" snapshot with a hot Bazel JVM +
Skyframe cache would make queries take ~0.3s instead of ~15s cold.

See `manager/snapshots.py` and `manager/clients.py` for the implementation.
