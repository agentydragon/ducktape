# KubeVirt execution environments

Status: the Sandbox Controller runs a Sandbox as a KubeVirt VM, the launcher relay policy and the
`runner-vm` template are deployed on `agentplane-testing`, and the guest runner dials the Runner
Gateway through the relay. The remaining acceptance gates below are independent of initial boot.
The basic VM provider, outbound control path and halted-VM template rerender are implemented:
`TestStartRendersAHaltedVMFromTheTemplateAsItIsThen` verifies a changed guest image applies on start
without replacing the halted VM or its disks. The [deployed suspend/resume](../acceptance/README.md#suspend-and-resume)
and [workspace-storage](../acceptance/README.md#workspace-storage-layout-acceptance) acceptance cases
cover VM session continuity and workspace retention through resume and VM-object replacement. The
open gates below concern failure handling, cross-Sandbox security and resource limits beyond that path.
The design and its constraints are in [VM Sandboxes](../docs/vm_sandboxes.md). Sequencing of the live checks is the
[DAG VM lane](task_dag.md#4-vm-environment-phases); this plan keeps the gates that lane does not
name. Warm pools, memory snapshots, live migration and arbitrary user-supplied VM specifications
stay out of scope.

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
- **Resource behavior not yet exercised on the VM.** Validate the delegated CPU, task and I/O limits,
  plus per-harness memory OOM behavior: an over-budget harness subtree is killed while the Runner
  remains available. Preserve native tool cancellation, Runner crash recovery and competing-owner
  checks where they validate those existing contracts. The Sandbox is the security boundary; no
  same-Sandbox process or Session-store isolation is required.
- **Resource budgets.** Check quotas and capacity for guest RAM plus QEMU overhead, disk helpers and
  the relay, and that a guest is not scheduled on a node without KVM.
