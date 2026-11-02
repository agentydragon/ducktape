# VM CPU budget comparison, 2026-11-02

The selected fixture uses two emulated CPUs per guest: both measured warm full-suite
observations passed, while neither four-CPU observation preserved the full lifecycle.
The observed execution durations support this bounded choice, without establishing
a precise causal speedup. Staging budgets require their own measurements.

The fixture's CPU budget was compared at one, two and four emulated vCPUs per
guest, with matching service, aggregate, process and executor quotas of 100,000,
200,000 and 400,000 microseconds per 100,000-microsecond period. The full suite
runs three independent guests concurrently: Codex turn, Claude turn with private
supervision checks, and Codex tool OOM containment/recovery.

All arms reserved eight BuildBuddy compute units. Guest memory stayed at 1.5 GiB,
and the OOM probe retained its 96 MiB leaf. The observed k3s Node advertised
11 CPUs and 20,012,876 KiB RAM; those are capacity observations, not the eight-unit
reservation. QEMU used TCG, and launcher arguments independently confirmed each
arm's SMP topology. The imported guest boot/root and application image content
IDs matched across arms.

## Warm baseline and limitations

The effective source was `f1e187cbb483e534db543bcadc1141f4fa55e6bd` plus the CPU
configuration/probe correction and frozen heartbeat diagnostic helpers with their
refresh branch disabled. Runs restored one immutable infrastructure snapshot,
`73c5f56c-8e38-4156-8826-e01660f94a8d`, produced by
[de343b58](https://app.buildbuddy.io/invocation/de343b58-0134-4893-8b69-bfd78b6ab07e).
Every warm receipt matched the same live k3s process, boot, Node UID and baseline
fingerprint. Application namespaces, Sandboxes and guest disks remained fresh.
Temporary snapshot selectors and helper copies are experiment controls, not the
selected fixture change.

The one-vCPU observations are shared heartbeat controls with refresh disabled.
Their original `CPUQuotaPerSecUSec=1s` assertion is semantically equivalent to the
corrected configured-quota assertion at one CPU. Exact kernel `cpu.max` checks
remain in every arm. The original two/four-CPU seeds failed the hardcoded 1s
expectation; they are not evidence against the requested CPU configuration.

First two/four-CPU observations overlapped on distinct workers. Repeats were
sequential. Worker performance and initial KubeVirt heartbeat/schedulability differ:
the first CPU arms initially restored a stale heartbeat and naturally refreshed,
whereas the first shared one-CPU control had refreshed before its initial dump.
Thus these small samples do not establish a precise CPU-caused speedup or a PR
feedback saving. Case and named scenario spans overlap and must not be summed.

## Results

| Arm                  | Invocation                                                                            | Execution seconds | Worker seconds | Full suite                   |
| -------------------- | ------------------------------------------------------------------------------------- | ----------------: | -------------: | ---------------------------- |
| 1 CPU shared control | [7149f44c](https://app.buildbuddy.io/invocation/7149f44c-b924-4e87-a9bf-581588ef2424) |           599.118 |        604.480 | Pass                         |
| 1 CPU shared repeat  | [9ede858f](https://app.buildbuddy.io/invocation/9ede858f-ec49-490c-ab53-f95d94e5e3eb) |           429.755 |        430.740 | Pass                         |
| 2 CPU first          | [35dd8198](https://app.buildbuddy.io/invocation/35dd8198-40e1-40e0-8e5a-e190ac1e1e9f) |           375.258 |        379.320 | Pass                         |
| 4 CPU first          | [d7d31806](https://app.buildbuddy.io/invocation/d7d31806-5545-4eb7-908f-64f5a635ef93) |           460.748 |        462.229 | Guest kernel stall           |
| 4 CPU repeat         | [b1090ce5](https://app.buildbuddy.io/invocation/b1090ce5-c5be-4b17-a2aa-9ce3c10ba059) |           299.232 |        300.600 | QEMU abort, missing shutdown |
| 2 CPU repeat         | [151209f7](https://app.buildbuddy.io/invocation/151209f7-e9b8-4602-b870-4b664e4b112e) |           299.433 |        300.347 | Pass                         |

The first two-CPU pass used 1,653.572 measured CPU-seconds; eight times its
execution duration is 3,002.062 requested-unit seconds, a reservation proxy rather
than CPU consumption or price. Setup/case/cleanup were 62.472/215.184/36.070 seconds.
Both two-CPU passes verified all three journals and all three guests emitted shutdown receipts. The OOM
probe exited 137 then recovered with exit 0 in the same cgroup/runtime, and cleanup
released three workspace loop devices. The sequential repeat used 1,318.040
CPU-seconds with setup/case/cleanup of 52.133/179.611/28.721 seconds.

Four CPUs did not preserve the tested lifecycle in either warm observation. In the
first, the Claude guest's Linux 6.12.63 kernel reported an RCU timer wakeup missing
on CPU 3 from 24.518 seconds after boot and never started Environment Runtime.
The suite reached its existing readiness deadline; this is a wedge, not evidence
for raising that deadline. In the sequential repeat QEMU aborted in
`iotlb_to_section` (`system/physmem.c:758`, assertion
`section_index < d->map.sections_nb`) during the original Claude guest boot.
KubeVirt replaced that guest and scenario assertions passed, but the suite correctly
failed on the original incarnation's missing shutdown receipt. Three successful
journals/receipts from four incarnations do not make that run valid.
The underlying QEMU/timer defect is unresolved; the evidence does not isolate
four CPUs as the cause of either failure.

Earlier family-key trials are excluded from this warm comparison. Their receipts
were cold, including a restored Firecracker VM whose clean, matching marker
referred to dead k3s PID 562 on the same boot. Process-exit cause is unknown.
One earlier CPU1 physical attempt also hit the separately tracked provider Linux
5.15 scheduler panic before an automatic retry passed cold. Failed/retry execution
cost remains part of the experiment inventory; it is not removed from totals.
