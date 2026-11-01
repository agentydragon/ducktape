# KubeVirt execution environments

The live gates for VM Sandboxes that no [DAG](task_dag.md#4-vm-environment-phases) node names. The design and its
constraints are in [VM Sandboxes](../docs/vm_sandboxes.md); warm pools, memory snapshots, live migration and
arbitrary user-supplied VM specifications are out of scope.

## Open gates

- **SQLite and the owner lock on virtiofs.** The runner journal commits with `journal_mode=DELETE`
  and `synchronous=EXTRA` and fences with a nonblocking lifetime lock on the state directory
  ([Harness Host retained-state contract](../env/harnesshost/docs/retained_state.md#sqlite-storage)). Both assume storage that honours sync and
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
  In the k3s VM stack, which runs Kyverno's admission controller and the launcher relay policy
  ([fixture](../system_tests/k3s/README.md)), scale the admission controller to zero and show that a VM launch
  fails while unrelated Pods still schedule and running VMs keep running.
- **Relay lifecycle.** The relay is an ordinary container in the launcher. Check its restart and
  termination against KubeVirt's launcher monitoring before choosing between ordinary and native
  sidecar lifecycle. Never patch a live launcher.
- **Resource budgets.** Check quotas and capacity for guest RAM plus QEMU overhead, disk helpers and
  the relay, and that a guest is not scheduled on a node without KVM.
