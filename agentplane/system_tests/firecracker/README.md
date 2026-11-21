# Firecracker system-test runtime

The pytest plugin supplies the Firecracker lifecycle adapter to the shared deployment stack.
It advertises the real KVM device through a dedicated generic-device-plugin, retaining the
production launcher resource request without KubeVirt, Kyverno or a loop-device provisioner.
The required suite retains K3s and PKI in its recycled Docker runner on a private ext4 disk.
A versioned infrastructure key and runtime fingerprint invalidate incompatible baselines;
missing, unhealthy or dirty baselines rebuild cold. Each action recreates application workloads,
credentials, storage and the KVM device plugin. Cleanup must complete before the baseline is
marked clean; failures discard the owned runner. Guest logs and `persistence-suite.json` remain
per-action outputs, while `recycled-runner.json` records the action and retained-runner chain.

`//agentplane/system_tests/suites:test_firecracker` runs the full acceptance scenario in CI
on the dedicated KVM worker. Its boot image is built by Bazel: a pinned Debian cloud kernel,
a BusyBox initramfs loading virtio MMIO/block/network/vsock and filesystem drivers, and a
small ext4 root containing systemd, Codex and the scenario's shell/Python tools. The launcher,
runner disk, Runtime and management agent come from the current source tree. No private boot
image fetch or NixOS build is needed for this test.

Systemd remains because the Runtime uses its transient units and resource slices to supervise
processes. Removing it would change that Runtime contract; shrinking the test guest does not
require that change. The test retains the production mount layout, credential synchronization,
network setup and RAM/process-continuity assertions. The first checkpoint uses idle suspension,
exercising the conditional Runtime admission barrier and its release after thaw. A second
checkpoint runs after the restored guest has used its live working set. The test preserves
launcher cgroup usage, high-water mark, memory counters and OOM events in
`firecracker/launcher-memory.jsonl`.

```sh
bbr test --nocache_test_results //agentplane/system_tests/suites:test_firecracker
```

`//agentplane/system_tests/suites:test_firecracker_cold` runs the same two scenarios with Docker
recycling disabled and explicitly discards its baseline after teardown. The
[`Firecracker cold start` workflow](../../../.github/workflows/firecracker-cold.yml) runs it
uncached nightly and on demand. Cold-start regressions therefore have a separate daily signal;
PR checks retain all scenario assertions but may reuse healthy infrastructure.

```sh
bbr test --nocache_test_results --flaky_test_attempts=1 \
  //agentplane/system_tests/suites:test_firecracker_cold
```
