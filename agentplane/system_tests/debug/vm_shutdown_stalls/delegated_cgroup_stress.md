# Delegated cgroup path stress

The opt-in [`delegated_cgroup_stress` guest probe](../../vm/guest/delegated_cgroup_stress)
creates an `io` controller transition inside a systemd-delegated disposable service subtree.
It is a reduced kernel-path diagnostic; it does not reproduce PID 1 changing its own/root
cgroup or prove that cgroup updates caused the original shutdown stall.

## First one-guest smoke: fixture did not exercise the path

The uncached one-guest run [34cdf126-7bac-489c-a044-31c5b4faf8e1](https://app.buildbuddy.io/invocation/34cdf126-7bac-489c-a044-31c5b4faf8e1)
used source head `324646d2bfd5941cdfa2d73b299f1600bc41cd65` on base
`32f7b63bf0604968e887969a48a45386e7cb4bb9`. The guest reached `multi-user.target`, then the
probe emitted `UNEXERCISED reason=delegate_subgroup_did_not_contain_only_writer`. Its cleanup
marker reported owned children removed, resident stopped, trace restored, and the service
controller restored. The Python test raised `RuntimeError: delegated cgroup stress did not
prove the requested path`; it sent no ACPI request, so this was a fixture failure before any
shutdown measurement.

The failing check counted `cgroup.procs` with an external `wc` while the probe itself was in
the delegated subgroup. That child process can also appear in the file being counted. A
[shell-builtin membership check](https://github.com/agentydragon/ducktape/commit/7637594cb711de46745ca9fdeb6656f8eb26ecef)
was prepared and tested separately, but is not included in merged PR #14811. The second
smoke failed at the same check, so the external child alone does not explain the observation.

## Second one-guest smoke: still unexercised

The [follow-up invocation](https://app.buildbuddy.io/invocation/2264ac32-7bc3-498d-899f-c13664e2f6f3)
checked out base `32f7b63bf0604968e887969a48a45386e7cb4bb9` and applied a patch from the
corrected source branch. Its uncached remote execution selected
`test_ci_guest_shutdown_via_standalone_kubevirt_vmi`, with one cycle, the boot RCU observer,
and the delegated probe enabled. It failed after the guest emitted the same
`UNEXERCISED reason=delegate_subgroup_did_not_contain_only_writer` marker. The cleanup marker
again reported owned children removed, resident stopped, trace restored, and service controller
restored. The observer recorded 62 samples and exited cleanly during teardown.

There is no marker proving the requested function path or any of the 20 transition pairs; the
membership check runs before both. The artifacts do not record the actual subgroup membership,
so they do not establish why the corrected check failed. The shutdown diagnostic contains no
ACPI request or ordered shutdown proof: this is another probe setup failure, not a reproduced
shutdown stall. No conclusion about the original stall's cause or frequency follows from it.

## Membership investigation

An uncached [one-guest smoke](https://app.buildbuddy.io/invocation/7aa2d165-8e35-464c-8d28-778b9533514b)
at `5823f06ebfab8bd3e9427bfc1ab7e6d6e5e5fcc1` used a shell-only membership read and recorded
three subgroup members: writer PID 532 and BusyBox PIDs 537 and 541. It exited unexercised
before the controller transitions or requested ACPI shutdown; cleanup reported success. Its
VMI UID was `52df93a4-ca67-414c-9a59-98aa7335da16`. The retained console identifies the
process names but does not establish their command lines or ancestry.

Separately, the same guest BusyBox binary was run locally as
`busybox timeout -s TERM 10 busybox true`. After the command returned, a BusyBox `timeout`
watchdog remained alive, reparented to PID 1. The probe calls BusyBox `timeout` twice with
three-second deadlines just before checking membership. A second [guest smoke](https://app.buildbuddy.io/invocation/9ab207e2-502a-4b5b-9c53-1e9560b2ae43)
at `8ff0ca988bde194a32ec4adf556f29a555386d64` confirmed the extra processes: PIDs 558 and
562, both reparented to PID 1, were the BusyBox `timeout` watchdogs for `systemctl show`
`ControlGroup` and `DelegateSubgroup`, respectively. Its VMI UID was
`8911e4fc-fd84-464f-b0b2-429f77016883`. Like the first smoke, it stopped before transitions
and the ACPI request and reported successful cleanup.

The current probe reads membership without forking and waits up to five seconds
for transient processes to leave; it still refuses to proceed unless the writer is the sole
member. It records bounded PID, process-name, ancestry and command-line evidence if that
condition is not met. A successful setup does not by itself establish the original shutdown
stall's cause.

The [next smoke](https://app.buildbuddy.io/invocation/0f241f5f-529e-4a60-9536-bda0717e287d)
at `500f9bbac350f86ff0dc49f9f84b2004fe39179a` waited once, verified writer PID 536 was the
sole subgroup member, enabled the service's `io` propagation, and established the empty
manager, writer and resident fixture. It then stopped at `trace_buffer_size_unavailable`
before the trace witness, stress pairs, or requested ACPI shutdown. Cleanup reported success.
Its VMI UID was `70f207f3-4a96-4510-90f4-e415160be48b`.

That run did not record the actual buffer-size string. The Linux 6.12
[tracefs implementation](https://github.com/torvalds/linux/blob/v6.12/kernel/trace/trace.c)
can report an unexpanded default as `<size> (expanded: <size>)` or `X` for differing
per-CPU sizes; treating all non-numeric output as an unavailable trace facility is therefore
incorrect. The follow-up uses a disposable tracefs instance for the trace buffer and removes
it during cleanup. The function-graph filters remain global kernel controls; the probe checks
they are empty before use and resets them during cleanup.

The [first private-instance smoke](https://app.buildbuddy.io/invocation/f65257e4-fb14-4919-9d97-56bdd519fb57)
at `706bc24fcd129c36bf45b25cede1225a12730ad1` reached the fixture and stopped at
`preexisting_or_unsupported_filter_set_ftrace_notrace`. The guest's actual filter content was
not captured, but [Linux 6.12](https://github.com/torvalds/linux/blob/v6.12/kernel/trace/ftrace.c)
represents an empty exclusion list as `#### no functions disabled ####`, which the probe did
not accept. Its VMI UID was `423775af-6f1b-4232-9342-1229971e7958`; cleanup reported success.
The follow-up accepts that exact sentinel but continues to reject nonempty filter lists.

The [next smoke](https://app.buildbuddy.io/invocation/3b82e516-d013-4c77-aa79-2dee2068edde)
at `8c4e6163b5d8d9f8cc501e0d5b00eb789b2c712e` reached the private trace and the first
controller write, then stopped with `trace_overrun_or_incomplete_stats`. The console did not
retain the before/after counters, so this does not distinguish lost events from a stats-check
problem. Its VMI UID was `e6490a39-d99a-4d1e-b8b5-d806e4b6a076`. Neither the collected
serial nor the sampled console contained a probe cleanup-complete marker; the retained host
summary does not prove why, and the test did not request measured ACPI shutdown. The next
diagnostic records per-CPU before/after counters and trace size on failure and adds cleanup
progress markers.
