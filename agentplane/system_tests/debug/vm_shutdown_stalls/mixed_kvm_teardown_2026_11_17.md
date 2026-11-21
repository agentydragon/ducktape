# Mixed-pool VM teardown investigation

The November 17 mixed-pool comparison produced two distinct failure chains. The
self-hosted KVM guests exceeded their existing shutdown grace while still making
progress. The managed TCG guest failed before Runtime connected, then could not
complete quiescence or fixture storage cleanup. Neither observation implicates
the larger executor retention cap; the four measured KVM fixtures were cold, and
their shutdown preceded the runner-retention decision.

## KVM: real over-grace shutdown, with substantial console overhead

The [original comparison](https://app.buildbuddy.io/invocation/90d926d2-0b20-42a1-a3cd-11a9ef25a04d)
ran source `501db4bd091984653f6430de69f85f3de7bc8062`. Both scenario bodies passed
in each of its four KVM suite executions. Runs 3 and 4 failed the shared fixture's
shutdown check. The three affected launcher incarnations had ACPI request
receipts, no proof-helper entry, no shutdown-complete receipt in either retained
CRI output or independent raw serial, and a later stopped/destroyed domain event:

| Suite run | Guest  | First shutdown signal, UTC | Forced domain stop, UTC | Interval |
| --------- | ------ | -------------------------- | ----------------------- | -------- |
| 3         | Claude | 09:49:26.769680            | 09:49:58.090931         | 31.32s   |
| 3         | Codex  | 09:49:35.077322            | 09:50:05.909666         | 30.83s   |
| 4         | Claude | 09:50:59.699947            | 09:51:30.613364         | 30.91s   |

This is a guest lifecycle failure, not just a malformed or lost receipt. The
independent watchdog recorded PID 1 moving between epoll, cgroup fork, inotify
release, and console waits. Run 3's Claude SysRq dump caught PID 1 in
`close -> inotify_release -> fsnotify_destroy_group -> __flush_work` and unmounts
in `virtio_kill_sb -> bdi_unregister -> synchronize_rcu_expedited`. Other samples
showed those operations finishing and later unmount jobs starting. The proof
unit was still held behind mount jobs when the domain was destroyed. These are
observed waits, not proof of a permanent deadlock. RCU waits also appeared in a
guest that completed orderly shutdown.

Eleven existing host register samples could be symbolized against the exact
guest kernel: nine stopped just after `io_serial_out`'s `outb` to port `0x3f8`,
with ASCII characters in the accumulator; two stopped just after serial-register
reads. All eleven had interrupts disabled on that CPU and the other CPU halted
at `pv_native_safe_halt+0xf`. Sampling can favor VM exits: these sparse snapshots
are not a CPU-time profile. They nevertheless identify console I/O at the
observed shutdown boundary, rather than an unknown instruction.

The deployed Debian kernel is `6.12.115-2`, release
`6.12.115+deb13.1-cloud-amd64`. The pinned kernel package SHA-256 is
`c26573a3b5dca6bd3bf2b06a13b81cbb845c04eb6e35d7ad57f4b885746153e4`.
The ELF extracted from its boot image and the matching debug package both have
GNU build ID `7e60601a433e9f8f8e8817516070311f1af3b8bc`. The debug package supplied
the symbol map; the halted CPU's known instruction and the kernel's 2 MiB KASLR
alignment supplied the per-guest slide. Disassembly of the pinned image places
`io_serial_out+0x1b` immediately after `outb`, and `io_serial_in+0x19` after
`inb`. Linux's [serial driver](https://github.com/gregkh/linux/blob/v6.12.115/drivers/tty/serial/8250/8250_port.c)
and [RCU stall documentation](https://www.kernel.org/doc/html/v6.12/RCU/stallwarn.html)
support treating slow console output and interrupt-disabled work as plausible
contributors. They do not establish why each RCU grace period waited.

## Controlled console-verbosity trial

One unchanged arm and one quieter arm each ran four full ordinary KVM VM suites
and four full Firecracker suites on the existing `kvm-vm` pool. Every suite kept
both Claude and Codex scenarios. The quieter source changes only the ACPI
handler's `systemctl log-level debug` to `log-level info` and its failure message.
It retains the console target, watchdog, SysRq worker, storage proof, and all
shutdown deadlines. No worker pin or infrastructure setting was changed.

| Evidence                                           | Unchanged                                                                             | Normal verbosity                                                                      |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Invocation                                         | [cb6b3385](https://app.buildbuddy.io/invocation/cb6b3385-424c-4c2a-890e-13b6e60467aa) | [9bcd50ee](https://app.buildbuddy.io/invocation/9bcd50ee-7854-4781-9b54-9888b47735e7) |
| New uncached actions                               | 8/8 passed                                                                            | 8/8 passed                                                                            |
| JUnit cases                                        | 16/16 passed                                                                          | 16/16 passed                                                                          |
| KubeVirt guests with shutdown proof                | 8/8                                                                                   | 8/8                                                                                   |
| Orderly domain stops / final guest powerdown lines | 7/8                                                                                   | 8/8                                                                                   |
| Forced domain stops                                | 1/8                                                                                   | 0/8                                                                                   |
| Request to proof, median                           | 16.42s                                                                                | 7.81s                                                                                 |
| Request to proof, maximum                          | 28.21s                                                                                | 13.33s                                                                                |
| Median shutdown console payload                    | 160,613 bytes                                                                         | 48,432 bytes                                                                          |

These clocks run from the first exact ACPI receipt to the exact completion
receipt in CRI forwarding timestamps. They include possible forwarding delay.
Payload counts exclude CRI metadata and include all forwarded payload after that
request. They are not request-to-full-poweroff or whole-CI measurements.

Both arms used the frozen source above. The candidate's exact patch SHA-256 and
per-action native/BES/JUnit joins are in the accompanying JSON. The remote runner
confirmed the candidate patch applied. Each action was `NEW`, its native request
had `skipCacheLookup=true`, and its native `test.xml` digest matched the downloaded
BES artifact. There were no skipped cases or replacement test attempts.

The control's run 2 Claude guest emitted storage proof at 10:50:06.5926057 UTC,
28.21 seconds after its first ACPI receipt, but never retained a final powerdown
line. Its launcher recorded a forced stopped/destroyed event at
10:50:09.207666 UTC. The raw serial agrees with the CRI capture. The suite still
passed: the ordinary `_require_shutdown` gate requires the exact storage-proof
receipt, not the kernel powerdown line or an orderly libvirt stop reason. Keep
that weaker gate distinct from a complete graceful shutdown. The other seven
control guests and all eight quieter guests retained both final powerdown and
orderly stopped/shutdown events. No current guard was loosened for the trial.

The selected command, run once per arm, was:

```sh
bbr test --config=rbe --config=ci --test_tag_filters=-live_openai_api \
  --nocache_test_results --flaky_test_attempts=1 --runs_per_test=1 \
  --runs_per_test=//agentplane/system_tests/x/vm_kvm:test_vm_kvm@4 \
  --runs_per_test=//agentplane/system_tests/suites:test_firecracker@4 \
  --jobs=30 --keep_going --test_output=errors \
  //agentplane/system_tests/x/vm_kvm:test_vm_kvm \
  //agentplane/system_tests/suites:test_firecracker
```

The control additionally used
`--run_from_branch=devel --run_from_commit=501db4bd091984653f6430de69f85f3de7bc8062`;
the candidate used automatic patch upload from that same source. The pool
template sampled during the control had four vCPUs, an 8 GiB memory limit and
request, and a two-CPU request; all eight workers were Ready at that observation.

This is a small sequential trial with unrelated load, not a randomized comparison.
It used ordinary KVM fixtures, which dispose their cluster, instead of the
original cold recycled fixtures on their private ext4 disk. It did not include
the original comparison's four singleton suites. Both unchanged and quieter
arms passed, so this supports a console-overhead reduction; it does not establish
that the earlier teardown flake is fixed. The historical missing-proof failures
predate the November 6 debug instrumentation, so this cannot explain that entire
failure family either.

## Managed TCG: boot timer stall, quiescence hold, then loop cleanup failure

The [managed comparison](https://app.buildbuddy.io/invocation/0bf614c9-5add-4abc-92a6-92601ae4dca5)
run 4 reused a warm k3s fixture, but its new Claude guest never connected Runtime.
The console contains thirteen RCU warnings with CPU 0 taking no timer ticks,
an unchanged softirq count, and repeated NMI backtraces in
`pv_native_safe_halt`. The first warning was at 10:04:59.777833848 UTC, before any
shutdown request. This failure therefore cannot be caused by the ACPI handler's
shutdown log level.

The existing boot collector retained 84 successful CPU 0 LAPIC samples from
10:04:20 through 10:09:19 UTC, all with timer vector 236 in service and processor
priority `0xe0`; the timer was also pending from the second sample onward.
The retained APIC trace last accepted timer vector 236 on CPU 0 at host-monotonic
936.113062. Subsequent higher-priority vector-251 acknowledgements and EOIs still
occurred, but no further timer acknowledgements appear before the byte cap.
All saved trace overrun/drop counts are zero, but the trace is explicitly
truncated at 16 MiB. These observations narrow the timer-delivery boundary;
they do not yet identify the writer or a safe emulator/kernel repair.

The upstream QEMU 10.1 [APIC implementation](https://github.com/qemu/qemu/blob/v10.1.0/hw/intc/apic.c)
interprets the sequence: `apic_get_interrupt` moves a pending vector from IRR
to ISR before returning. Vector 236 in service blocks another interrupt of the
same priority class, while higher-class vector 251 remains eligible. Each EOI
clears the highest-priority ISR, so an EOI for a later 251 need not clear 236.
The [TCG injection path](https://github.com/qemu/qemu/blob/v10.1.0/target/i386/tcg/system/seg_helper.c)
runs after virtual-APIC acceptance. The return probe does not establish that
injection, guest interrupt entry, or the normal Linux timer EOI occurred; it
also does not exclude another EOI path. This is a source comparison with
upstream 10.1, not a claim to have identified a CentOS downstream defect.

A late 10:13:44 report still shows ISR 236 but PPR `0x10`, unlike the consistent
boot samples. The [LAPIC dumper](https://github.com/qemu/qemu/blob/v10.1.0/target/i386/cpu-dump.c)
reads ISR, IRR, and PPR separately; it is not an atomic snapshot. The output
labels and QMP `cpu-index` do select CPU 0, but that internally inconsistent
report cannot prove that the earlier priority condition cleared. Guest-handler
entry remains the missing discriminator, not another unsupported EOI workaround.

After the 300-second readiness failure, Sandbox cleanup waited another 300
seconds. The final snapshot still had the launcher Running and the deleting
Sandbox in `Quiescing`. This follows the controller's
[conservative sealing contract](../../../environments/README.md): failure to
contact Runtime does not prove there is no session state to preserve.

Fixture teardown then rejected the surviving Sandbox in `_baseline_clean` and
tried to detach `/dev/loop0`. Retained k3s and its containers had not been stopped;
the outer snapshot invalidation guard only requested that BuildBuddy discard the
runner after upload. The loop mapping was still attached. Its exact remaining
file-descriptor holder was not captured, so attributing the descriptor to QEMU
specifically remains an inference.

The failure-only guard from [PR #17323](https://github.com/agentydragon/ducktape/pull/17323)
in `deployment/stack.py` runs after opening the retained baseline. It calls the existing ownership-checked
`baseline.stop_owned` after ordinary resource cleanup and before raw volume
cleanup. Successful stacks keep their warm daemon. If the stop fails, the
original error is re-raised with a note. The original [focused regression](https://app.buildbuddy.io/invocation/9b5ce5c7-e6bb-4559-a320-d9cd479227c0)
exercised a simulated resource unwind. A later retained-stack probe found a
kubelet block-device lease still attached after userspace owners exited.
[PR #17371](https://github.com/agentydragon/ducktape/pull/17371) added identity-checked lease
release. The real Ready-VM error probe then preserved the injected failure and
detached both mappings. See [the repair qualification and its repeat command](../../x/vm_kvm/README.md#retained-failure-cleanup-qualification).

## PR CI: QEMU boot assertion reported by the teardown gate

[PR #17324](https://github.com/agentydragon/ducktape/pull/17324)'s first
[CI invocation](https://app.buildbuddy.io/invocation/efa57d6e-366a-59fd-9f3f-4144a5f4b0f3)
failed `//agentplane/system_tests/suites:test_vm` at PR head `7a82dfc2ea`, tested
as merge `d84118c2235f4faaeaaf72f8461f7fba8b64c621`. Both scenario bodies passed,
but the first Claude launcher (`b19a097a-46be-4dd3-a45d-7ca65b78771a`) crashed
during boot. Its serial ends during PCI enumeration at guest uptime 3.871820
seconds. Its launcher records
`iotlb_to_section` asserting `section_index < d->map.sections_nb` and native QEMU
shutdown with `reason=crashed` at 11:29:53.542 UTC. Neither raw serial nor CRI has
an ACPI receipt from that incarnation. It never reached the changed handler.
This matches the independently tracked [QEMU boot assertion #16057](https://github.com/agentydragon/ducktape/issues/16057),
not the managed timer stall or a post-ACPI shutdown delay.

KubeVirt replaced the crashed guest. The replacement Claude and the Codex guest
both retained exact storage-proof receipts, final powerdown lines in independent
serial and CRI, and orderly launcher stop reasons. Their ACPI-to-proof intervals
were 4.80 and 4.80 seconds. The gate correctly rejects the earlier crashed
incarnation despite the successful replacement; it must not omit that receipt.

The bounded IOTLB observer added by [#17034](https://github.com/agentydragon/ducktape/pull/17034)
captured the assertion. The accompanying JSON retains the last trace window,
artifact digests, exact executable identity, and loss metadata. Host-monotonic
timestamps show:

1. At `275.615681`, CPU 1 prepared an APIC-page I/O fill using the old dispatch
   `0x7fc8fdaae5f0`, with 164 sections. Its section pointer implies index 162.
2. At `275.615725`, that CPU's MMIO access supplied packed index `0xa030a2`
   (low 12 bits: 162), attributes zero, physical page `0xfee00000`.
3. At `275.615731`, CPU 0 reached publication of CPU 1's same AddressSpace
   `0x55ebf615ca30`, replacing the old dispatch with `0x7fc8fe369000`, which has
   161 sections.
4. At `275.615752` the lookup selected that new dispatch with index 162, and at
   `275.615803` the failure probe retained the same invalid pair.

This positively supports a map-generation mismatch at the assertion. In
[upstream QEMU 10.1](https://github.com/qemu/qemu/blob/v10.1.0/system/physmem.c),
`memory_region_section_get_iotlb` derives an index from the section's FlatView,
whereas `iotlb_to_section` looks it up in the AddressSpace's current dispatch.
The candidate fill's FlatView matches CPU 1's ordinary AddressSpace before its
replacement. No `iommu_target` event is retained. This makes publication/flush
ordering the next discriminator; absent target-change events do not rule out
another path or establish which upstream patch repairs this build.

The trace is incomplete: 60,027 records were overwritten, kernel overrun counts
total 1,514, and three records were malformed. Thread discovery, attachment, and
failure-to-stop gaps also apply. The fill probe precedes the final TLB store;
victim copies, RAM fills and individual-page invalidations are not fully traced.
No CPU 1 flush appears between the retained fill and assertion, but loss prevents
promoting that absence to proof that no flush occurred. The exact matching
fill/access provenance and a safe emulator repair therefore remain unproven.

### Earlier IOTLB assertions

The same assertion occurred on [#16997 at `a84addf4`](https://app.buildbuddy.io/invocation/0b4780e9-5f98-5c50-bf88-dcf69bbcb2e1)
and on devel at [`2faec465` on November 13](https://app.buildbuddy.io/invocation/fe172529-66fa-5a0f-bbe1-bab5f5372d6a)
and [`b15bd054` on November 10](https://app.buildbuddy.io/invocation/ed5acdf3-4dd2-524c-9d20-5e81631698c9).
All three used QEMU `10.1.0-10.el9`, libvirt `11.9.0-1.el9`, host Linux 5.15 and
two guest vCPUs under explicit TCG with no `/dev/kvm`. The failures occurred
in the CI k3s/Firecracker environments.

QEMU v11 contains [`854cd16e318e`](https://github.com/qemu/qemu/commit/854cd16e318eed12de2995014b28d9f374c64bf7),
which addresses a translation through an `IOMMUMemoryRegion` that changes
`target_as`. The earlier domain configurations used direct-kernel q35 boot
without EFI/pflash or a vIOMMU. Default SMM supplies `cpu-memory` and `cpu-smm`
AddressSpaces, but its presence alone does not establish that IOMMU translation.
The later trace above supports investigating the map-generation mismatch in
the same AddressSpace. Loss and incomplete provenance leave the safe repair open.

All 14 exact-executable probe definitions registered on the Linux 5.15
Firecracker executor, including nested user-memory fetches. The [observer contract](../../vm/README.md#qemu-iotlb-assertion-capture)
records binary verification, isolation from the APIC sampler and collection
limits. The [historical observer report](https://github.com/agentydragon/ducktape/blob/22aab7654376a05a3ffaa592dbfaf6ec8e60d1bb/agentplane/debug/2026_11_12_vm_ci_reliability.md#qemu-iotlb-assertion-distinct-from-the-timer-stall)
retains the original debuginfo identities, fixture results and upstream comparison.

## Later qualification and open questions

Normal log verbosity restored some KVM shutdown margin in the ordinary-fixture
trial. With the storage fix and reduced verbosity in place, two later recycled
KVM bursts each retained strict orderly-shutdown evidence for all eight guests.
The cold managed control failed and its second burst was canceled. The later
window leaves the mixed-pool comparison open and does not isolate what prevented
the earlier KVM failures. See [the post-fix qualification](https://github.com/agentydragon/ducktape/blob/b3b90e445a785bbde3c2ddb78b201b932d5abc5d/latency/evidence.md#post-fix-qualification-evidence).

The managed boot investigation now includes a natural recurrence with exact
APIC probes. QEMU returned from dispatch for the retained timer vector, but
the capture lacks guest instruction flow and has a trace-retention gap. The next
discriminator is execution at the guest interrupt entry and acknowledgement
with verified CPU 0 coverage. See [the recurrence](../vm_boot_stalls/vm_runtime_connection_2026_11_17.md#natural-devel-recurrence-with-exact-probes).
