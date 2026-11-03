# BuildBuddy KVM executors

The `buildbuddy-executor` HelmRelease advertises pool `wyrm2-kvm`; OCI actions
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

From the repository root, run it after the `wyrm2-kvm` executor is deployed:

```bash
bbr test //devinfra/buildbuddy/kvm:kvm_test --nocache_test_results
```
