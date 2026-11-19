# VM shutdown stalls

The open shutdown question is what blocks a Ready guest between ACPI and orderly
powerdown. In the November 17 KVM comparison, three guests exceeded the grace
period while still advancing through shutdown jobs. Normal systemd log
verbosity reduced time to storage proof in a small ordinary-KVM trial. Two
later recycled KVM bursts each retained orderly shutdown for eight of eight
guests. The managed control failed, leaving the mixed-pool comparison open.
Older forced stops predate the debug logging and their exact blocker remains
unknown. See [the mixed-pool investigation](mixed_kvm_teardown_2026_11_17.md),
[the later qualification](https://github.com/agentydragon/ducktape/blob/b3b90e445a785bbde3c2ddb78b201b932d5abc5d/latency/evidence.md#post-fix-qualification-evidence)
and [the historical census](historical_census.md).

## Identify the failing boundary

- **Post-ACPI guest shutdown.** Join the exact guest's
  `AGENTPLANE_GUEST_SHUTDOWN_REQUESTED`, storage proof
  `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`, kernel `reboot: Power down`, and
  launcher stop reason. The ordinary suite requires storage proof. A guest can
  emit it and still be forcibly stopped before powerdown, as the November 17
  unchanged arm showed. Forced stops with missing proof also vary in unmount
  progress: the November 6 Claude guest reached both state unmounts, while the
  boot-debug experiment's Codex guest ended after the `mount-monitor-dispatch`
  rate-limit exit, before the `/state` helper appeared. A healthy guest can
  emit the same mount-monitor message or show a halted vCPU. See
  [the KVM trial](mixed_kvm_teardown_2026_11_17.md#controlled-console-verbosity-trial),
  [the November 6 failure](https://app.buildbuddy.io/invocation/15e9310a-7352-56a3-87c6-322dfb3065c0)
  and [the instrumented recurrence](https://app.buildbuddy.io/invocation/ac743375-b5cb-4109-aee8-67243e9ac78e).
- **Pre-Ready boot and readiness cleanup.** In the managed TCG timer stall,
  CPU 0 stopped taking timer ticks before userspace. A later exact-probe
  recurrence retained QEMU returning from interrupt dispatch and subsequent
  EOIs leaving timer vector 236 in service. Confirm guest interrupt entry and
  acknowledgement with verified CPU 0 retention. Earlier evidence includes a
  CPU 1 stall and a paired grants control with no recurrence. Separate captures
  show early PCI/ACPI silence and PID 1 waiting in cgroup/RCU code after ACPI
  during cleanup of a guest that had never become Ready. See
  [the managed timer recurrence](../vm_boot_stalls/vm_runtime_connection_2026_11_17.md#natural-devel-recurrence-with-exact-probes),
  [the earlier timer stalls and paired control](../vm_boot_stalls/vm_runtime_connection_2026_11_17.md#earlier-managed-tcg-failures),
  [early PCI/ACPI silence](../vm_boot_stalls/vm_boot_before_userspace_2026_11_09.md),
  and [the readiness-cleanup RCU wait](five_run_full_suite_rcu.md).
- **QEMU boot assertion.** An original launcher aborted in
  `iotlb_to_section` before userspace. Its replacement completed the scenario
  and powered down. The original crash still fails the teardown gate. The
  trace shows a map-generation mismatch with incomplete fill/flush provenance.
  See [the IOTLB assertion](mixed_kvm_teardown_2026_11_17.md#pr-ci-qemu-boot-assertion-reported-by-the-teardown-gate).
- **Suspend request cleanup.** In [development invocation `5f545d80`](https://app.buildbuddy.io/invocation/5f545d80-51a7-4573-a0a3-b059e18eb9c8),
  the cleanup request to `/upstream/environments/v1/environments/systest/systest-vm-1:suspend`
  received HTTP 503, then teardown reported an Identity still referenced.
  This happened before APIC probe attachment. The cause remains unexplained.
- **Host fixture teardown.** The November 17 managed TCG guest never connected
  Runtime. Its deleting Sandbox stayed in `Quiescing`, baseline certification
  rejected it, and fixture storage cleanup left a raw loop attached. The
  original mixed capture did not identify the remaining holder. A later
  retained-stack failure probe identified a kernel-held kubelet loop lease
  after all owned userspace processes exited. The merged identity-checked
  release passed a real Ready-VM injected-error probe, preserving the original
  error and detaching both mappings. The managed boot and quiescence failure
  and mixed-load comparison remain open. See [the mixed-pool capture](mixed_kvm_teardown_2026_11_17.md#managed-tcg-boot-timer-stall-quiescence-hold-then-loop-cleanup-failure),
  [the storage qualification](raw_block_kernel_lease_2026_11_17.md)
  and [the repeat command](../../x/vm_kvm/README.md#retained-failure-cleanup-qualification).

A transport or turn error can coexist with orderly guest shutdown. An outer
BuildBuddy Firecracker failure can leave an empty test output tree. Check
`bb execution get <executionId> --output=json` for `vm_log_tail.txt` in REAPI
`serverLogs`, and record provider retries separately from test attempts. See
[the serial RBE experiment's flake analysis](rbe_200_flakiness.md)
and [the original outer-VM panic observations](https://github.com/agentydragon/ducktape/blob/4ecd9e8e1dc60442658710da96e31b0342f4a06c/agentplane/system_tests/debug/vm_shutdown_stalls/README.md#rbe-execution-vm-failures).

## Collect the next recurrence

- Use the [manual failure watch](offline_failure_watch.md#repeat-the-review) to
  resolve the uncached child execution, run/shard/attempt, VMI UID and launcher
  Pod UID. Retain the same incarnation's raw serial, CRI console and launcher
  lifecycle before the artifacts expire. A replacement guest's proof cannot
  account for the original launcher. The assertion case has retained
  [serial](run6-serial.log.gz), [console](run6-console.log.gz),
  [launcher](run6-launcher.log.gz) and [report-phase host sample](run6-report.log.gz).
- For post-ACPI cases, correlate PID 1 and unmount-helper stacks, pending jobs
  and SysRq output with the host QEMU and virtiofsd timeline. The guest
  watchdog and SysRq still need guest and console progress. QMP can wake
  QEMU or vCPUs, and a report-phase sample can occur before ACPI or after the
  launcher is gone. The guest follows only the volatile journal across the
  late switch away from `/state`. Opening the persistent journal in a guest
  follower can itself hold the tested mount busy. A second mount of the same
  virtiofs tag shares the connection and changes when final teardown occurs.
  See [the VM runtime capture contract](../../vm/README.md),
  [SysRq collection limits](standalone_kubevirt/sysrq_default_ci.md) and
  [the guest stack assessment](stack_capture/epistemic_state.md).
- For boot cases, use the [full-suite RCU observer command](full_suite_boot_rcu_observer.md)
  or the [direct-VMI APIC preset](standalone_kubevirt/README.md#early-boot-apic-arm).
  Check attachment time, executable identity and trace loss before drawing
  conclusions from missing events. The [five-run boot observer batch](full_suite_five_run_boot_rcu_observer.md)
  retained partial captures. The [APIC collection contract](../../vm/README.md#boot-apic-delivery-and-eoi-capture)
  describes current coverage.

## Experiment reports and captures

- **Guest and host controls.** [Direct QEMU/TCG](tcg_reproduction.md) has the
  reduced guest scripts and logs. [Direct KubeVirt](standalone_kubevirt/README.md)
  has the CI guest, bounded policy A/B and the manual APIC negative control.
  The policy-off case deliberately induces a FUSE wait. See also
  [delegated cgroup stress](delegated_cgroup_stress.md),
  [what the synthetic slice arm exercises in cgroup v2](synthetic_cgroup_update_path.md)
  and [source-backed RCU comparisons](upstream_rcu_tcg_analogues.md).
- **Full-stack controls.** [Five full-suite executions](five_run_full_suite_control.md),
  [one-guest diagnostic](guest_only_reproduction.md),
  [passive virtiofsd snapshots and checksums](passive_virtiofsd_timeline/README.md),
  [five concurrent one-guest executions](fullstack_five_unperturbed/README.md),
  and [the 200-run job, commands and progress criteria](rbe_200_run_job.md).
  Passing controls exercise capture and ordinary shutdown. The historical
  census and passing batches do not yield a comparable fleet recurrence rate.
- **Collection variants.** [Libvirt monitor capture](libvirt_monitor/README.md),
  [proof-helper progress](standalone_kubevirt/proof_progress.md),
  [watchdog receipt framing](watchdog_receipt_framing.md) and
  [the overlength virtiofs tag startup failure](virtiofs_tag_limit.md).
  The [original investigation record](https://github.com/agentydragon/ducktape/blob/4ecd9e8e1dc60442658710da96e31b0342f4a06c/agentplane/system_tests/debug/vm_shutdown_stalls/README.md)
  preserves the early observer trials, source and patch identities, original
  invocation links and negative results.
