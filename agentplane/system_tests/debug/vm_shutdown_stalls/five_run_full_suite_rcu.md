# One uncached five-run full-suite batch: readiness cleanup and an RCU wait

On 2026-11-07 UTC, the full `//agentplane/system_tests/suites:test_vm` target ran five uncached times from a clean checkout at source `9dc88d0432576155057d07530de01a34e397a504` (which includes #14712 and #14710). BuildBuddy records `agentplane/system_tests/suites/test_vm.py` as a 4,036-byte input with digest `e40e75e0811911dd03cbccdb8670816a0949049ef3ae23a3a8907258acff9260`. The [BBR invocation](https://app.buildbuddy.io/invocation/c4d8ad7d-afbd-4d14-8a93-f54b968fce8f) used runner invocation [6aadb416-3f2d-401e-84d9-f6fd29ee6e6f](https://app.buildbuddy.io/invocation/6aadb416-3f2d-401e-84d9-f6fd29ee6e6f). Its exact command was:

```text
bbr test //agentplane/system_tests/suites:test_vm --runs_per_test=5 --flaky_test_attempts=1 --nocache_test_results --jobs=5
```

The test target ran once per requested repetition, with no flaky retry: runs 2–5 passed, and run 1 failed. Each execution starts three VMs, so 12 of the 15 guests in the batch completed the ordinary shutdown proof and powered down. Run 1's `vm-1` and `codex-oom` guests did the same. Only run 1's `vm-2` failed to complete shutdown, during cleanup after the test had already timed out waiting for that Sandbox to become Ready.

| Run | Test result | Guest shutdown evidence                                                                                                                                       |
| --: | :---------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------ |
|   1 | Failed      | `vm-1` and `codex-oom` completed proof and powered down; `vm-2` reached ACPI during readiness-failure cleanup, then was destroyed without proof or powerdown. |
|   2 | Passed      | All three guests completed proof, logged `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`, and powered down.                                                              |
|   3 | Passed      | All three guests completed proof, logged `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`, and powered down.                                                              |
|   4 | Passed      | All three guests completed proof, logged `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`, and powered down.                                                              |
|   5 | Passed      | All three guests completed proof, logged `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`, and powered down.                                                              |

## Run 1 vm-2

The failed guest was VMI `systest-vm-2`, UID `e8a82f46-cc88-43d5-a76a-1941a4505186`, in launcher Pod `virt-launcher-systest-vm-2-9j4bj`, UID `7c6aac8e-0b5c-42d3-bd30-570c2c6ae771`. The test's 300-second readiness wait expired while the VMI and launcher Pod were Running/Ready. The Sandbox's `EnvironmentRuntimeConnected` and `Ready` conditions remained False because the Runner Gateway had not received the runner's first state message. No session started for this guest.

Fixture teardown then sent graceful shutdown. The guest console recorded `acpid: poweroff` at 03:55:24.881 UTC and eight `AGENTPLANE_GUEST_SHUTDOWN_REQUESTED` markers. These count guest-observed request markers; they are not a count of host QMP commands. The guest serial has no shutdown-proof entry/result, no `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`, and no `reboot: Power down`. A watchdog snapshot at 2026-11-07 03:55:25.036 UTC showed PID 1 in D state, `wchan=__wait_rcu_gp`, with this kernel stack:

```text
__wait_rcu_gp
synchronize_rcu_normal.part.0
synchronize_rcu_normal
rcu_sync_enter
cgroup_update_dfl_csses
cgroup_subtree_control_write
kernfs_fop_write_iter
vfs_write
ksys_write
```

A second snapshot at 03:55:27.669 UTC showed the same wait path. The `/proc/uptime` values recorded by the snapshots were 48.59 and 51.29 seconds, respectively. The helper observer also reported that it did not see a shutdown-proof helper among the PID 1 children. This does not identify which shutdown job was pending or localize the missing proof to a particular unmount.

The host timeline confirms forced domain destruction. Host-QEMU snapshots at 03:55:30.030 and 03:55:33.987 UTC both saw the domain `running`. The UID-correlated `virt-handler` log then recorded `Grace period expired, killing deleted VirtualMachineInstance systest-vm-2` at 03:55:54.880 UTC and `Domain is in state Shutoff reason Destroyed` at 03:55:55.106 UTC. The launcher log recorded QEMU terminating on signal 15 from `virtqemud` and `shutting down, reason=destroyed`. A later host report at 03:56:07.425 found no matching QEMU process; its pod lookup returned NotFound. The Pod's captured `deletionTimestamp` is 03:56:24Z, after these events, so it is not used here as the initiation time for deletion.

The launcher log records that `/dev/kvm` was unavailable and that KubeVirt used software emulation. The host snapshot's QEMU command line explicitly contains `-accel tcg`. Both host samples report QEMU status `running`; both vCPUs report `HLT=1` in both register samples. IRQ counters advanced between samples (IRQ 4 from 128,483 to 128,541; IRQ 9 from 4 to 5). Across the 3.957-second interval, QEMU's CPU 0 TCG thread accumulated 129.1 ms runtime with no increase in its recorded run-queue delay; CPU 1 accumulated 22.8 ms runtime and 1.32 ms run-queue delay. These short samples show no material run-queue delay during that interval; they do not measure earlier periods.

The host scanner found three virtiofsd processes and nine threads, all correlated to the launcher Pod UID. The captured Pod status listed `virtiofs-state`, `virtiofs-agentplane-environment-runtime-trust`, and `virtiofs-agentplane-environment-runtime-tls` as Running restartable init containers. The process records have `container_name=unmapped`, so these samples cannot identify which PID serves the state tag. Process presence and Kubernetes Running status do not establish that a daemon was servicing requests. No pre-ACPI daemon sample was retained in this run.

## Timing and comparison limits

The console CRI record places `[44.811173] hrtimer: interrupt took 916876027 ns` at 03:51:55.558 UTC. The first shutdown snapshot records guest uptime 48.59 at 03:55:25.036 UTC: about 209.48 seconds of host console-ingestion time later, while the guest uptime counter is about 3.78 seconds beyond 44.811. Early boot records mark `tsc-early` unstable, switch to `refined-jiffies` at guest time 4.043 seconds, then to `acpi_pm` at 4.192 seconds; the log records another unstable-TSC check and switch back to `acpi_pm` around guest time 31.119 seconds. These are timestamp and clocksource observations, not proof of a specific clock fault. The guest uptime advances from 48.59 to 51.29 over the next 2.64 seconds of console time.

This is distinct from the standalone policy-off VMI failure, whose guest snapshots showed FUSE waits (`fuse_sync_fs` and `request_wait_answer`) while the native-sidecar policy was absent. Here the observed PID 1 wait is in RCU/cgroup reconfiguration, and the launcher Pod has the restartable virtiofs sidecar topology. That evidence does not reproduce the policy-off FUSE stack or its missing-policy setup. It also does not rule out every virtiofs-related mechanism: this run lacks a pre-ACPI sample and cannot map host virtiofsd PIDs to container names or show request progress.

The QMP collection is labeled read-only but may wake QEMU's main loop or vCPUs. Both host shutdown snapshots began after ACPI, at approximately five and nine seconds after the guest request markers, so they cannot establish pre-stop daemon state. This was one bounded five-run batch; it is not a recurrence-rate estimate or evidence that the shutdown issue is fixed.

## Retained evidence

[`full_suite_five_run/raw_artifacts.tar.gz`](full_suite_five_run/raw_artifacts.tar.gz) contains the unmodified run-1 vm-2 serial and timestamped console, both UID-correlated host shutdown snapshots, the later host report, and selected verbatim launcher/handler log lines. Verify the archive with its adjacent `.sha256` file; after extraction, `raw/SHA256SUMS` verifies each retained file. The candidate raw files were checked for private-key blocks, bearer strings, JWTs, GitHub-token patterns, and credential assignments before inclusion. Other guests are summarized above from their per-run serial artifacts and test logs; they are not included in the raw archive.
