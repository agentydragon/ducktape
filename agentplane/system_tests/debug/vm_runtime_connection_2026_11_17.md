# Pre-Runtime guest stall, cleanup fence and snapshot invalidation

The Codex VM in [manual invocation 7ba9c250](https://app.buildbuddy.io/invocation/7ba9c250-4568-467b-9ab6-8496e2e2a795) never reached userspace or Environment Runtime in the retained serial output. The first observed failure is a guest CPU 0 timer/RCU stall. Its subsequent cleanup reached the controller's deliberate quiescence fence, not a missing delete or cancellation race. The exact instruction or emulator mutation that first lost timer progress remains unobserved.

This is distinct from the [admitted-turn stall](vm_guest_progress_and_host_crashes_2026_11_12.md), the inner QEMU IOTLB assertion (#16057), and the outer Firecracker failures (#14952). The report's original seven-day population and interim baseline census retain their original windows; the samples below are selected forensic observations.

## Samples and clocks

All samples use `//agentplane/system_tests/suites:test_vm`, configuration and shard/run/attempt recorded in the adjacent evidence index. Each of the two completed samples has one fresh remote TestRunner; the bounded repetition below has two completed results and a separate provider retry. Build/lint actions sharing a target label are not additional TestRunners.

| Sample                    | Effective source                                                                                            | Baseline               | Native pytest setup / Claude / Codex / teardown (s) | JUnit suite / TestRunner (s) |
| ------------------------- | ----------------------------------------------------------------------------------------------------------- | ---------------------- | --------------------------------------------------- | ---------------------------- |
| Manual failure `7ba9c250` | Accounting working diff on `61a5f814adddc1955a359cbaeb62eaeab9390793`; not the final PR head                | Warm, `reused`         | 55.44 / 192.903 / 603.947 / 40.89                   | 704.008 / 730.501            |
| PR CI pass `3efe5700`     | Synthetic merge `705e62f414eaee959b3d011142bc31cbc77ec6a4`, head `3bbb9a307818dfc2171be8df2cb03db69fdfc3d9` | Cold, `missing_marker` | 210.13 / 176.016 / 161.332 / 33.71                  | 423.021 / 441.176            |

Callbacks overlap; do not sum their durations. TestRunner includes work outside pytest. Source, configuration, baseline and worker state differ, so this is not a matched performance comparison or same-source recovery. Stock pytest JUnit records the failed sample's two callbacks plus a zero-call-time duplicate Codex teardown-error row; this is not a third scenario.

[Accounting PR #17261](https://github.com/agentydragon/ducktape/pull/17261) merged as `db353034c0bc1e8d4a044f9e2c542ebf8a4caf36` at 09:10:46 UTC. Its three required checks passed. Five [focused accounting regressions](https://app.buildbuddy.io/invocation/b432a3f9-397b-41d5-8a01-1171fa5b34be) and the [scaled old-policy negative control](https://app.buildbuddy.io/invocation/d01b5f9c-efc7-401d-854f-a54303c31402) establish the logical deadline boundary. They do not repair guest timer delivery or prove a fleet recurrence reduction.

## Bounded current-source repetition

[Invocation 95e444be](https://app.buildbuddy.io/invocation/95e444be-2b63-40c0-a51f-4f89cfa582db) ran `bbr test //agentplane/system_tests/suites:test_vm --runs_per_test=2 --nocache_test_results --flaky_test_attempts=1 --remote_build_event_upload=all` at pinned source `d16ba5c2b1d7e93efe812d4c6a9d9a2143913aca` without source changes. Both fresh remote results pass, each with both harness cases and complete baseline cleanup. All four guest serials contain shutdown-complete receipts and no RCU-stall warning.

| Run     | Shared setup / Claude / Codex / shared teardown (s) | JUnit suite / TestRunner (s) |
| ------- | --------------------------------------------------- | ---------------------------- |
| 1, cold | 362.72 / 288.473 / 251.191 / 31.48                  | 687.555 / 716.162            |
| 2, cold | 214.23 / 213.821 / 196.651 / 31.51                  | 463.435 / 481.474            |

Run 1 exercises the intended accounting boundary: its suite exceeds the former 600-second item deadline while each callback stays below 289 seconds. It is not an old-policy full-suite control; that negative control is the scaled regression above. There are **three** distinct physical TestRunner IDs: two successful producers plus one provider UNAVAILABLE attempt whose outer Firecracker pause socket refused connections, followed by a native retry. Requested runs, final results and physical attempts are separate counts. Neither pass reproduces or repairs the guest stall or establishes its recurrence rate.

## Startup evidence

Sandbox UID `e046822b-1845-428b-8315-a562014ea222` maps to launcher Pod UID `27e359f0-5402-438d-b440-9414a6dd2d6c`, VMI UID `ce214419-274f-47f8-9ac0-eb319178d3aa`, QEMU PID 7158, CPU 0 TID 7270 and CPU 1 TID 7271. QEMU uses TCG, two vCPUs and `qemu-kvm-10.1.0-10.el9`; guest Linux is Debian `6.12.115-2`.

Independent raw serial and timestamped console continue through device initialization. At guest uptime 6.720469 seconds the serial reaches the i8042 controller initialization line. At uptime 26.573188 it reports CPU 0 RCU stalls, zero timer ticks in the grace period, unchanged softirq counters and a missed RCU timer wakeup. NMI backtraces repeatedly locate CPU 0 in `pv_native_safe_halt`; CPU 1 continues reporting stalls. There is no systemd or Runtime startup output. A missing guest journal alone would not establish this boundary, but the positive kernel stacks and interrupt state do.

The existing boot APIC observer attached before the first retained APIC register writes. Its monitor first samples CPU 0 with timer vector 236 in service at 08:28:04.976 UTC; later samples retain that vector and priority `0xe0`, with timer delivery pending. The interrupt event file reaches its 16 MiB cap at 08:30:04.202; monitor samples continue through 08:32:53.659. The APIC summary reports no ring overruns/drops, but attachment, output cap, probe scope and observer perturbation still limit conclusions. A monitor snapshot alone cannot prove a missing acknowledgement.

The exact downstream [QEMU debug-source RPM](https://kojihub.stream.centos.org/kojifiles/packages/qemu-kvm/10.1.0/10.el9/x86_64/qemu-kvm-debugsource-10.1.0-10.el9.x86_64.rpm) was inspected; its hashes are in the evidence index. In `hw/intc/apic.c`, `apic_get_interrupt` moves an accepted vector from IRR into ISR, `apic_irq_pending` blocks equal/lower priority pending vectors, and `apic_eoi` clears the highest ISR vector. Therefore an EOI after a higher-priority IPI can leave timer vector 236 in service. The trace must be replayed with that rule rather than treating every later EOI as acknowledgement of the timer. Upstream Linux v6.12.115 `sysvec_apic_timer_interrupt` acknowledges before invoking its timer callback; the Debian package's complete patches and the failed guest's instruction flow were not captured.

The reviewed CPU 0 event replay counts 641 accepted timer vectors and 640 EOIs which would clear that timer vector. The last accepted vector 236 is at host monotonic 682.536071 seconds, event-file line 4878. All subsequent accepted vectors are 251/253; their EOIs clear the higher vector while leaving 236 in service. The monitor agrees. Passing guests also show transient ISR236/IRR236 states, so a single monitor sample would not establish this failure. The guest identifies itself as bare hardware and rejects x2APIC, supporting the native APIC path rather than a KVM paravirtual acknowledgement. This narrows the failure to the timer delivery/acknowledgement boundary; it does not expose the failed interrupt entry or prove that the emulator, rather than the guest, caused the missing acknowledgement.

The remaining discriminator is the first divergence between accepted timer delivery, guest interrupt entry and acknowledgement, including the exact vCPU/APIC instance and state before/after EOI. The current artifacts do not capture that instruction flow or every APIC state mutation. No kernel/QEMU patch or timeout increase is justified from this boundary alone. Uprobes and QMP queries themselves perturb scheduling; a passing instrumented rerun is not a repair.

### Instrumented control on November 17

[Manual invocation `55f35d1f`](https://app.buildbuddy.io/invocation/55f35d1f-38cd-451b-940b-eab1688f505b) ran one fresh, uncached `//agentplane/system_tests/suites:test_vm` action with its unchanged two-case scenario and the new instruction-byte-verified APIC sites. Both original cases and their cleanup assertions passed (JUnit: Claude 186.361 seconds, Codex 167.368 seconds). The two launcher summaries and traces are retained as undeclared test outputs under `vm-artifacts/boot-interrupts/`.

Both launchers matched QEMU executable SHA-256 `7921a53da58154d272c13b0d217314c92e5cde1bae05fe32e8607ae74549d0cb`; all five exact sites fired, setup errors and missing sites were empty, and every host ring reported zero overruns and drops. CPU 0 retained its entire observed trace in both launchers. CPU 1's 2 MiB reserve overwrote 21,304 and 22,606 older lines, so an absence claim about early CPU 1 events would be invalid. The observer still starts after process discovery and perturbs scheduling.

One passing CPU 0 timer sequence at host monotonic 308.647252–308.647773 seconds records vector 236 accepted on APIC pointer `0x558114178140`, TCG hardware interrupt dispatch on vCPU index 0 and that same APIC, a return with guest EIP `0xffffffffa92015f0`, and ISR word 7 changing from `0x1000` to zero across EOI. This validates the collector's identity and before/after fields for a successful transition; it does not show the failed sequence again or prove that Linux's handler executed after QEMU returned. The first divergence in the failed guest remains unobserved.

The next discriminating observation is a failing guest with uninterrupted CPU 0 coverage: join the accepted vector 236 to the same vCPU/APIC's `guest_before` and `guest_after`, then inspect ISR before/after every EOI. If QEMU dispatch returns but timer EOI is absent, capture guest instruction execution at the IDT target and `sysvec_apic_timer_interrupt` entry/acknowledgement in that exact Debian kernel before assigning the fault to Linux or QEMU. A passing instrumented control and a missing EOI alone do not identify which side caused the loss.

## Cleanup follows the requested sequence

| UTC          | Observation                                                                                                                                                       |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 08:32:23.175 | Readiness fails after 300.118 seconds; `RuntimeConnected=False/NeverConnected`.                                                                                   |
| 08:32:23.539 | Suspend accepted.                                                                                                                                                 |
| 08:32:23.611 | Suspended observed.                                                                                                                                               |
| 08:32:23.625 | Exact Sandbox deletion accepted.                                                                                                                                  |
| 08:37:23     | Same UID retains deletion timestamp and `state-volume` finalizer, `stopStage=Quiescing`, no quiesce receipt and a retry for the same Pod UID. VM/Pod still alive. |
| 08:37:23.797 | Cleanup ends with its 300.275-second timeout.                                                                                                                     |

The [driver](../driver/app_sandbox.py) waits for disappearance of the exact UID; [owned cleanup](../driver/cleanup.py) drains despite caller cancellation and retains its failure. The [controller finalizer](../../environments/internal/controller/controller.go) seals published incarnations before deleting workloads/state. `TestDeletionSealsAnAcceptedIncarnationWithoutARuntimeSnapshot` expressly preserves this rule after controller takeover: absent observation cannot prove absence of previously admitted Sessions.

The Runtime remains unreachable, so it cannot produce that seal. The first Quiesce RPC result predates the captured controller log window; later connection failures, the final retry status and the still-live QEMU are retained. The successful comparator completes exact-UID deletion in 18.671 seconds. No missed deletion, cancellation race or duplicate mutation owner was established.

Baseline certification then correctly refuses the remaining Sandbox. Removing its finalizer, pretending quiescence or certifying the contaminated baseline would weaken safety. A different early-boot recovery contract would need durable incarnation-scoped proof that no work could have been admitted, or a recovered Runtime which seals retained Sessions. Neither was implemented here.

## Retained-baseline provider boundary

The new analysis of the existing #16027 census recovers native VM metadata for 11 of 14 `missing_marker` results. All 11 are first-task runners; ten recovered Actions match the source recycling-key family and one Action is unavailable. For nine of 11, the closest earlier recovered response with the same platform/configuration hashes has a different snapshot version. The other two are concurrent cases in a new version. Five observed platform/configuration/version groups contain both warm and cold results, so a version change is not a complete explanation.

One sequence supports deliberate safety invalidation:

1. [76be72fb](https://app.buildbuddy.io/invocation/76be72fb-c63f-5f20-a630-77e3c50ba474) ends at 04:25:38 UTC on November 17 with one Sandbox left. The stack exception guard calls the runner-discard marker writer; its retained log contains no marker-write error.
2. [5512f466](https://app.buildbuddy.io/invocation/5512f466-349d-5750-bedc-e4c31edc9ae2) started before that failure completed and reuses the old version.
3. [b273c841](https://app.buildbuddy.io/invocation/b273c841-8191-55bd-958d-b7e4f732569f) starts at 04:31:06 on the same worker with identical platform/configuration hashes and runtime fingerprint, but a new version and `missing_marker`. Relevant source/policy files are byte-identical.

Inspected BuildBuddy commit `a398894f6eb7a4ebbdfb7085843470f8c20f64c1` checks `.BUILDBUDDY_INVALIDATE_SNAPSHOT` and invalidates the version family. Its version-cache miss path also creates a new version. This sequence fits appropriate invalidation after unsafe cleanup; a concurrent invalidation or version-record loss remains possible because the provider mutation record is unavailable. The hosted executor build/settings are not attested by the inspected upstream source.

Live `GetExecution`, the exact retained `ExecuteResponse`, its server-log inventory and the full Bazel build log were checked. They expose VM lineage and save intent, but neither attempted manifest reads/admission nor version-mutation cause nor completed remote publication. The local escalation packet contains the exact 11 physical execution IDs and requests the provider's hosted revision/settings, version-record reads/mutations, candidate lookup/admission and prior producer publication results. No provider message was sent; #16027 remains open. Keep compatibility and cleanup guards unchanged.

The [reliability report](https://agentydragon.github.io/ducktape/reliability/#vm-forensics) carries reviewed counts and the selected validation separately from the original population. Raw logs, source RPMs and private executor/key metadata remain local; the adjacent JSON keeps reviewed provenance and artifact hashes.
