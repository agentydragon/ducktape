# Keep silent guest turns separate from host crashes

[Issue #16031](https://github.com/agentydragon/ducktape/issues/16031) tracks the
unexplained admitted-turn stall. No timeout, runner-lifecycle or guest-setting
change is supported by the newly inspected evidence.

## Historical admitted-turn boundary

The [PR #15493 attempt](https://app.buildbuddy.io/invocation/5d2cc998-8c9b-5d7f-8b67-9509593f00ae)
admitted Codex input at 22:19:10 UTC. The app-server launch span ended at
22:19:11.506, and both CRI console and independent QEMU serial stopped near
22:19:11.5, guest uptime about 86 s. There was no retained harness completion or
failure during the 300-second turn wait. The later 600-second outer deadline
and cleanup errors do not identify the stalled component. Late QEMU `running`
and halted vCPUs do not distinguish a kernel/timer stall, userspace wedge or
lost telemetry.

[PR #15993](https://github.com/agentydragon/ducktape/pull/15993), merged at
`2faec465369c1033303dac05740c840229248c45`, samples the first outstanding turn at
210 and 240 seconds, keyed by Sandbox UID, with bounded QMP/host diagnostics.
A recurrence needs those live samples together with serial, launcher and Runtime
history; a post-cleanup process scan cannot reconstruct a former live guest.

The [other historical attempt](https://app.buildbuddy.io/invocation/d1131cb0-8e38-509f-8b02-eaca85afba23)
reported a kernel general protection fault in `update_blocked_averages`, a refused
Firecracker pause and a later TSC `EINVAL` restore failure. Those are distinct
boundaries, not an explanation of the first admitted-turn stall.

## Fresh natural QEMU crash

The [2026-11-13 02:09 UTC invocation](https://app.buildbuddy.io/invocation/fe172529-66fa-5a0f-bbe1-bab5f5372d6a)
on source `2faec465369c1033303dac05740c840229248c45` completed the VM scenario
assertions, then failed teardown because launcher
`systest_virt-launcher-systest-vm-1-9hr5s_b01f1912-8ad2-4faf-adb1-44b72189b96f`
had no shutdown-complete receipt. The exact incarnation's independent serial
and CRI console end at guest uptime 1.772752 s (02:11:53.071289117 UTC), during
PCI enumeration, before Runtime or a turn began.

Its retained `1539352-0.log.cri.log` launcher log supplies the cause of that
incarnation's exit: QEMU aborts in `system/physmem.c:758`, `iotlb_to_section`,
asserting `section_index < d->map.sections_nb`. The native QEMU message reports
02:11:53.222 UTC and `shutting down, reason=crashed`. KubeVirt replaced the
incarnation and the scenario assertions passed; the original crash still
correctly makes the lifecycle gate red. This matches the independently recorded
[earlier QEMU abort](vm_cpu_budgets.md), without proving its underlying cause.

The host report was taken at 02:14:22.917 UTC, after the crashed Pod disappeared:
it reports zero matching QEMU processes and Kubernetes Pod NotFound. That late
snapshot is not evidence that a live QEMU was idling or that a turn stalled.
Increasing a deadline or discarding the crashed incarnation's missing receipt
would conceal this distinct host failure.

## Bounded faithful repetition

[Two requested uncached VM runs](https://app.buildbuddy.io/invocation/ceafbc7e-36fa-4539-b686-5bcb2b9a9600)
on effective source `4415ac2544` both completed successfully. Their target summary
contains two remote runs, zero cached. JUnit contains the real `test_vm_scenarios`
case in each, at 296.068 and 301.197 s. Four retained guest consoles each contain
the shutdown-complete receipt. The twelve existing `system_test.turn.wait` spans
range from 2.634 to 33.212 s, so these passes do not exercise the late 210/240-second
diagnostic trigger or reproduce the original stall.

GetExecution contains **three** distinct TestRunner IDs, not two. The first
attempt of one requested run returned provider UNAVAILABLE: pausing the outer
Firecracker VM failed because its `fc.sock` refused connections. Worker clocks
show that attempt ran for 138.841 s, but its advertised result supplies no completed
VM-case/guest-receipt evidence. The provider/native retry produced a different
execution ID for the same action digest. The other two physical IDs completed
with OK status. This is three physical executor attempts, two completed passing
suite results, and one provider failure; it is not a measured silent-turn rate.

## Ownership and next discriminator

The [scenario cleanup owner](../driver/sandbox.py) requests suspension, waits for
the controller's effective suspended mode, requests deletion, then verifies the
exact Sandbox UID disappeared. It keeps Kubernetes/controllers alive until
finalization completes. The [Sandbox controller](../../sandbox_controller/controller.go)
owns finalization; [VM reconciliation](../../sandbox_controller/vm.go) halts the
KubeVirt VM by setting `RunStrategyHalted`. KubeVirt/libvirt/QEMU deliver guest
shutdown. [Runtime Close](../../env/runtime/runtime.go) owns stopping Runtime work,
fences and joins; cancellation stops a caller's wait while owned cleanup continues.
The [guest receipt collector](../vm/guest_logs.py) observes completion for every
launcher incarnation; it does not own shutdown or repair crashes.

No inspected artifact establishes a race between these owners in the silent-turn
incident, so no ownership layer was removed on speculation. The lifecycle
simplification follow-up remains open: first observe an actual duplicate-owner
or cancellation race, then narrow one owner and race-test active work, retained
history and the next input.

At the next admitted-turn recurrence, correlate the first missing Runtime/native
history progress with the same Sandbox UID's 210/240-second samples and serial
clock before cleanup. A QEMU abort belongs to the host-crash branch above; a
running guest without userspace progress requires its live registers/timers and
thread state. Review seven days of non-cached physical exposure separately from
cached target verdicts. No scheduled monitor, runtime repair or reduction in
recurrence has been established.
