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
