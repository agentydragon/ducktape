# Prebuilt NixOS KVM worker image

This directory defines the prebuilt NixOS qcow2 guest and KubeVirt
`containerDisk` used by the [KVM worker pool](../../../../../cluster/k8s/buildbuddy-kvm-worker/README.md).
The package and registry repository retain their original spike names.
Production pins a qualified immutable digest manually; the separate canary
advertises `kvm-vm-nixos-spike` and takes its pool name from the runtime config disk.
A successful image build or publish does not promote it into production.

Build the registered image with:

```sh
nix build .#buildbuddy-kvm-worker-spike-container-disk
```

The guest image includes Docker, containerd, node exporter, nested-KVM setup,
the worker systemd services, and a locally archived BuildBuddy executor image.
The executor archive is pinned to upstream digest
`sha256:465d670159f7f2be756eb31815d7c6be54ce812de74a5cc49d3f6bda1fc7b078`;
the Nix fixed-output hash is recorded in `nixos.nix`. Starting the guest does
not install packages, fetch the executor image, rebuild NixOS, or reboot for
configuration changes. Docker is configured to use the guest's external
containerd service and both runtime stores live on the per-VM work disk.
See the [canary qualification guide](../nixos_worker_qualification/README.md)
for its one-member VM pool and runtime validation.

KubeVirt supplies three serial disks using the existing production disk names
and mount points:

| Disk serial | Guest mount              | Runtime contents                                                           |
| ----------- | ------------------------ | -------------------------------------------------------------------------- |
| `work`      | `/buildbuddy`            | Per-VM ext4 disk; initialized once if blank; Docker and containerd stores. |
| `bbkey`     | `/run/buildbuddy-key`    | `api-key`; supplied at runtime and never included in the image.            |
| `bbconfig`  | `/run/buildbuddy-config` | `executor.yaml`, `k3s.yaml`, `registries.yaml`, and `pool`.                |

The executor service waits for these disks and fails if any required runtime
file is absent. `pool` must contain exactly one non-empty line; there is no
production-pool fallback. `k3s.yaml` and `registries.yaml` are copied into a
real runtime directory and exposed at `/etc/rancher/k3s` for the executor's
container bind mount. Each VM reports its SMBIOS product UUID as its unique
executor hostname.

The guest loads the worker's executor configuration from the ConfigMap. It preserves
the advertised 4 vCPUs and 7 GiB memory, 24-hour task limit plus shutdown grace,
Docker seccomp profile, ext4 data disk, nested KVM and kernel/sysctl settings,
and disabled SSH. Node exporter listens on port 9100; the executor's host
metrics and readiness endpoints use ports 9090 and 8080.
