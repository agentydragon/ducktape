# KubeVirt execution environments

Status: the Sandbox Controller runs a Sandbox as a KubeVirt VM, the launcher relay policy and the
`runner-vm` template are deployed on `agentplane-testing`, and the guest runner dials the Runner
Gateway through the relay. The remaining acceptance gates below are independent of initial boot.
The design and its constraints are in [VM Sandboxes](../docs/vm_sandboxes.md). Sequencing of the live checks is the
[DAG VM lane](task_dag.md#4-vm-environment-phases); this plan keeps the gates that lane does not
name. Warm pools, memory snapshots, live migration and arbitrary user-supplied VM specifications
stay out of scope.

## Storage rollout

[Storage layouts](../docs/sandbox_storage.md) separates Sandbox ownership from content format and
attachment compatibility. #10440 adds the declared workspace contract. [Testing storage acceptance](../acceptance/debug/storage_acceptance.md)
records image-pin gating, the historical comparison with the old DataVolume path, and selection of
the retained path. Typed workspace templates from #10507 have reconciled in testing and staging;
the operator-directed incompatible cutoff is complete ([evidence](workspace_cutoff.md)). This
follow-up retires the superseded blank-DataVolume production workspace path while retaining generic
DataVolume disks in custom VM templates. The guest shell-to-NixOS work is
`VM_GUEST_DECLARATIVE_STORAGE`. Conversion is deferred to
`VOLUME_LAYOUT_CONVERSION`, much later, and does not gate this rollout. A filesystem-backed raw
`disk.img` and an ordinary Pod directory are explicitly incompatible without such a conversion.

## Open gates

- **SQLite and the owner lock on virtiofs.** The runner journal commits with `journal_mode=DELETE`
  and `synchronous=EXTRA` and fences with a nonblocking lifetime lock on the state directory
  ([runner README](../runner/README.md#sqlite-storage)). Both assume storage that honours sync and
  locks. Prove them on the `/state` virtiofs mount through KubeVirt's `virtiofsd` cache mode:
  crash-recover a journal commit by killing the guest mid-write, and show a second runner on the same
  state exits on the lock.
- **Node loss.** `local-path` state is node-local. Decide what a VM does when its node is lost: no
  second VMI may open the PVC until the old instance is confirmed terminated, so a force replacement
  on an unreachable node needs node fencing. Until decided, node loss is not automatic failover.
- **Migration and eviction.** Nothing in the template stops KubeVirt from live-migrating a VM or
  evicting it with a migration. Reject both for these VMs and prevent unauthorized migration
  creation.
- **Admission outage.** The launcher policy's failure scope is designed to block only VM launches.
  Stop Kyverno on `agentplane-testing` and show that unrelated Pods still schedule and running VMs
  keep running.
- **Relay lifecycle.** The relay is an ordinary container in the launcher. Check its restart and
  termination against KubeVirt's launcher monitoring before choosing between ordinary and native
  sidecar lifecycle. Never patch a live launcher.
- **Real egress gateway.** The prototype fixtures validated bearers with TokenReview. A VM Sandbox
  must show credential substitution and `CONNECT`/TLS through the production gateway for LLM and
  Action traffic, not only the control routes.
- **Isolation limits not yet exercised.** Pressure on CPU, task and I/O limits; native tool
  cancellation; killing the runner mid-write; competing runner ownership; replacing the root image
  after native histories exist. Test only those that bear on a stated guarantee, with a stopping
  condition; the harness-process boundary itself is `VM_PROCESS_ISOLATION`.
- **Resource budgets.** Check quotas and capacity for guest RAM plus QEMU overhead, disk helpers and
  the relay, and that a guest is not scheduled on a node without KVM.
