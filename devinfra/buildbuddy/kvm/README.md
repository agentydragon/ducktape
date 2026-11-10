# BuildBuddy KVM executors

The `buildbuddy-executor` HelmRelease configures pool `wyrm2-kvm`, disabled with
zero replicas. Helm reconciliation remains enabled so the replica count is applied;
suspending the HelmRelease alone would leave the executor running. When enabled, OCI actions
receive `/dev/kvm` through BuildBuddy's OCI device configuration. Its
`child_cgroups_enabled` setting keeps action processes in a child cgroup of the
Kubernetes container's resource-limited cgroup. The pinned executor image
starts under Tini; both releases set `child_cgroups_move_tini` so BuildBuddy
moves Tini and the executor into the executor child cgroup before delegating
controllers to the task cgroup.

The separate `buildbuddy-vm-driver` HelmRelease advertises
`wyrm2-vm-tests`, requests one KVM device on `wyrm2`, and enables only
Firecracker isolation. It is suspended until the Firecracker runtime is
verified on the node. The driver uses
`executor.max_concurrent_tasks: 1`; unspecified isolation defaults to
Firecracker, and explicit OCI or bare (`none`) requests are unsupported by this
pool. The pinned Helm chart makes the executor Pod privileged for Firecracker;
the driver adds no action-level OCI devices or capabilities. The 8 GiB Pod
limit bounds all processes;
`executor.oci.enable_cgroup_memory_limit` also enables the memory controller
used by task cgroups, but the OCI-specific per-task `memory.max` limit does not
apply to Firecracker guests.
The Flux owner's health check covers the active OCI release while the new driver
is suspended. Add the driver's HelmRelease to that check when activating it.
The driver's cache is capped at 8 GB; its 30 GiB ephemeral-storage limit is a
kubelet eviction threshold, rather than a filesystem quota.

This smoke test checks that the selected executor can open `/dev/kvm`, reports
KVM API version 12, and creates a VM and vCPU. It logs `CapEff` from
`/proc/self/status` for executor diagnostics. It does not run guest code or
verify Firecracker.

From the repository root, run it after enabling the `wyrm2-kvm` executor:

```bash
bbr test //devinfra/buildbuddy/kvm:kvm_test --nocache_test_results
```

## Agentplane VM system-test lane

Adoption for ordinary CI is deferred. The manual lane has executed all three
VM scenarios with actual KVM acceleration, but the full target timed out during
final cleanup after over ten minutes of execution. See the
[validation and latency record](../debug/2026_11_03_wyrm2_kvm_worker.md)
for the measured phases and remaining adoption requirements.

The manual `kvm_vm_test` target runs the same `/dev/kvm` smoke check on pool
`kvm-vm`, using Docker workload isolation. After that pool is deployed,
`//agentplane/system_tests/x/vm_kvm:test_vm_kvm` runs the existing `test_vm.py`
scenarios with KubeVirt hardware acceleration (`useEmulation: false`). Both
targets inherit the repository's pinned Ubuntu RBE action image and set
`init-dockerd=false`; the worker guest's Docker executor runs the action, while
the K3s fixture uses its own containerd and needs no nested Docker daemon.
Before starting K3s, the suite opens `/dev/kvm`, checks API version 12, and
creates a temporary VM and vCPU.

The KVM suite starts and removes its K3s stack inside each action. It does not
opt into BuildBuddy's retained-baseline or remote-snapshot settings. The worker
VM can persist between actions, with its executor limited to one concurrent
task. This is a trusted-code lane: the privileged action can access `/dev/kvm`,
and root in the test can control its guest. It does not provide isolation
between mutually hostile jobs.

Run the smoke check first, then the full VM system-test suite:

```bash
bbr test //devinfra/buildbuddy/kvm:kvm_vm_test --nocache_test_results
bbr test //agentplane/system_tests/x/vm_kvm:test_vm_kvm --nocache_test_results
```
