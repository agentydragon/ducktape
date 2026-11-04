# Firecracker system-test runtime

The pytest plugin supplies the Firecracker lifecycle adapter to the shared deployment stack.
It waits for KVM and the shared virtualization controllers, without VM-only admission policies
or a loop-device provisioner. The fixture starts a fresh image store, puts only the disposable
k3s control database on a bounded tmpfs, and captures guest logs through teardown. Retained
executor snapshots are rejected.

`//agentplane/system_tests/suites:test_firecracker` runs the full acceptance scenario in CI
on the dedicated KVM worker. Its boot image is built by Bazel: a pinned Debian cloud kernel,
a BusyBox initramfs loading virtio MMIO/block/network/vsock and filesystem drivers, and a
small ext4 root containing systemd, Codex and the scenario's shell/Python tools. The launcher,
runner disk, Runtime and management agent come from the current source tree. No private boot
image fetch or NixOS build is needed for this test.

Systemd remains because the Runtime uses its transient units and resource slices to supervise
processes. Removing it would change that Runtime contract; shrinking the test guest does not
require that change. The test retains the production mount layout, credential synchronization,
network setup and RAM/process-continuity assertions.

```sh
bbr test --nocache_test_results //agentplane/system_tests/suites:test_firecracker
```

`//agentplane/system_tests/suites:test_firecracker_production` runs the same scenario against
the separately published NixOS guest. Its `production_boot_layout` is a declared Bazel input;
this manual target remains outside automatic CI selection.

The pinned image comes from
`.#agentplane-environment-firecracker-image` at source commit
`57efc97b8c315ababd4046cd40f2bb7f58926dcf`. The Nix archive SHA-256 is
`c008dc2ec34e17ebf396f15d889e5e1b5646444363e0f916dbcbab512362cdd7`; its
OCI manifest is pinned as
`sha256:79cec82bc56731b75128797df9e9d2ef9fd954b60fbed089c5348493b58d1a97`
from the experimental tag
`spike-firecracker-57efc97b8c315ababd4046cd40f2bb7f58926dcf`.

The image contains only the built guest kernel, ext4 root filesystem, and init
path. The guest configuration has no SOPS secrets; runtime TLS credentials and
identity arrive through host-provided mounts and virtio devices when it boots.
The experimental tag does not match the managed Flux policy, which selects
`devel-<timestamp>-<commit>` tags only.

The Forgejo package is private. Bazel's pinned `rules_oci` reads registry auth
from the standard Docker config under `DOCKER_CONFIG/config.json`. Run the
manual test from an authorized local Bazel client with an existing read-capable
Docker config, for example:

```sh
DOCKER_CONFIG=/secure/path/to/docker-config bazelisk test \
  --repo_env=DOCKER_CONFIG \
  --nocache_test_results //agentplane/system_tests/suites:test_firecracker_production
```

The config directory must contain `config.json` and remain mode-restricted.
The Bazel client uses it to fetch the pinned OCI repository; the test and build
actions still use the repository's standard RBE configuration. Do not forward
registry credentials to BuildBuddy runners, put them in command arguments or
logs, or add a persistent registry key for this test. The system test is
tagged `manual` and is excluded from the default CI test selection.

To refresh the pin, publish the same Nix output through the existing
`Agentplane Environment Firecracker Image` workflow, read the resulting image
manifest digest, and update `devinfra/image_pins.json`. Keep the tag and digest
paired so the source artifact is traceable and immutable to Bazel.
