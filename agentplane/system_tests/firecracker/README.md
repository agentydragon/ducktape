# Firecracker system-test runtime

The pytest plugin supplies the Firecracker lifecycle adapter to the shared deployment stack.
It advertises the real KVM device through a dedicated generic-device-plugin, retaining the
production launcher resource request without KubeVirt, Kyverno or a loop-device provisioner. The fixture keeps the worker's digest-addressed image store between actions, dropping it when the
last stack did not stop cleanly or free space falls below the scheduled headroom
(`K3s.image_store_is_retainable`). It puts only the disposable k3s control database on a bounded
tmpfs and captures guest logs through teardown. Retained executor snapshots are rejected.

`//agentplane/system_tests/suites:test_firecracker` runs the full acceptance scenario in CI
on the dedicated KVM worker. Its boot image is built by Bazel: a pinned Debian cloud kernel,
a BusyBox initramfs loading virtio MMIO/block/network/vsock and filesystem drivers, and a
small ext4 root containing systemd, Codex and the scenario's shell/Python tools. The launcher,
runner disk, Runtime and management agent come from the current source tree. No private boot
image fetch or NixOS build is needed for this test.

Systemd remains because the Runtime uses its transient units and resource slices to supervise
processes. Removing it would change that Runtime contract; shrinking the test guest does not
require that change. The test retains the production mount layout, credential synchronization,
network setup and RAM/process-continuity assertions. It reaches the checkpoint through idle
suspension, exercising the conditional Runtime admission barrier and its release after thaw.

```sh
bbr test --nocache_test_results //agentplane/system_tests/suites:test_firecracker
```
