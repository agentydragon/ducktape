# Upstream analogues for the guest RCU wait

This note compares upstream reports with the observed wait in the [five-run full-suite batch](five_run_full_suite_rcu.md). It does not identify the cause of the Agentplane failure.

## Observed guest and emulator

The failed `vm-2` in [BuildBuddy invocation `c4d8ad7d`](https://app.buildbuddy.io/invocation/c4d8ad7d-afbd-4d14-8a93-f54b968fce8f) did not reach Sandbox Ready. During readiness cleanup it received ACPI; PID 1 was sampled twice in D state at `__wait_rcu_gp`, through `synchronize_rcu_normal` and `rcu_sync_enter` to `cgroup_subtree_control_write`. No shutdown proof or `reboot: Power down` followed, and KubeVirt destroyed the domain after its grace period. These samples show a synchronous RCU grace-period waiter. They do not show an RCU CPU-stall warning or identify the blocked shutdown job.

The run used a two-vCPU x86 guest with QEMU `-accel tcg`. Its launcher reported QEMU `10.1.0qemu-kvm-10.1.0-10.el9`; KubeVirt reported launcher image `quay.io/kubevirt/virt-launcher:v1.8.2`. The [KubeVirt v1.8.2 RPM dependency script](https://github.com/kubevirt/kubevirt/blob/v1.8.2/hack/rpm-deps.sh#L14-L34) pins QEMU `17:10.1.0-10.el9` for its CentOS Stream 9 build. The runtime version banner does not identify the exact RPM build ID or prove its complete downstream patch set.

The guest identified itself as Debian kernel `6.12.115-2`. Its early boot log marked `tsc-early` unstable, selected `refined-jiffies` and then `acpi_pm`, and later recorded another unstable-TSC check. The console also reported a 916,876,027 ns hrtimer interrupt. There is a roughly 209-second gap between that CRI timestamp and the first shutdown snapshot, while guest uptime differs by about 3.8 seconds. Console buffering and clock-source changes prevent treating that comparison as proof that guest time stopped. Two later host snapshots saw both vCPUs halted and QEMU running; interrupt counters advanced, and QEMU threads accrued runtime. Those short post-ACPI samples do not establish timer or scheduling progress before the snapshots.

## Linux fanotify report: same waiter stack, different trigger conditions

The December 2024 [Linux filesystem mailing-list report](https://www.spinics.net/lists/linux-fsdevel/msg288927.html) is a strong stack analogue. It records a `systemd` child (PID 1293, parent PID 1) blocked in the same path:

```text
__wait_rcu_gp
synchronize_rcu_normal
rcu_sync_enter
percpu_down_write
cgroup_update_dfl_csses
cgroup_subtree_control_write
```

That report separately showed PID 1 waiting in `cgroup_rmdir` on an rt-mutex. The author bisected the hang to `0790303ec869` (`fsnotify: generate pre-content permission event on page fault`) in `linux-next-20241206`, and reported that its failure occurred with `CONFIG_FANOTIFY_ACCESS_PERMISSIONS` disabled. The discussion traced the disabled-config case to fsnotify mode bookkeeping; a partial revert removed the problematic page-fault path for the reporter, and the follow-up proposed fixing the disabled-config mode handling. The reporter said enabling the config also avoided the failure. See the [follow-up fix discussion](https://lkml.iu.edu/hypermail/linux/kernel/2412.1/01195.html) and [config-guard discussion](https://lkml.iu.edu/hypermail/linux/kernel/2412.1/03207.html).

The observed guest does not match those reported preconditions. Debian's exact [`debian/6.12.115-2` kernel configuration](https://salsa.debian.org/kernel-team/linux/-/blob/debian/6.12.115-2/debian/config/config#L6159) sets `CONFIG_FANOTIFY_ACCESS_PERMISSIONS=y`; the cloud config fragments do not override it. Also, upstream [Linux v6.12 `mm/filemap.c`](https://github.com/torvalds/linux/blob/v6.12/mm/filemap.c) does not contain the page-fault `fsnotify_file_area_perm()` call introduced by the reported 6.13-next change. This rules out the report's stated disabled-config trigger as an explanation supported by the available guest evidence. It does not rule out another kernel path that can leave `synchronize_rcu()` waiting.

## QEMU TCG timer and RCU reports

QEMU issues [#1846](https://gitlab.com/qemu-project/qemu/-/issues/1846) and [#1864](https://gitlab.com/qemu-project/qemu/-/issues/1864) show that software-emulated SMP guests have had timer/RCU boot stalls. Issue #1846 reports intermittent x86_64 Linux-host QEMU 8.1 `q35`, two-vCPU TCG hangs after clock initialization, with an RCU stall report and CPU 0 in `amd_e400_idle`. Issue #1864 reports an x86_64 guest under QEMU 8.1.0 TCG on an ARM Mac host; its log says the RCU GP timer wakeup did not happen, reports a possible timer-handling issue on CPU 0, and shows that CPU halted. These are boot-time RCU stall warnings, unlike this run's shutdown-time PID 1 synchronous RCU wait.

The QEMU maintainer thread identifies commit [`0d58c660689f`](https://lists.gnu.org/archive/html/qemu-devel/2023-09/msg05276.html) (`softmmu: Use async_run_on_cpu in tcg_commit`) as the fix for #1864. The reports establish that TCG/SMP timer failure is possible, but do not show that this fixed QEMU 8.1 behavior exists in the observed QEMU 10.1 runtime or explains its cgroup wait.

A separate upstream report, QEMU [#3040](https://gitlab.com/qemu-project/qemu/-/issues/3040), concerns the `iotlb_to_section` assertion during PCI enumeration; Debian tracked the same failure in [bug 1106792](https://bugs.debian.org/1106792). Upstream commit [`2865bf1c`](https://qemu.googlesource.com/qemu/+/2865bf1c5795371ef441e9029bc22566ccff8299) fixes a cached `AddressSpaceDispatch` use-after-free; its commit is an ancestor of the upstream QEMU [`v10.1.0` tag](https://github.com/qemu/qemu/tree/v10.1.0). This is relevant to the earlier PCI-enumeration assertion observed in a [separate two-vCPU diagnostic](README.md#uncached-control-after-observer-merges), not to the late RCU wait after ACPI in this batch. It also demonstrates that fixing one TCG SMP race did not eliminate every such race.

QEMU [#2381](https://gitlab.com/qemu-project/qemu/-/issues/2381) is a feature request for a stable, known-frequency x86 TSC under TCG on non-x86 hosts; it notes that Linux boot-time TSC calibration does not work reliably in that setting. It is only an analogue if the relevant emulator host is non-x86, which the retained run evidence does not establish. The guest's clock-source messages and hrtimer record justify retaining timing as an open observation, not assigning it as the cause.

## Boundary

The upstream material establishes two things: a Linux `synchronize_rcu()` waiter can occur at this exact cgroup write path under a specific fanotify configuration bug, and QEMU TCG/SMP has had separate timer-related RCU boot failures. The current run has the same Linux waiter stack but not the fanotify report's configuration condition; it uses a newer QEMU version than the cited 8.1 timer reports; and its short host samples do not expose the preceding guest's timer/RCU progress. The retained evidence therefore narrows the comparisons but does not distinguish a guest-kernel cgroup/RCU problem from a lower-level scheduling or timing problem.
