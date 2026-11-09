# VM readiness failure before userspace, November 9, 2026

The Codex VM in [invocation
`46993f63-7c5b-5685-a009-4664f7c8318f`](https://app.buildbuddy.io/invocation/46993f63-7c5b-5685-a009-4664f7c8318f)
never reported Ready. Both its retained raw serial file and console sidecar log
end during early Linux PCI/ACPI initialization. Neither contains initramfs,
systemd, or Environment Runtime startup output. The gateway's missing first state
message is downstream of this observed boot boundary; changing credentials or
gateway timeouts is not supported by this evidence.

This does **not** identify the blocked kernel instruction, prove a QEMU defect,
or rule out lost console output. There is no retained live host-thread/register
snapshot from the stall. The report-time snapshot ran after cleanup destroyed the
domain. The next useful evidence is passive host QEMU state before cleanup,
followed by kernel initcall tracing or a deliberately labeled monitor/debugger
capture if the failure recurs.

## Exact attempt

- Target: `//agentplane/system_tests/suites:test_vm`, run 1, shard 1, attempt 1.
- Source: `a099f6439911ed7e0221e5df257ae99a19a4f13b`, draft
  [PR #15126](https://github.com/agentydragon/ducktape/pull/15126).
- BES physical test window: 09:48:42.318–09:57:43.667 UTC, 541.349 seconds.
  Pytest reports 520.84 seconds. These are target clocks, not full CI latency.
- Failed Sandbox: `systest-vm-2`; its launcher Pod and VMI reported Running/Ready,
  while `EnvironmentRuntimeConnected` and Sandbox Ready remained false with
  `ConnectionPending`.
- The launcher explicitly reports absent `/dev/kvm` and software emulation; its
  final command uses `-accel tcg`, QEMU `10.1.0-10.el9`, two guest vCPUs and
  1536 MiB RAM. This is the normal TCG test lane, not the separate manual KVM lane.
- Guest: Debian Linux `6.12.115+deb13.1-cloud-amd64`, package `6.12.115-2`.
  Kernel arguments: `console=ttyS0 net.ifnames=0 agentplane.shutdown-sysrq=1`.
- All three independent VM scenarios start concurrently. The other two guests
  became Ready and completed their scenarios in this same action.

| UTC                       | Observation                                                            |
| ------------------------- | ---------------------------------------------------------------------- |
| 09:49:02.446–09:51:22.300 | Shared stack setup, 139.854 seconds.                                   |
| 09:51:25.452              | VM 2's readiness wait begins.                                          |
| 09:51:52.770–09:51:52.785 | Launcher resumes the domain and reports Running.                       |
| 09:51:58.79               | Console begins showing Linux boot output.                              |
| 09:52:03.088              | Final console line, guest uptime 5.157459 seconds.                     |
| 09:52:47.209              | OOM guest becomes Ready, after 81.760 seconds.                         |
| 09:53:03.527              | VM 1 becomes Ready, after 98.076 seconds.                              |
| 09:56:25.561              | VM 2's 300-second readiness wait expires.                              |
| 09:56:25.75               | Cleanup requests graceful shutdown; no guest completion proof follows. |
| 09:56:56                  | KubeVirt destroys the domain after its existing shutdown grace.        |
| 09:57:08.621              | Report-time QEMU snapshot finds no matching process and Pod NotFound.  |

The failed guest's serial tail is:

```text
[    5.156924] ACPI: PCI: Interrupt link GSIF configured for IRQ 21
[    5.157203] ACPI: PCI: Interrupt link GSIG configured for IRQ 22
[    5.157459] ACPI: PCI: Interrupt link GSIH configured for IRQ 23
```

VM 1 continues past the same point to `iommu: Default domain type: Translated`
at uptime 5.188278 seconds, then clocksource initialization and userspace. The
last printk narrows the observed phase; it is not a stack trace of the blocker.
There is no VM 2 guest journal in the advertised test artifacts. That absence is
consistent with early boot, but does not independently prove that journald never ran.

## Failure classification and source boundary

The same invocation's `test_pod` also failed readiness, but its recorded cause is
`Unschedulable`: the inner K3s node reports **Insufficient memory** for
`systest-msg-from-1`. That workload never reached Running. Treat this separately
from a Running VM with an early-kernel console tail.

A 48-hour target-history query returned 193 rows, including two older failures.
The index omitted this PR's failure, so its counts must not be presented as a
representative failure rate. Both inspected older failures completed all three
readiness waits and failed later in the OOM command assertion:

| Invocation                                                                                                          | Failure after readiness                                                                                                         |
| ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| [`993090b5-818b-5fd1-a093-892740372ed0`](https://app.buildbuddy.io/invocation/993090b5-818b-5fd1-a093-892740372ed0) | Expected exit 137; exec-server transport disconnect/reset and an unknown resumed session ID prevented the expected tool report. |
| [`79acbe48-12c4-579a-a163-3c8ab29771a9`](https://app.buildbuddy.io/invocation/79acbe48-12c4-579a-a163-3c8ab29771a9) | Expected exit 137; the allocating command instead reported exit 143.                                                            |

Comparison with investigation source `4a2ef6f0f1d4e9da7d7ea7b3dcc08bf0dcc00ea0`
found no changes to the VM CPU/kernel/initramfs configuration or readiness path.
Image-import scheduling and post-initramfs workload trust setup do differ;
therefore a current-source pass would not be an exact-source reproduction or a
causal test of those differences.

Relevant source owners:

- `vm/infrastructure.py` selects KubeVirt emulation; `vm/configuration.py` owns
  test guest CPU/RAM.
- `deployment/fixture_chart.py` constructs direct kernel/initramfs boot;
  `vm/guest/initramfs_init` eventually switches to systemd.
- `driver/sandbox.py` owns readiness and shielded suspend/delete cleanup.
- `vm/host_qemu.py` and `vm/boot_rcu.py` already provide bounded host collectors.
- Runtime's first state and Gateway observation occur after guest userspace
  startup, in `env/runtime/control/server.go` and
  `runner_gateway/gateway/runner_observer.go`.

Earlier [guest-vCPU trials](vm_cpu_budgets.md) and
[boot/shutdown investigations](vm_shutdown_stalls/README.md) recorded TCG RCU/timer
stalls and QEMU assertions. They are useful comparisons, not a demonstrated
shared cause for this failure. The five-run passive-observer batch on November 7
passed without reproducing those stalls.

## Evidence and reproduction

The adjacent [evidence index](vm_boot_before_userspace_2026_11_09.json) preserves
native BES result identity, artifact paths, CAS URIs and hashes. Raw launcher
logs contain encoded domain configuration; retain them as authenticated
BuildBuddy artifacts instead of copying full logs into this report.

```bash
bbapi target log 46993f63-7c5b-5685-a009-4664f7c8318f \
  //agentplane/system_tests/suites:test_vm
bbapi artifact list 46993f63-7c5b-5685-a009-4664f7c8318f --kind test --failed --json

# One uncached real-suite observation; a pass does not establish a repair.
bbr test //agentplane/system_tests/suites:test_vm \
  --test_env=AGENTPLANE_VM_BOOT_RCU_OBSERVER=1 \
  --nocache_test_results --flaky_test_attempts=1
```

The observer reads host `/proc` and does not issue QMP commands. Its guest
component requires systemd, so an early-kernel stall can only be observed from
the host. Observer output must be checked for coverage gaps, missing identities
and truncation before interpreting absent samples.

The readiness-failure collector uses the last matching Sandbox descriptor and
runs inside the existing shielded cleanup task, before suspension/deletion.
It writes `vm-artifacts/readiness-failures/<sandbox-uid>.json`, recording the
Sandbox and launcher Pod identities, host QEMU thread states and collection
errors. Its existing process sampler limits scans to 0.5 seconds / 4096
processes and 16 threads; the output is capped at 256 KiB. It does not send QMP
commands, change guest settings, retry the scenario, or capture successful VM
boots or ordinary Pod failures. A missing process match is a coverage result,
not evidence that QEMU was healthy. Collection failure adds an exception note
and leaves cleanup ownership intact.

## Current-source observation: separate virtiofs startup failure

The [uncached observer attempt
`cb469644-f04f-4844-9021-f3a425dce6ef`](https://app.buildbuddy.io/invocation/cb469644-f04f-4844-9021-f3a425dce6ef)
failed for a different, concrete reason. It reconstructed source `4a2ef6f0`
from BuildBuddy's fetched base `e73261cb` plus a workspace patch. Its physical
test window was 11:01:32.859–11:10:31.785 UTC (538.926 seconds); pytest reports
482.14 seconds. The readiness timeout's last state was a replacement VMI still
Scheduling, rather than the original case's Running guest.

Every inspected QEMU launch failed before Linux with:

```text
tag="agentplane-environment-runtime-workload-trust"
tag property must be 36 bytes or less
```

That newly introduced virtiofs tag is 45 UTF-8 bytes. All four retained serial
files are empty. This is a launch-configuration defect, distinct from the
original attempt's Linux console stopping after 5.157459 seconds. It must be
fixed before another current-source boot observation can reach that phase.

The passive host observer exited cleanly after 312 samples spanning five
launcher Pod identities, including replacements. It found QEMU in only three
samples, with no incomplete process scans or Pod query failures. Those sparse
matches describe short-lived startup attempts; they cannot diagnose the older
running VM's stall. The launcher error, rather than zero process matches,
identifies this attempt's startup failure.

## Diagnostic validation

The focused lifecycle and passive-collector tests passed in
[`73ba35d7-0398-4d87-af07-4e0c704a97e5`](https://app.buildbuddy.io/invocation/73ba35d7-0398-4d87-af07-4e0c704a97e5):
`//agentplane/system_tests/driver:test_sandbox` and
`//agentplane/system_tests/vm:test_boot_rcu`. They exercise capture ordering,
identity fencing, collection failure and successful/Pod exclusions. Existing
cleanup tests retain cancellation and cleanup-error coverage.

Building both changed libraries by name, including their lint/type aspects,
passed in
[`c7fb5084-6535-49ac-940a-99e2bde53730`](https://app.buildbuddy.io/invocation/c7fb5084-6535-49ac-940a-99e2bde53730).
These checks validate the diagnostic repair; they do not reproduce or fix the
original early-kernel stall. The full-suite observer attempt above ran before
this diagnostic patch and was blocked by the separate tag-length defect.
