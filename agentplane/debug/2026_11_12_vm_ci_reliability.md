# VM CI failures with distinct evidence boundaries

The `test_vm` failures on [PR #15493](https://github.com/agentydragon/ducktape/pull/15493)
at head `a065ad40c83321f6a15612c48ca221422695ce10` do not share one
established cause. The failed guest proof in [BuildBuddy
`8dd3217e`](https://app.buildbuddy.io/invocation/8dd3217e-38a2-5b1c-a46f-fa0b55149e12)
is a unit-proof timeout. Its retained `systest-vm-1`
console reports `systemd_kernel_test.go:613: context deadline exceeded` during
`reap_retries_property_deadline_on_connected_manager`. At the _tested_ head,
line 613 called `assertSystemdProofEmpty` with the same 10-second context used
for PID observation, stdin and `Wait`; the helper failed reading systemd unit
properties. The guest journal records that the unit exited with status 23 about
one second after launch. The current source added a guard before that final
read in [#15571](https://github.com/agentydragon/ducktape/pull/15571); its
different line numbers must not be used to classify the older failure.

The candidate gives the final unit and empty-domain proof a fresh bounded
10-second context under the fixture's 70-second parent, while retaining the
retry, identity, exit 23 and stream checks. The failed artifact does not show
whether the original context was already spent or the final D-Bus read itself
was slow. A passing fresh VM run is candidate validation, not proof that the
historical failure mechanism is eliminated. On recurrence, retain the unit-read
start time, context remaining at entry and read duration alongside the guest
journal and native unit identity; these distinguish budget exhaustion from a
slow manager reply.

Another [#15493 failed attempt](https://app.buildbuddy.io/invocation/5d2cc998-8c9b-5d7f-8b67-9509593f00ae)
stopped producing both launcher console and direct serial output near guest
monotonic 86 seconds after Codex input. The case then hit its turn and outer
deadlines. A host QEMU report collected near test timeout says the domain was
`running`, but this does not establish guest progress. The retained evidence
does not separate guest kernel sleep, lost wakeup, QEMU scheduling and blocked
I/O. The turn wait ran from 22:19:10.042 to 22:24:10.074 UTC; the raw serial's
last Runtime reconciliation was at 22:19:11.495. The retained persistent
journal ends at 22:19:01, before the last serial line, so its absence of later
records cannot establish when guest progress stopped. The late host sample at
22:25:03 shows both vCPUs halted at that instant. A single QMP snapshot cannot
establish whether they were making progress beforehand.

The proposed suite collector marks the first VM turn by exact Sandbox name and
UID, then takes two bounded samples at turn age 210 and 240 seconds if it is
still pending. Each reuses the existing host-thread and QMP probes and records
the launcher Pod UID in separate `turn-0` and `turn-1` artifacts. Normal turns
under 210 seconds issue no new probes. The capture may itself wake QEMU; a
passing instrumented run is validation of collection, not a measured reduction
in stalls. On recurrence, compare the paired vCPU/register, APIC/IRQ and host
thread state with the retained raw serial and native journal. If both samples
still leave a guest scheduling or userspace ambiguity, capture guest SysRq
task stacks before the turn deadline in a separate bounded experiment.

The same head also had a BuildBuddy managed-kernel general protection fault
followed by snapshot-pause and restore failures. Those happened outside the
guest's test assertions; [#15849](https://github.com/agentydragon/ducktape/pull/15849)
tracks the missing snapshot TSC metadata needed to attribute the restore
`EINVAL`. Retain provider snapshot creation and restore metadata plus the
host kernel trace on the next incident; guest code cannot explain a failure
before guest boot. A separate cold invocation spent 323.5 seconds in stack
setup and roughly 304 seconds running scenarios and cleanup before the outer
limit. Its phase spans show budget exhaustion, not a specific failed guest
contract. Compare uncached setup and scenario spans on the same image inputs
before changing test placement or limits. [PR #15481's failed
attempt](https://app.buildbuddy.io/invocation/4b599700-757c-5e24-9e56-9fbcd554cd07)
similarly spent 428 seconds in setup before a 230-second case interval; the
cleanup's Kubernetes API deadlines are downstream symptoms. Its retained
artifacts do not locate the first blocked scenario operation, so the next
failure needs the exact active span and guest/host process state at the case
deadline.

## Boot interrupt stall, 2026-11-16

[PR #16945](https://github.com/agentydragon/ducktape/pull/16945), head `f89eba69`,
failed `//agentplane/system_tests/suites:test_vm` in 781.6 seconds in
[invocation `a5515ec2`](https://app.buildbuddy.io/invocation/a5515ec2-4bc4-5047-ba40-b3d2d5007648).
The App was Ready at 08:53:56 UTC. Guest `systest-vm-1` never connected to Runtime:
its 300-second readiness wait and 300-second cleanup wait exhausted the case
budget. The other guest completed its scenario and cleanup.

The failed guest's serial reports CPU 1 with **zero timer ticks** in the RCU
grace period, halted in `pv_native_safe_halt`, and a missing RCU timer wakeup.
The undeclared output `vm-artifacts/boot-interrupts/77525c20-c2f0-4dfe-b092-482b5c42a662/timeline.jsonl`
has 92 successful CPU 1 APIC samples from 08:54:50.428 to 08:59:37.713 UTC.
Every sample has local timer vector `0xec` in service; its pending interrupt
remains set, later joined by vectors 46, 51, 52 and 53. Processor priority stays
at `0xe0`. The late host report still shows that state, QEMU `running`, and the
vCPU halted with interrupts enabled. This is persistent loss of guest interrupt
progress, not evidence of a slow App deployment. The dumps do not identify
whether QEMU lost an EOI/state transition or the guest failed to issue one.

`bbapi target history` over seven days returned 837 status records, including
cached results; these are not 837 independent physical attempts. A confirmed
`devel` failure on November 9 at commit `0e7fc09b6a19b3755d1d5398a4d5537e6d287950`,
[invocation `c3a9fd9f`](https://app.buildbuddy.io/invocation/c3a9fd9f-e61f-5e11-8d20-4ff9ccf8d81f),
has the same CPU 1 zero-tick, idle-stack and missing-timer-wakeup signature.
The boot failure therefore predates #16945.

The captured five-second `context deadline exceeded` is a caught background
stats-sampler timeout: `k3s kubectl get --raw /api/v1/nodes/env/proxy/stats/summary`
against the test's own k3s at `127.0.0.1:6443`, proxying its kubelet. Sampling
resumes, and k3s continues serving controller, stats and QMP exec requests during
the guest stall. The warning is a transient symptom, not the fatal scenario
operation. It does not address the RBE cluster API or staging. Available memory
stays above 13.6 GiB; the failed guest's CPU 1 accumulated about 1.37 seconds of
host scheduler wait by 08:59. The action queued for about 60 ms and other suites
completed concurrently. These observations argue against sustained RBE resource
starvation, but cannot exclude a brief physical-host event without provider
telemetry. An earlier provider attempt failed with a vanished Firecracker
socket before this guest boot. Staging's overlapping Flux wedge has no observed
connection to this isolated CI cluster.

The next capture must retain delivery and EOI transitions during boot, with
vCPU/thread identity and explicit coverage limits. An EOI reaching the emulator
without clearing the corresponding in-service bit implicates emulator state;
a delivered interrupt with no guest acknowledgement instead calls for the guest
handler/return path, provided the capture covers its acknowledgement mechanism.
Missing probes, late activation, truncation or dropped events leave that choice
unproven. APIC snapshots alone cannot reconstruct the intervening EOIs, and a
passing instrumented suite validates collection rather than elimination of the
underlying stall.

## QEMU IOTLB assertion: distinct from the timer stall

[#16997's failed invocation](https://app.buildbuddy.io/invocation/0b4780e9-5f98-5c50-bf88-dcf69bbcb2e1)
at `a84addf4` aborted QEMU in `system/physmem.c:758`,
`iotlb_to_section`: `section_index < d->map.sections_nb`. This is not a recurrence
of the timer stall that motivated the APIC capture. The same assertion occurred
on devel at [`2faec465`, November 13](https://app.buildbuddy.io/invocation/fe172529-66fa-5a0f-bbe1-bab5f5372d6a)
and [`b15bd054`, November 10](https://app.buildbuddy.io/invocation/ed5acdf3-4dd2-524c-9d20-5e81631698c9).
All three retained launcher logs/configurations identify QEMU `10.1.0-10.el9`,
libvirt `11.9.0-1.el9`, host kernel `5.15`, explicit `-accel tcg` and two vCPUs.
`/dev/kvm` is absent in this intended emulation CI lane. These are the tests'
own k3s/Firecracker environments, with no observed connection to staging.

Jim Shu's upstream [854cd16e318e](https://github.com/qemu/qemu/commit/854cd16e318eed12de2995014b28d9f374c64bf7),
“accel/tcg: Fix iotlb_to_section() for different AddressSpace”, is absent from
this QEMU build. It was merged via `b377abc220fc` and is contained in v11, not
v10.1. Its documented trigger requires a CPU translation through an
`IOMMUMemoryRegion` changing `target_as`. The retained domain XML and command
lines show q35, direct kernel/initrd boot, no EFI/pflash and no vIOMMU. Default
SMM provides `cpu-memory`/`cpu-smm` AddressSpaces but does not establish that
IOMMU path. Thus the documented trigger does not match the retained config;
the underlying invalid index/dispatch mechanism remains undecidable. A newer
QEMU is a candidate experiment, not a demonstrated fix for these failures.

The pinned v1.8.2 launcher executable's GNU build-id is
`3bdb548b9f6ed0a112c7ef0b9afc123c4a8bdf86`; its SHA-256 is
`7921a53da58154d272c13b0d217314c92e5cde1bae05fe32e8607ae74549d0cb`.
CentOS Stream Koji retains matching core debuginfo plus supplementary DWZ data
(build-id `bd8286379100de6e362afeea18e8ea6cccd345af`). Bazel pins both RPMs and
verifies the identities, DWARF link and recipe instruction bytes against the
launcher image. Exact DWARF/disassembly exposes operands at the I/O fill,
IOMMU target change, four MMIO callers, assertion check/failure, map publication,
TCG commit and flush points. All 14 definitions register on the Linux 5.15
Firecracker executor; nested user-memory fetches work there. A bounded native
fixture also exposed that `traceoff` does not stop a private instance and its
snapshot trigger is rejected. The observer therefore reserves a separate
failure-only ring and records its userspace stop window.

The [IOTLB observer contract](../system_tests/vm/README.md#qemu-iotlb-assertion-capture)
defines the limits and interpretation. On recurrence, compare the failed
packed index/attributes and dispatch with candidate fills, IOMMU target changes,
and map-publication/flush order. Cross-AddressSpace translation would support
the upstream candidate; stale map publication without the needed flush would
support another invalidation fault. Overwrites, fetch faults, missing fills or
ambiguous provenance must leave the result undecidable. No guest, QEMU version,
CPU count or test deadline is changed by this diagnostic.

## Separate unexplained suspend cleanup failure

[Development invocation `5f545d80`](https://app.buildbuddy.io/invocation/5f545d80-51a7-4573-a0a3-b059e18eb9c8)
received HTTP 503 on the `systest-vm-1` cleanup request to
`/upstream/environments/v1/environments/systest/systest-vm-1:suspend`, followed
by an Identity-referenced teardown error. This occurred before APIC probes
attached. Its cause is not yet explained; subsequent passes do not explain it.
