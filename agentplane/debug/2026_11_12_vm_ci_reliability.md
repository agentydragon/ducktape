# VM CI failures with distinct evidence boundaries

The `test_vm` failures on [PR #15493](https://github.com/agentydragon/ducktape/pull/15493)
at head `a065ad40c83321f6a15612c48ca221422695ce10` do not share one
established cause. The failed guest proof in [BuildBuddy
`8dd3217e`](https://app.buildbuddy.io/invocation/8dd3217e-38a2-5b1c-a46f-fa0b55149e12)
is the one addressed by this candidate test change. Its retained `systest-vm-1`
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
