# Required VM suite on managed TCG and the existing KVM pool

An uncached, same-source selected-suite comparison on November 13, 2026 UTC
passed the current required VM test on both execution lanes. The KVM test action
finished 126.5 seconds earlier than the managed action, principally because its
two concurrent VM turns occupied 79.6 rather than 164.7 seconds. This is one
physical pair, not a measured required-CI saving or a placement decision. The
whole selected Bazel command still took 397.0 seconds because it waited for the
managed control; other required suites and GitHub gates were not selected.

## Exact workload and result

The test used source `c44a15e40c162fbb9d7760254020a6e78aeaca73` plus one
temporary, uncommitted sizing patch to the **managed control** target. That patch
added `test.EstimatedCPU=4`, `test.EstimatedMemory=7GiB` and
`test.EstimatedFreeDiskBytes=50GB` to `//agentplane/system_tests/suites:test_vm`.
The KVM target already requests those values. The remote runner fetched that
source and applied the patch cleanly. The patch was removed from the worktree
after the experiment; neither required CI nor worker configuration changed.

```bash
bbr test //agentplane/system_tests/suites:test_vm \
  //agentplane/system_tests/x/vm_kvm:test_vm_kvm \
  --nocache_test_results --keep_going
```

- [Inner Bazel invocation](https://app.buildbuddy.io/invocation/587be4c8-2b03-4323-966c-3fc1b1f26e53): complete, success, 396.977 seconds; [outer remote runner](https://app.buildbuddy.io/invocation/93128286-2b96-4963-9ab8-0ebf3dbb9321) completed normally.
- Both targets execute the same `test_vm.py` source with the same `vm` fixture, deployment manifest and pytest plugin. Their JUnit files each contain exactly one passing `agentplane.system_tests.suites.test_vm::test_vm_scenarios` case, with zero failures, errors or skips. Both traces contain the ordinary `vm-turn-harness_claude` and `vm-turn-harness_codex` scenarios. The Codex OOM probe is a separate manual target and was not selected.
- Both QEMU shutdown artifacts from the KVM action contain `-accel kvm`; both from the managed control contain `-accel tcg`. The selected test still exercises real KubeVirt guests, the guest kernel and systemd, VM process traces, and the two harness turns.
- The managed control's `persistence-suite.json` says `outcome=cold` and `cleanup=complete`, with fresh namespace, database Pod and root Secret UIDs and two workspace loop devices released. The KVM action opts out of baseline retention: its `k3s.stop` span completed in 5.92 seconds, its containerd teardown log reached `Stop CRI service`, two guest shutdown artifacts were uploaded, and pytest and the Bazel action passed after fixture teardown. There is no comparable persistence certification artifact for that lane.

| Returned physical TestRunner record | Managed TCG |                                    KVM pool |
| ----------------------------------- | ----------: | ------------------------------------------: |
| Bazel test duration                 |     346.4 s |                                     220.0 s |
| Worker execution span               |   346.448 s |                                   219.986 s |
| Pytest duration                     |   328.543 s |                                   212.698 s |
| Stack setup                         |    128.80 s |                                    119.40 s |
| Case envelope                       |    164.67 s |                                     79.64 s |
| Claude scenario                     |    163.80 s |                                     78.98 s |
| Codex scenario                      |    136.48 s |                                     78.15 s |
| Final retained-baseline cleanup     |     29.92 s | not applicable; nonretained K3s stop 5.92 s |
| Action input fetch                  |     1.039 s |                                     1.473 s |
| Worker output upload                |     0.470 s |                                     0.930 s |

The exact physical operation IDs are:

```text
Managed: /uploads/1b835ae2-9ffc-4f7e-8525-ab9b101005a9/blobs/3cecdb2553380732351f36b33a03ecfaddf29518a93791dac7f70d3e04f97d6f/1896
KVM:     /uploads/533d1bbf-aa1b-464f-8514-33f8d13744a7/blobs/11cce9288573fc97bbbdb1d953869a5e943068051294629fb3a57a3a74900663/596
```

## Placement, admission and cost boundary

The decoded action platforms have the same pinned OCI image and explicit
`EstimatedCPU=4`, `EstimatedMemory=7GiB` and `EstimatedFreeDiskBytes=50GB`.
Their other properties differ by design: managed execution uses Firecracker,
runner recycling and a snapshot read policy; `kvm-vm` uses self-hosted Docker,
no recycling and real KVM. The managed platform also carries
`EstimatedComputeUnits=8` and KVM carries the default `3`. In
[BuildBuddy's public sizing implementation at `4db6dd74`](https://github.com/buildbuddy-io/buildbuddy/blob/4db6dd74f9a43bab0d0bf3ef7aceb42a05708438/enterprise/server/tasksize/tasksize.go#L603-L625),
explicit CPU and memory replace the values derived from compute units in the
requested task size. This is source evidence for the request interpretation,
not proof that the hosted scheduler uses that exact revision. A live Mimir sample
at 01:41:40 UTC showed the selected KVM worker assigned 4000 milli-CPU and
7,516,192,768 bytes, matching its explicit request. The managed provider does
not expose its physical assignment to this investigation.

At that sample the KVM pool had three ready 4-vCPU/8-GiB worker VMs, each with
one action slot. Worker 0 had this action assigned; workers 1 and 2 showed zero
assigned CPU and memory and the exposed per-worker queue gauge was zero. The
pool's KEDA range is one to three VMs, with stabilized scale-up/down and boot
time; an idle slot in this sample does not establish admission at normal PR
load. The managed action's reported queue-to-start interval was 0.011 seconds.
The KVM record's queued timestamp was **1.034 seconds after** its worker-start
timestamp, so its queue interval is unknown, not negative or zero. Both actions
have provider queue records near 01:40:15 UTC; managed worker output finished at
01:46:03.344, KVM at 01:43:56.556.

The managed execution reported 1,098.955 CPU seconds and 5,787,660,288 peak
memory bytes. The KVM response has neither CPU nor peak-memory usage populated;
the prior [KVM sizing run](vm_kvm_existing_pool_2026_11_12.md) sampled a 5.51-GiB
action cgroup peak on the older three-scenario suite, not this run. Multiplying
the requested 4 CPUs by these execution spans gives 23.10 versus 14.67
requested CPU-minutes; those are capacity occupancy proxies, not measured CPU
consumption or billing. The three ready KVM VMs also carry standing reservations
while idle, so this pair cannot establish an overall compute-cost saving.

The managed baseline was explicitly cold. The KVM action started a fresh K3s
stack, but its persistent worker's local image cache may have been warm. Worker
CPU, storage, cache, isolation, and fixture retention differ, and managed pool
contention was not observable. This pair shows that current-source KVM coverage
can pass on existing capacity and that its VM scenario span was shorter. It
does not identify an independent KVM-only causal effect, a queue percentile, or
the last required CI gate. Before routing required CI, repeat matched runs
across cold and warm states under representative competing load, measure all
required paths and the resulting last gate, and account for the trusted-code
worker and its autoscaled standing capacity.
