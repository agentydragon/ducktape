# Hosted Firecracker guest scheduler panics

## Finding

The evidence strongly implicates **CVE-2021-47209**, a Linux fair-scheduler
use-after-free fixed in 5.15.5. The hosted executor still runs a matching,
vulnerable 5.15.0 build. Four retained physical VM executions panic on the same
scheduler pointer access; one is a fresh boot, so snapshot restore is not required.

This is the **BuildBuddy Firecracker guest kernel** surrounding our k3s test
environment. It is separate from the RBE container image and the Debian kernel
inside each QEMU/KubeVirt test guest. Updating our Dockerfile would not replace it.

The exact race that freed each object is not captured. Matching the kernel,
instructions, configuration and absent upstream fix establishes a strong causal
candidate, not a demonstrated repair. A provider crash dump or comparison on a
patched kernel is the remaining confirmation step.

## Physical failures and CI cost

The retained diagnostic census deduplicated 6,321 execution IDs from 92 previously
collected execution JSON files, including 1,789 TestRunner executions. It found
four native status-14 test failures, all VM suites with the same pause/socket
signature. This selected, overlapping historical sample is not a fleet failure rate.

| Physical execution start, UTC | Invocation                                                                            | Target under `//agentplane/system_tests/suites:` | Process time | Boot path  | Initial fault                         |
| ----------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------ | -----------: | ---------- | ------------------------------------- |
| 2026-11-02 01:22:00           | [8752f1b1](https://app.buildbuddy.io/invocation/8752f1b1-ad4d-412f-a7f2-dccbe85a0f71) | `test_vm_persistence_spike`                      |      702.9 s | Fresh boot | `update_load_avg+0x92/0x5c0`          |
| 2026-11-02 01:46:55           | [a690a9d1](https://app.buildbuddy.io/invocation/a690a9d1-34b8-4b9a-ada6-2689937e3f83) | `test_vm_profile_persistence_spike`              |      234.5 s | Restored   | `update_blocked_averages+0x273/0x740` |
| 2026-11-02 08:07:14           | [ecfd7e66](https://app.buildbuddy.io/invocation/ecfd7e66-680a-5dfe-b539-1eeb4f4076c7) | `test_vm`                                        |      107.5 s | Restored   | `update_load_avg+0x92/0x5c0`          |
| 2026-11-02 09:20:25           | [ecd7382e](https://app.buildbuddy.io/invocation/ecd7382e-9d22-5d7f-b68b-41b676789e4a) | `test_vm`                                        |       99.0 s | Restored   | `update_load_avg+0x92/0x5c0`          |

These four attempts ran on four executor hostnames. The last two restored the same
VM lineage, created at 06:27:40 UTC, at saved snapshot versions 9 and 16. Their
common ancestry is worth retaining, but the fresh-boot counterexample prevents
attributing the bug solely to snapshot restore or cross-host memory paging.

The newest failed process ended at 09:22:04.580 UTC; its replacement started at
09:22:05.250 and passed in 336.2 seconds. The logical test passed, while its Bazel
testing action contributed 437.9 seconds to a 438.6-second critical path. That
extra approximately 100 seconds was a failed physical execution and recovery,
not unexplained execution overhead or ordinary queueing.

Each panic precedes Firecracker exiting. The later unavailable `fc.sock` is a
consequence: BuildBuddy tries to pause the dead guest while copying workspace
outputs. The matching public callsite is the
[`copy_workspace_outputs` path](https://github.com/buildbuddy-io/buildbuddy/blob/acaf608b95780c2b0681b0205182c38bf5b6237d/enterprise/server/remote_execution/containers/firecracker/firecracker.go#L3307-L3355),
separate from post-completion snapshot recycling. VM metadata's saved-snapshot
flags do not prove a successful save after a failed action.

## Kernel identity and instruction match

Inspected provider source:
[`acaf608b95780c2b0681b0205182c38bf5b6237d`](https://github.com/buildbuddy-io/buildbuddy/tree/acaf608b95780c2b0681b0205182c38bf5b6237d).
Its [`deps.bzl` kernel pin](https://github.com/buildbuddy-io/buildbuddy/blob/acaf608b95780c2b0681b0205182c38bf5b6237d/deps.bzl#L288-L292)
provides the public x86-64 v5.15 ELF. Downloaded it and verified SHA-256
`c6dd56fff7dada624dcb68db317541f45504261ae6f2aae5dc91848aacbbfda3`.

A successful read-only hosted probe at 10:09 UTC used a separate diagnostic
recycling key, without loading k3s or seeding a production snapshot family. Its
`/sys/kernel/notes` GNU build ID matched the ELF:
`c69c6742fdf34d3f64cd616f1a133cb310f6895f`. Its `/proc/config.gz` matched the
configuration extracted from the ELF byte for byte, SHA-256
`a7b3dc0877b5f281c75fbd86a2e522da0caaf53a227397caff500bb0f33ce959`.
The probe establishes the current default hosted kernel identity; it does not
retroactively read the terminated VMs' build IDs. Their boot banner and faulting
code bytes match this binary, including the October 15, 2026 build timestamp.

Disassembling this ELF resolves all four initial faults to the same field:

- `update_load_avg+0x92` reads a group scheduler entity's `my_q` at offset `0x80`,
  then accesses the child runqueue's `propagate` at offset `0x108`.
- `update_blocked_averages+0x273` performs the same pointer dereference through
  its inlined skip/update path.
- The configuration enables fair-group scheduling, CFS bandwidth, SLUB and
  hardened freelists. In this layout, the entity's `my_q` overlaps SLUB's
  encoded free-object link. The faulting values are noncanonical pointers,
  consistent with reading that link after the entity was freed.
- `sched_offline_group()` in the actual binary calls
  `unregister_fair_sched_group()` before unlinking the group. This is the
  vulnerable ordering; the upstream repair is absent.

## Matching upstream defect

The upstream [scheduler fix `b027789e`](https://github.com/torvalds/linux/commit/b027789e5e50494c2325cc70c8642e7fd6059479)
describes a cgroup teardown race: a bandwidth timer can relink a dying group's
runqueues, which are then freed while still reachable by scheduler maintenance.
It specifically identifies `my_q` being overwritten by a hardened SLUB freelist
link as the reason the stale access causes a general-protection fault. The repair
unlinks the group and waits for RCU readers before unregistering/freeing it.

The Linux kernel CVE team's
[CVE-2021-47209 announcement](https://lists.openwall.net/linux-cve-announce/2024/04/10/31)
identifies the 5.15.y fix as
[`512e21c150c1`](https://git.kernel.org/stable/c/512e21c150c1c3ee298852660f3a796e267e62ec),
first released in **5.15.5**. That is the minimum release containing this repair,
not a recommendation to deploy that old point release today.

The evidence fits this known defect closely. A backtrace cannot prove which
operation relinked or freed the object; no vmcore or KASAN allocation/free trace
was exposed for these executions. Other memory-corruption causes remain possible.

## Provider action and confirmation

Request a maintained Firecracker guest kernel containing the scheduler repair
for this workload's executor cohort, then compare identical ordinary VM tests
through teardown across cold boots and restored snapshots. Preserve case coverage,
resource settings and the separate physical-attempt records. A few passes alone
would not establish a fleet recurrence-rate improvement.

Kernel selection is provider-owned in the inspected source. The existing newer
kernel path requires an internal executor flag and server-side
[`upgrade-fc-guest-kernel` experiment](https://github.com/buildbuddy-io/buildbuddy/blob/acaf608b95780c2b0681b0205182c38bf5b6237d/enterprise/server/remote_execution/containers/firecracker/firecracker.go#L4554-L4556).
No per-action kernel selector was found. The selected kernel digest participates
in the VM configuration hash, so changed kernels should select separate snapshots.
The experimental 6.18 path exists, but updating the vulnerable kernel within a
provider-supported rollout is the decision; an arbitrary container-image repin
does not implement it.

For definitive attribution, the provider needs either a crash dump identifying
the freed entity and stale runqueue-list membership, or an instrumented
reproduction that records the free and subsequent access. The existing four
failures, exact kernel identity and missing fix are enough to make that request
concrete. No provider settings or production test behavior were changed, and no
upstream message or issue was sent.

Raw execution responses, kernel tails, the successful identity probe, ELF,
extracted configuration and disassembly remain in task scratch. Investigation
notes here contain the reviewed conclusions and durable invocation/source links.

## 13 November architecture validation recurrence

Native invocation [`a90059e6`](https://app.buildbuddy.io/invocation/a90059e6-cddf-459f-9928-fbd2d3c65ed1)
validates the readonly guest-mount/shared-supervision candidate over source
`8a555b21141fded2166fcc17cd14096187e22842`. Its first physical `test_vm`
action starts at 18:52:45.089992 UTC and ends at 18:55:20.433288 UTC: 155.343s,
530.514 CPU seconds and 3,033,096,192 bytes peak action-cgroup memory.
It ends REAPI 14 / exit -2 because the Firecracker socket refuses the pause
request. The retained `vm_log_tail.txt` shows Firecracker 1.16.2 loading and
resuming a snapshot, then Linux `5.15.0 #1` faulting at
`update_blocked_averages+0x273/0x740`, panicking in interrupt context and rebooting.
The pause/socket failure follows the kernel death. Case exposure before that
failure is unknown; final target results must retain the automatic retry.

The same accepted invocation finishes successfully: its replacement VM process
runs 525.032s and passes the full scenario. All three selected targets pass in
722.499s Bazel elapsed. There are four NEW physical TestRunner records: two
healthy direct guest contracts, one failed managed VM execution and one passing
replacement. The 155.343s failure remains part of feedback and reliability cost.

The log digest is `a4c52f83ee7ae97157b19ba1bec2e18ff77f75e6eb7c8ad3721d0743a45fac50`,
24,191 bytes. The failed execution ID begins
`/uploads/51d06d77-90de-4676-b257-ea68ebb1656e/`; its cached response provides the
server-log digest. Track recurrence and recovery in #16222 and #16034 separately
from application failures, outer log-observer loss and cached successes.

Rechecking pinned provider source
[`f3508d33`](https://github.com/buildbuddy-io/buildbuddy/tree/f3508d33d1fbe0482d05ef9b0f0b9bbd7dac4962)
finds the same ownership boundary. Its Firecracker implementation selects the
6.18 kernel through an internal default or a task experiment; the scheduler adds
`upgrade-fc-guest-kernel` through its server-side feature decision. This is source
capability, not deployed acceptance or a client-selectable kernel property.
`recycle-runner=false` can force a cold action VM in that source, but remote
snapshot permission and `local-only` do not prevent local snapshot restores.
The earlier fresh-boot scheduler panic above already prevents treating restore
as a necessary trigger. Do not discard useful fixture reuse based on one
successful cold run.

The next useful comparison is provider-controlled: identical full VM contracts,
resources and snapshot policy on current and maintained-kernel cohorts. Verify
the kernel digest and cold/restored path for each physical attempt. Stop on an
unverified cohort identity or any same-signature panic in the repaired cohort;
retain enough matched attempts to assess recurrence rather than counting a few
passes as a fix. A kernel change should segregate snapshot configuration hashes,
but actual loads must be checked. No provider contact, setting change or kernel
rollout is performed here.
