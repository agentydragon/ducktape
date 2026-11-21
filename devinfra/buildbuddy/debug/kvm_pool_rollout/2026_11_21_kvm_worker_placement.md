# KVM worker placement, 21 November 2026

Keep `wyrm2` eligible for the production `kvm-vm` pool. Revisit placement if
guest teardown failures recur enough to affect CI. The
[pool runbook](../../../../cluster/k8s/buildbuddy-kvm-worker/README.md) owns the
production configuration and operating procedure.

[PR #18613](https://github.com/agentydragon/ducktape/pull/18613) recorded the
live expansion of wyrm2's Piraeus SSD backing disk from 150 to 600 GiB.
[PR #18615](https://github.com/agentydragon/ducktape/pull/18615) removed the
OVH-only node selector. The nested-KVM requirement and the node-local
`piraeus-ssd-local-delete` work claims still constrain placement. Live inspection
on 21 November found `buildbuddy-kvm-worker-1` Ready on `wyrm2` with a bound
`work-1` SSD claim. Workers on OVH nodes were also Ready.

## Pinned SSD comparison

Both workers ran `//agentplane/system_tests/x/vm_kvm:test_vm_kvm` at
`0d1f154903b9e9c58b1c7fb5f4484651b800caf2`. Each run built a fresh K3s fixture
on a worker with warm caches, with the action pinned to a registered executor
ID. The table uses the TestRunner's `executionStartTimestamp` through
`executionCompletedTimestamp`. Queueing, input fetch and output upload are
outside that interval. The production suite's retained fixture needs its own
measurement.

| Run             | OVH control plane (`ovh-ns104963`)                                                          | `wyrm2`                                                                                               |
| --------------- | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Pair 1          | [207.97 s, pass](https://app.buildbuddy.io/invocation/ea50b356-e020-4547-a60d-c135ceffc497) | [369.01 s, pass](https://app.buildbuddy.io/invocation/2cb2fa36-535d-4b57-9ccc-7e0ee1f754e2)           |
| Pair 2          | [207.70 s, pass](https://app.buildbuddy.io/invocation/7b2be7f7-3949-4d55-aa2c-bad8ac3dc917) | [355.85 s, teardown error](https://app.buildbuddy.io/invocation/e03dc565-cb69-42df-a7fc-cbc10be52e05) |
| Profiled repeat | —                                                                                           | [336.52 s, pass](https://app.buildbuddy.io/invocation/3f17c33a-f4f6-42e9-889e-8a40a400463f)           |

Another worker booted on `wyrm2` during pair 1. Pair 2 ran without that boot.
Both scenario test cases passed in pair 2 before teardown failed.

Pair 1's two guest consoles took about 10 s on the OVH node and 32–33 s on
`wyrm2` from the first Linux line to systemd. Guest readiness took 42–45 s and
116–120 s respectively. Shutdown request to completion took about 4 s and
13–25 s respectively. The consoles buffer output. Both hosts ran the nested
guests with KVM. The bare-metal Intel control plane runs the KubeVirt worker
directly. The AMD Atlas host runs `wyrm2` in Proxmox, adding a virtualization
layer before the KubeVirt worker. Five-minute node I/O pressure rates were
roughly 0.2% on the control plane and 2.6–4.9% on `wyrm2`, including other
workloads. These measurements do not isolate the effects of storage, CPU,
nesting or concurrent load.

## Guest shutdown

In the [failed pair-2 run](https://app.buildbuddy.io/invocation/e03dc565-cb69-42df-a7fc-cbc10be52e05),
`systest-vm-1` (VMI UID `1fc2083d-191f-4a95-a0af-02bf3dfbd226`) received the
shutdown request at 11:44:31 UTC. Its retained console and raw serial have
neither `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE` nor kernel powerdown. The
UID-correlated `virt-handler` log records grace-period expiry and a kill at
11:45:01.286 UTC, then `Shutoff reason Destroyed` at 11:45:02.141 UTC.
The sibling guest emitted the marker, powered down, and reached
`Shutoff reason Shutdown`. The artifact paths are under
`test.outputs/vm-artifacts/{consoles,serial,handlers}/`. What held up the first
guest remains unknown.

Seven manual `test_vm_kvm` invocations at the pinned commit produced ten
uncached TestRunner executions. The census joined their BuildBuddy executor
host IDs to Kubernetes node names in the workers' Loki console streams.
Nineteen of twenty nested guests emitted the marker in both console and raw
serial, logged kernel powerdown, and had a corresponding `Shutoff reason Shutdown`.

| Worker node     | Passing actions | Actions with a forced guest stop | Additional runs                                                                                               |
| --------------- | --------------: | -------------------------------: | ------------------------------------------------------------------------------------------------------------- |
| `wyrm2`         |               3 |                                1 | [Earlier fresh-fixture pass](https://app.buildbuddy.io/invocation/9f0998bf-19a9-48a5-9aa8-cd93cd6ee407)       |
| `ovh-ns104963`  |               4 |                                0 | Two passes in the [four-run burst](https://app.buildbuddy.io/invocation/3b655597-26e4-4f0a-b458-d97eabcf83d6) |
| `ovh-ns1001419` |               2 |                                0 | Two passes in that burst                                                                                      |

The manual benchmarks disabled BuildBuddy target tracking. From 09:00 to
13:20 UTC on 21 November, explicit enumeration of Ducktape test invocations
found the seven manual children above and 63 children with a required
`//agentplane/system_tests/suites:test_vm` result. All 63 required-suite
results were cache hits. The fresh-fixture benchmarks give no production
failure-rate estimate by node.

The [17 November mixed-burst trial](https://app.buildbuddy.io/invocation/90d926d2-0b20-42a1-a3cd-11a9ef25a04d)
had two earlier teardown failures on separate workers on `ovh-ns104952`.
Both scenarios passed in each failed action. Across runs 3 and 4, three guests
lacked the marker and kernel powerdown. The matching `virt-handler` records
show grace-period expiry followed by `Shutoff reason Destroyed` for each.
The trial used `test_vm_kvm_recycled` with cold fixtures and a different
source commit. It establishes that the failure predates the placement change.

## Next experiment · HDD work disk

1. Use the [one-member qualification canary](../../kvm/x/nixos_worker_qualification/README.md)
   as the isolation pattern. Give the test VM, PVC, runtime config, network
   policy and BuildBuddy pool distinct names. Keep it outside production KEDA
   labels and route only manual, explicitly targeted actions to it.
2. Compare an SSD and HDD work claim on the **same** nested-KVM host with both
   local pools. Start with `wyrm2` if fresh node, LINSTOR and I/O checks show
   headroom for the additional disk and worker. Keep the image digest, executor
   settings, 80 GiB guest disk size, filesystem volume mode and resources
   identical. Pin each worker to that host, then verify the VMI, bound PVC/PV,
   local LINSTOR backing pool and registered executor before submitting work.
   Use `piraeus-hdd-local` for the HDD side and allow for CDI's filesystem
   overhead in the capacity check.
3. Match source, test config, inputs and cache state. Record fresh-worker
   readiness and first pulls separately, then alternate serial SSD/HDD runs
   of the same uncached VM target with warmed worker caches and fresh
   fixtures. Repeat with the production retained fixture before considering
   a production change. Keep invocation links, worker IDs and placement,
   queue and action clocks, setup/guest boot/shutdown spans, teardown logs,
   and node/pool I/O and CPU pressure. Pause the trial if it disturbs
   production placement, readiness or storage headroom.
4. Drain and remove the canary after its actions finish. `piraeus-hdd-local`
   has `Retain` reclaim policy. Record the canary's PVC, PV and LINSTOR volume
   identities when they bind. After removing the claim, confirm that the
   retained PV and storage resource belong to the disposable trial and are
   unmounted before removing them. Verify that backing space is reclaimed
   and the canary VM, executor and temporary configuration are gone.
