# Actions service grants: VM CI boot failure

PR #16467 at b0a5719548 failed only `//agentplane/system_tests/suites:test_vm` in
[invocation e8fc2499-aeae-572a-b01b-07ac6371e588](https://app.buildbuddy.io/invocation/e8fc2499-aeae-572a-b01b-07ac6371e588).
The test action took 665.362 seconds. No unchanged rerun was submitted.

The failure is `SandboxNotReadyError: systest-vm-2 did not reach Ready within
300.0s`, then the scenario's 600-second timeout during cleanup. The VMI and launcher
Pod are running and Ready; the Runtime is `NeverConnected` and has no logs.
The saved readiness diagnostic embeds 100 Gateway and 100 Sandbox Controller log
lines. They show connection attempts to the Runtime port 9090 failing with
`no route to host`, with no certificate, TLS, permission or unauthenticated error.
Actions container logs are not present among the saved artifacts. The first VM
completed six scripted upstream exchanges.

The failing VM's `vm-artifacts/serial/...systest-vm-2.../virt-serial0-log.log`
contains repeated timer/RCU stalls: CPU 0 takes no ticks, the RCU kthread's timer
wakeup does not happen, and PID 1 is blocked in `i8042_probe` through
`kernel_init_freeable`. Its userspace and Runtime have not started.
The saved host QEMU command uses `-accel tcg -smp 2` with Debian
6.12.115+deb13.1-cloud-amd64; its command line has no boot observer option.

This establishes an early guest-kernel boot wedge, before any receiver auth
path could run. It does not establish the cause of the lost guest timer ticks,
or a safe code fix. The PR adds service allowlist entries without selecting a
new outgoing Actions transport. Devel runs at df69d1c9d and 7624a97e3 were green
according to the coordinator; that does not exclude an intermittent guest boot
failure. Do not alter auth grants to hide this boot failure or increase budgets.

## APIC state and comparison

The report-time host snapshot at 00:56:43 UTC identifies the matching launcher
Pod UID `895c7dbc-c68b-4c01-87ac-6dbd0339bfdd`. Both virtual CPUs are halted in
`pv_native_safe_halt`, with interrupts enabled. CPU 0's LAPIC has vector 236
(local timer) in ISR, vectors 52 and 236 in IRR, and PPR `0xe0`; its periodic
timer is unmasked. CPU 1 has neither an in-service nor pending interrupt.
The CPU 0 in-service priority blocks the next timer and device interrupt.
This snapshot does not show how that in-service timer failed to complete;
it does not distinguish a missing EOI from an emulator delivery defect.
The kernel's repeated NMI backtraces put CPU 0 in the ordinary idle task,
while CPU 1 continues reporting the stall. This is stronger evidence than
an absence of Runtime log lines, but still does not identify a fix.

The failed devel execution at `3cab27e` (invocation
`b144fcec-660d-565b-9b6a-8ca97c905344`) was also inspected. Both its retained
guest serial logs reach systemd and Environment Runtime startup, without
RCU stall reports. Its readiness failure is not evidence of the same
pre-userspace failure. Earlier TCG timer stalls are catalogued in
[the VM investigation](../system_tests/debug/vm_shutdown_stalls/README.md),
but a shared underlying cause has not been demonstrated.

The upstream QEMU v10.1.0 interrupt/APIC sources and later changes were
checked for a matching repair. No matching repair was established. Do not
change the guest CPU count, remove the input device, switch execution lanes,
or change auth admission on the strength of this single snapshot.

## Instrumented execution

The single opt-in diagnostic execution on the same PR head plus observer code was
[056388a3-0a6c-4d48-8a75-729c1c0c180e](https://app.buildbuddy.io/invocation/056388a3-0a6c-4d48-8a75-729c1c0c180e).
`test_vm` passed in 284.7 seconds and the observer unit tests passed in 15.7 seconds.
The suite assertions, guest image, CPU count, authentication and time limits were
unchanged. This execution did not reproduce the boot wedge and is not a repair.

Both exact launcher Pod UIDs were discovered, but the deployed QEMU rejected
`trace-event-get-state` for `apic_register_write` as an unknown event. The observer
refused to enable tracing or redirect the logger; both retained traces are empty.
Thus this execution provides no APIC delivery/EOI trace evidence. No repeated
execution was submitted. Obtaining such evidence requires a QEMU build exposing
the relevant events, or another explicitly designed observation mechanism.

## November 15 recorded-history comparison (no execution submitted)

A fresh GetTargetHistory query (the `bbapi target history` backing RPC, using its
repo-wide pagination) retained only the `//agentplane/system_tests/suites:test_vm`
rows from six pages, back through November 13 at 01:52 UTC. The rows are an
index, include cached results, and must not be counted as independent test
executions. The PR failure is not in this index. Three indexed failures were
found and classified from existing artifacts:

| Source         | Invocation                             | Retained failure boundary                                                                                          |
| -------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `156710b7add6` | `44881c80-3129-5e3b-b846-720d05cdd3f0` | Both retained guests reached systemd and powered down; no timer-wakeup RCU stall. The test hit its scenario limit. |
| `3cab27eadddf` | `b144fcec-660d-565b-9b6a-8ca97c905344` | Both retained guests reached systemd/Runtime; no timer-wakeup RCU stall.                                           |
| `2faec465369c` | `fe172529-66fa-5a0f-bbe1-bab5f5372d6a` | GuestShutdownError for VM 1; a missing shutdown-complete proof, not this VM 2 pre-userspace timer stall.           |

These failures do not establish the same defect on devel. No claim that #16467
is exonerated follows from this comparison. GitHub still reports its head as
`b0a5719548b8f96d3f4591189953032fdaa0845c`; no push was made.

The original fault's immediate mechanism is priority blockage: CPU 0 retains
timer vector 236 in its local APIC ISR, pending vectors 52 and 236 cannot exceed
PPR 0xe0, and the CPU is halted with interrupts enabled. The guest timer-softirq
counter stays at 21. PID 1's completion wait in `i8042_probe` is downstream of
that absence of timer progress; removing PS/2 or changing vCPU counts would not
repair the established interrupt problem.

Upstream source inspection narrows the missing evidence. In QEMU v10.1.0,
`apic_get_interrupt` moves a selected vector from IRR to ISR before
`do_interrupt_x86_hardirq` delivers it, while `apic_eoi` clears the highest ISR
vector. A final snapshot does not say whether vector 236 was delivered to its
guest handler or whether its EOI was issued to the correct APIC. The prior
instrumented execution's unsupported trace event supplied neither fact.
Upstream atomic-interrupt-load, atomic-step CF_NOIRQ and vAPIC polling changes
were inspected; none has been shown to explain this captured ISR state. Do not
repin QEMU or apply one as a speculative repair.

The next decisive evidence is the CPU-attributed delivery/EOI sequence before
this fault, or an existing matching failure retaining equivalent state. No
additional test execution, timeout adjustment, skipped assertion or auth change
was submitted during this comparison.

## Authorized bounded reproduction

The coordinator authorized a separate diagnostics PR and ten executions each
of `test_vm` at `b0a5719548b8f96d3f4591189953032fdaa0845c` and fresh devel,
with identical boot interrupt instrumentation. This is an evidence-gathering
experiment, not a retry of #16467's CI verdict. The auth branch stays unchanged.
The collector discovers compiled trace events at runtime and always retains
repeated LAPIC/IOAPIC monitor snapshots, even when no candidate trace is supported.
Results and actual accepted event names will be recorded after the runs complete.

## Bounded reproduction results

The authorized experiment completed with no VM failures and no matching timer
stall in the 40 retained guest consoles:

| Source before identical diagnostics               | Invocation                                                                                                        | Results                           |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| #16467 `b0a5719548b8f96d3f4591189953032fdaa0845c` | [05413723-5146-4b10-8efd-b40c2e5aff3a](https://app.buildbuddy.io/invocation/05413723-5146-4b10-8efd-b40c2e5aff3a) | 10/10 passed; 245.9–391.8 seconds |
| devel `a8c4b186458f1522d783c74c5cd63ab6a35e36b8`  | [3ea624ce-ced3-4fc5-91c9-6e1cd53878c8](https://app.buildbuddy.io/invocation/3ea624ce-ced3-4fc5-91c9-6e1cd53878c8) | 10/10 passed; 235.0–380.0 seconds |

Both used `--runs_per_test=10 --nocache_test_results --flaky_test_attempts=1`.
The diagnostic source commits were `ccdd85dfeea9a104453beb328e621c034729f8a6`
for the auth head and `38574eada7d5b64dfa8eb71471f9c07b845e39d3` for devel.
Run/shard/attempt IDs were retained separately: ten distinct run IDs, each with
attempt 1. Two earlier launch attempts used abbreviated SHAs and failed during
checkout, before any test executed; they are not included in these counts.

The running QEMU exposed **no APIC trace events**. No candidate event was
accepted or enabled. `log int` was accepted for 16 of 20 guest boots in each
cohort, together with repeated per-CPU `info lapic` and `info pic` samples.
These are sampled state transitions and hardware-delivery/register logs, not
an event-by-event EOI trace. Logging generally reached its 16 MiB retention
limit before userspace; monitor sampling continued. All 40 early consoles
were retained independently.

The experiment also exposed a collector defect: six observers selected a host
QEMU PID that disappeared before `/proc/<pid>/cmdline` could be opened. They
aborted before starting the independent monitor timeline. Re-sampling after
the Running query and treating a missing logging process as a logging-only gap
preserves monitor collection. Two other devel observers exhausted their
existing startup discovery budget; those gaps are recorded, and the budget
has not been raised. Some discovery/probe requests also timed out and were
recorded individually.

There is no reproduced failure sequence to interpret and no established
kernel or QEMU repair. No further reproduction runs are authorized by this
experiment, and #16467 has received no push.
