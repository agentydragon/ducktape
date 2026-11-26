# Keep silent guest turns separate from host crashes

[Issue #16031](https://github.com/agentydragon/ducktape/issues/16031) tracks the
unexplained admitted-turn stall. No timeout, runner-lifecycle or guest-setting
change is supported by the newly inspected evidence.

## Historical admitted-turn boundary

The [PR #15493 attempt](https://app.buildbuddy.io/invocation/5d2cc998-8c9b-5d7f-8b67-9509593f00ae)
at `a065ad40c83321f6a15612c48ca221422695ce10` waited for the Codex turn from
22:19:10.042 to 22:24:10.074 UTC. Raw serial's last Runtime reconciliation
was 22:19:11.495, the app-server launch span ended at 22:19:11.506, and both
CRI console and independent QEMU serial stopped near then at guest uptime 86 s.
The retained persistent journal ends at 22:19:01, before that serial record.
The 22:25:03 host sample showed QEMU `running` and both vCPUs halted. The
300-second turn and 600-second outer deadlines expired. The late snapshot
leaves kernel/timer, userspace and I/O causes open.

[PR #15993](https://github.com/agentydragon/ducktape/pull/15993), merged at
`2faec465369c1033303dac05740c840229248c45`, samples the first outstanding turn at
210 and 240 seconds with bounded QMP/host diagnostics. It keys on Sandbox name
and UID, and retains the launcher Pod UID in the separate `turn-0` and `turn-1`
artifacts. Shorter turns issue no new probes. A recurrence needs those samples
alongside serial, launcher and Runtime history. QMP may wake QEMU. If both
samples leave guest scheduling or userspace progress ambiguous, a separate
bounded experiment can collect guest SysRq task stacks before the turn deadline.

## Independent failures on nearby attempts

- **Outer provider fault.** On the same source, [another attempt](https://app.buildbuddy.io/invocation/d1131cb0-8e38-509f-8b02-eaca85afba23)
  reported a managed-kernel general protection fault in `update_blocked_averages`,
  a refused Firecracker pause and a later TSC `EINVAL` restore failure. Retain
  provider snapshot creation and restore metadata alongside the host kernel
  trace to attribute the restore error. See [the snapshot TSC metadata gap](https://github.com/agentydragon/ducktape/pull/15849).
- **Guest systemd proof.** The [unit proof attempt](https://app.buildbuddy.io/invocation/8dd3217e-38a2-5b1c-a46f-fa0b55149e12)
  failed in `reap_retries_property_deadline_on_connected_manager`. At the tested
  head, line 613 read systemd properties in `assertSystemdProofEmpty` with the
  same 10-second context used for PID observation, stdin and `Wait`. The guest
  journal records exit 23 about one second after launch. [#15571](https://github.com/agentydragon/ducktape/pull/15571)
  added a guard and moved the line. [#15957](https://github.com/agentydragon/ducktape/pull/15957)
  gave the final unit and empty-domain proof a fresh 10 seconds inside the
  70-second fixture context, retaining retry, identity, exit and stream checks.
  The old artifact leaves budget exhaustion and a slow manager read possible.
  On recurrence, record read start, context time remaining and read duration
  with the native unit identity and guest journal.
- **Outer suite budget.** A cold action spent 323.5 seconds in setup and about
  304 seconds in scenarios and cleanup. The [#15481 attempt](https://app.buildbuddy.io/invocation/4b599700-757c-5e24-9e56-9fbcd554cd07)
  spent 428 seconds in setup and 230 seconds in the case interval. The exact
  scenario operation blocked before the Kubernetes API cleanup deadlines
  remains unknown. Compare uncached spans on matching image inputs and retain
  the active span and guest/host state at expiry. See [the historical CI report](https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/2026_11_12_vm_ci_reliability.md).

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
[earlier QEMU abort](vm_cpu_budgets.md). A [later IOTLB trace](vm_shutdown_stalls/mixed_kvm_teardown_2026_11_17.md#pr-ci-qemu-boot-assertion-reported-by-the-teardown-gate)
captures a map mismatch and leaves the exact provenance and repair open.

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
finalization completes. The [Sandbox controller](../../environments/internal/controller/controller.go)
owns finalization; [VM reconciliation](../../environments/internal/controller/vm.go) halts the
KubeVirt VM by setting `RunStrategyHalted`. KubeVirt/libvirt/QEMU deliver guest
shutdown. [Runtime Close](../../env/runtime/internal/coordinator/close.go) owns stopping Runtime work,
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
