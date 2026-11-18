# Pre-Runtime guest stall, cleanup fence and snapshot invalidation

The Codex VM in [manual invocation 7ba9c250](https://app.buildbuddy.io/invocation/7ba9c250-4568-467b-9ab6-8496e2e2a795) never reached userspace or Environment Runtime in the retained serial output. A later natural [devel failure `94f0de0c`](https://app.buildbuddy.io/invocation/94f0de0c-a4d1-59b9-a465-2d9790f4f2aa) records QEMU returning from timer interrupt dispatch with the expected guest entry PC, followed by a timer left in service while later IPIs are acknowledged. The exact instruction or emulator mutation that first lost timer progress remains unobserved. Cleanup reaches the controller's deliberate quiescence fence; weakening that fence would not repair timer delivery.

This is distinct from the [admitted-turn stall](../vm_guest_progress_and_host_crashes_2026_11_12.md), the inner QEMU IOTLB assertion (#16057), and the outer Firecracker failures (#14952). The report's original seven-day population and interim baseline census retain their original windows; the samples below are selected forensic observations.

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

The next discriminating observation was a failing guest with CPU 0 transition coverage. The natural recurrence below supplies the accepted/dispatch/EOI sequence, with an additional retention limitation discovered during its review.

### Natural devel recurrence with exact probes

[Invocation `94f0de0c`](https://app.buildbuddy.io/invocation/94f0de0c-a4d1-59b9-a465-2d9790f4f2aa), source `fd882ca51d21aac9b51c2bc48e7f5c966cbd55b4`, contains one fresh remote TestRunner for this target (run/shard/attempt 1). Claude passes; Codex never connects and its cleanup times out. Native pytest reports 53.83 seconds shared setup, 228.397 seconds Claude call, 602.913 seconds Codex call and 49.35 seconds shared teardown; the calls overlap. JUnit totals 710.927 seconds and native execution takes 751.085 seconds. The zero-call duplicate Codex row is a teardown error, not another scenario. Baseline cleanup also reports a remaining fixture loop device; it is not evidence about the first guest timer divergence.

The failed launcher Pod UID is `5471d325-6d01-497d-aaca-09b25766bbe6`, VMI UID `ff79259d-0304-4df9-a215-653257d056c5`, QEMU PID 6720, CPU 0 TID 6858. Its same-run healthy comparator is Pod UID `02caebe2-0e4b-4ee0-91c0-df7f619b2b09`, QEMU PID 6688, CPU 0 TID 6846. Both match the pinned executable hash and build ID above, enable all 15 sites, and report no setup errors or missing exact sites. These are two different guest workloads, not matched performance arms.

Offline review joins events by owned TID, vCPU index and APIC pointer, pairs every retained EOI before/after, and checks that it removes the highest ISR bit without adding another. All retained acceptance sequences have matching return-vector and guest-before/after records; all retained EOI pairs satisfy that state rule.

| Retained CPU 0 evidence                        |   Failed Codex |  Healthy Claude |
| ---------------------------------------------- | -------------: | --------------: |
| Timer 236 acceptance / completed QEMU dispatch |      678 / 678 |   2,961 / 2,961 |
| EOI pairs clearing timer 236                   |            677 |           2,961 |
| EOI pairs clearing vector 48 / 251 / 253       | 139 / 301 / 66 | 147 / 928 / 125 |

The failed transition is at native host tracefs monotonic seconds, not guest uptime:

| Time (s)              | Native observation                                                                                                |
| --------------------- | ----------------------------------------------------------------------------------------------------------------- |
| 639.989679            | Accept timer 236 on APIC `0x555a49e79140`, ID 0; ISR7 becomes `0x1000`.                                           |
| 639.989775            | `apic_get_interrupt` returns 236 on CPU 0 TID 6858.                                                               |
| 639.989779            | Before guest dispatch: CPU pointer `0x555a49f62160`, index 0, same APIC, EIP `0xffffffffb5ebb42f`, flags `0x202`. |
| 639.989806            | Dispatch returns: same identity, EIP `0xffffffffb60015f0`, flags `0x2`, IDT base unchanged.                       |
| 641.607219            | Accept higher-priority IPI 251; ISR7 is now `0x08001000`, with another timer pending.                             |
| 641.607454            | Before IPI dispatch, guest EIP is back at `0xffffffffb5ebb42f`, flags `0x202`.                                    |
| 641.607970–641.608055 | IPI EOI changes ISR7 from `0x08001000` to `0x1000`.                                                               |

After the last retained timer acceptance, 327 retained EOI pairs clear 265 vector-251 and 62 vector-253 interrupts while leaving timer 236 in service. The last CPU 0 EOI sample is at 916.425072 seconds. Serial independently repeats `pv_native_safe_halt+0xf`, zero CPU 0 RCU ticks and unchanged softirq counters, without systemd startup. x2APIC is rejected during boot. The exact EOI probes surround QEMU's xAPIC MMIO callsite; they do not observe every possible APIC state writer or prove execution of Linux's handler after QEMU sets the entry PC.

Coverage needs care. Both APIC collectors report zero kernel overruns/drops and zero overwrites in their named CPU 0 reserve. The failed collector overwrites 571,322 CPU 1 and 128,246 other-group records. Review found four healthy CPU 0 records printed as `<...>-6846`; joining by TID recovers the extra timer acceptance and dispatch, balancing all 2,961 acknowledgements. The old collector placed anonymous records in the other reserve, so its zero named-CPU-0 overwrite count cannot prove uninterrupted coverage if that reserve overwrote. No anonymous failed CPU 0 records survive. This does not establish that none were lost. The collector now resolves anonymous owned TIDs to their known/procfs comm before selecting a reserve; a noisy-pipe regression exercises an anonymous first record.

The separate IOTLB collector cannot fill the gap: its failed-guest rolling output only retains 925.633944–927.986022 seconds, far after the transition, with 603 kernel overruns and 670,142 overwritten records. It reports no observed assertion; that is not proof of no earlier mapping problem. There is no guest instruction trace at the IDT target or timer acknowledgement in this execution. Capturing that exact-kernel path, with verified CPU 0 retention, is the remaining discriminator between guest execution and TCG state/translation faults. This evidence supports neither a kernel/QEMU patch nor a timeout increase. No new boot repetition or provider outreach was needed for this analysis.

## Earlier managed TCG failures

On [Actions service grants PR #16467](https://github.com/agentydragon/ducktape/pull/16467) at
`b0a5719548b8f96d3f4591189953032fdaa0845c`, the [failed test action](https://app.buildbuddy.io/invocation/e8fc2499-aeae-572a-b01b-07ac6371e588)
took 665.362 seconds. VM 2's VMI and launcher reported Ready while Runtime was
`NeverConnected`. The 300-second readiness wait was followed by the 600-second
scenario deadline during cleanup. Gateway and Sandbox Controller logs report
`no route to host` on port 9090, with no retained TLS or authorization error.
Actions logs are absent. VM 1 completed six scripted upstream exchanges. VM 2's
serial records PID 1 blocked in `i8042_probe` and CPU 0 in `pv_native_safe_halt`
with zero ticks in the RCU grace period, an unchanged timer-softirq count of 21
and a missed timer wakeup. The wedge preceded userspace and receiver authorization.
The guest used Debian `6.12.115+deb13.1-cloud-amd64` on TCG with two vCPUs and
no boot observer. The PR changed allowlist entries and kept the outgoing Actions
transport.

The 00:56:43 UTC host snapshot for launcher Pod UID
`895c7dbc-c68b-4c01-87ac-6dbd0339bfdd` shows both vCPUs halted with interrupts
enabled. CPU 0 had unmasked timer vector 236 in service, vectors 52 and 236
pending, and priority `0xe0`. CPU 1 had neither pending nor in-service vectors.
The priority blockage accounts for the missed timer and the downstream probe
wait. Delivery and acknowledgement leading to that state were unobserved.

[PR #16945](https://github.com/agentydragon/ducktape/pull/16945) at `f89eba69`
[failed in 781.6 seconds](https://app.buildbuddy.io/invocation/a5515ec2-4bc4-5047-ba40-b3d2d5007648).
Serial records **CPU 1** with zero ticks in the RCU grace period, halted in
`pv_native_safe_halt` and missing its timer wakeup. The App was Ready at 08:53:56 UTC, the other
guest completed its scenario and cleanup, and VM 1 exhausted the 300-second
readiness and 300-second cleanup waits. All 92 successful CPU 1 LAPIC samples
from 08:54:50.428 to 08:59:37.713 UTC show timer vector `0xec` both in service
and pending with PPR `0xe0`. Vectors 46, 51, 52 and 53 later joined the pending
set. The late snapshot still shows QEMU `running` with the vCPU halted and
interrupts enabled. A [November 9 devel failure at `0e7fc09b6a19b`](https://app.buildbuddy.io/invocation/c3a9fd9f-e61f-5e11-8d20-4ff9ccf8d81f)
already showed the same CPU 1 zero-tick, idle-stack and missing-wakeup signature.
The seven-day target-history index included 837 status rows with cached results.
Use the matched execution as recurrence evidence, with the index as a locator.

The five-second `context deadline exceeded` on #16945 came from a background
`stats/summary` query to the test's own k3s/kubelet. Sampling resumed, and
controller and QMP exec requests continued. Available memory stayed above
13.6 GiB, failed CPU 1 accumulated about 1.37 seconds of host scheduler wait
by 08:59 and the action queued for about 60 ms. These observations argue against
sustained resource starvation. A brief physical-host event remains possible
without provider telemetry. An earlier provider attempt lost its Firecracker
socket before guest boot. Full run references, artifact paths and source
comparisons are in the [historical VM CI report](https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/2026_11_12_vm_ci_reliability.md#boot-interrupt-stall-2026-11-16).

### Actions grants control

Both recorded controls added diagnostics to the PR head. The [single opt-in run](https://app.buildbuddy.io/invocation/056388a3-0a6c-4d48-8a75-729c1c0c180e)
added observer code to #16467 and passed `test_vm` in 284.7 seconds and observer
unit tests in 15.7 seconds. The guest image, CPU count, assertions, auth and
deadlines were held constant. Both launcher UIDs were found. QEMU rejected
`apic_register_write` as an unknown event, leaving both traces empty.

The separate paired batch applied identical diagnostics and used
`--runs_per_test=10 --nocache_test_results --flaky_test_attempts=1`. Each arm
retained ten distinct run IDs at attempt 1 and both guest consoles per run:

| Source before diagnostics                         | Invocation                                                                            | Result                       |
| ------------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------- |
| #16467 `b0a5719548b8f96d3f4591189953032fdaa0845c` | [05413723](https://app.buildbuddy.io/invocation/05413723-5146-4b10-8efd-b40c2e5aff3a) | 10/10 passed (245.9–391.8 s) |
| devel `a8c4b186458f1522d783c74c5cd63ab6a35e36b8`  | [3ea624ce](https://app.buildbuddy.io/invocation/3ea624ce-ced3-4fc5-91c9-6e1cd53878c8) | 10/10 passed (235.0–380.0 s) |

With diagnostics, the effective commits were `ccdd85dfeea9a104453beb328e621c034729f8a6`
for #16467 and `38574eada7d5b64dfa8eb71471f9c07b845e39d3` for devel. Two attempts
with abbreviated SHAs failed during checkout, before any test executed. The timer
stall did not recur in 40 guest consoles. QEMU exposed no APIC trace events.
Each arm accepted `log int` for 16 of 20 boots alongside repeated
`info lapic` and `info pic` samples. Logging generally hit 16 MiB before
userspace while sampling continued. Six observers selected QEMU PIDs that
disappeared before reading `/proc/<pid>/cmdline` and aborted before starting
their monitor timelines. Two more devel observers exhausted startup discovery.
The collector now checks for the logging PID again after Running and keeps
monitor sampling if logging discovery fails. The original stall's delivery/EOI
sequence and any effect of the grants change on recurrence remain unmeasured.
See [the collector change](https://github.com/agentydragon/ducktape/commit/1bb5bd25230d2045d3ea7e2df74c8bdebabbece3).

The November 15 history comparison covered six pages back to November 13
at 01:52 UTC and omitted the PR failure. The three failed rows cover a scenario
timeout with two orderly guest powerdowns, a readiness failure with both guests
reaching systemd/Runtime and a missing shutdown proof later traced to a QEMU boot
assertion. Full run and collection details are in
the [historical Actions grants report](https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/actions_service_grants_vm_boot.md).

## Cleanup follows the requested sequence

| UTC          | Observation                                                                                                                                                       |
| ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 08:32:23.175 | Readiness fails after 300.118 seconds; `RuntimeConnected=False/NeverConnected`.                                                                                   |
| 08:32:23.539 | Suspend accepted.                                                                                                                                                 |
| 08:32:23.611 | Suspended observed.                                                                                                                                               |
| 08:32:23.625 | Exact Sandbox deletion accepted.                                                                                                                                  |
| 08:37:23     | Same UID retains deletion timestamp and `state-volume` finalizer, `stopStage=Quiescing`, no quiesce receipt and a retry for the same Pod UID. VM/Pod still alive. |
| 08:37:23.797 | Cleanup ends with its 300.275-second timeout.                                                                                                                     |

The [driver](../../driver/app_sandbox.py) waits for disappearance of the exact UID; [owned cleanup](../../driver/cleanup.py) drains despite caller cancellation and retains its failure. The [controller finalizer](../../../environments/internal/controller/controller.go) seals published incarnations before deleting workloads/state. `TestDeletionSealsAnAcceptedIncarnationWithoutARuntimeSnapshot` expressly preserves this rule after controller takeover: absent observation cannot prove absence of previously admitted Sessions.

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
