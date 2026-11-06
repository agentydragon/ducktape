# VM shutdown stalls

## Observed failure

The VM suite's scenarios passed in [invocation
15e9310a-7352-56a3-87c6-322dfb3065c0](https://app.buildbuddy.io/invocation/15e9310a-7352-56a3-87c6-322dfb3065c0),
but the Claude guest never emitted `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`. KubeVirt destroyed
the running domain after its existing shutdown grace expired. The unchanged retry passed.

The guest's serial console reached `/state/workspaces` and `/state` unmount. Environment
Runtime had already stopped. The retained native journal ended before those final unmounts:
`systemd-journal-flush.service` ran `journalctl --smart-relinquish-var`, closing persistent
journals before `/state` unmounted. Journald then writes to `/run/log/journal`; those files
are in the guest's memory and disappear when the VM is deleted.

The exact pending shutdown job is still unknown as of the experiments below. A missing proof receipt does not by itself
identify the proof unit as the blocker.

## Collection constraints

- Retain the native journal files already written through `/state/systemd-journal` from the
  host, including unlinked inodes and in-place journal-header updates. Refresh snapshots once
  per second and drain again after teardown; an abruptly killed driver previously left only
  the initial snapshot.
- Follow only `/run/log/journal` to the serial console across the early flush and late
  relinquish. A default `journalctl --follow` also opens persistent files and could keep
  the tested `/state` mount busy.
- Keep the follower alive through the normal shutdown transaction using
  `DefaultDependencies=no`. Its direct tty output avoids a journal feedback loop and keeps
  a slow console writer outside journald's own event loop.
- Route PID 1's debug job transitions directly to the console during shutdown. This covers
  manager messages after journald and the follower exit.
- Sample PID 1's kernel wait channel, syscall and stack from an independent service, plus
  bounded pending-job queries. This survives a stalled manager event loop; console writes
  can still block, so absence of a sample is not proof that the service stopped.
- Retain QEMU's raw serial regular file from the launcher's `private` EmptyDir, independently
  of `virt-tail` forwarding to CRI. Retain virtiofs container logs alongside launcher logs.
- Preserve the existing proof's storage checks, ordering and shutdown grace.

Runtime stdout and stderr already use `journal+console` in `environment-runtime-guest.conf`.
The reproduced failed guest retained Runtime lifecycle records through its successful stop.
No additional Runtime logging configuration is necessary.

A second mount of the same `state` virtiofs tag is unsuitable for a causal reproduction:
Linux shares its superblock and connection between both mounts, moving final teardown past
the existing proof even if `/state` disappears from `findmnt`.

The source checks used systemd `v257.9`, matching the guest's `257.9-1~deb13u1`:
[journal flush unit](https://github.com/systemd/systemd/blob/v257.9/units/systemd-journal-flush.service),
[directory-only reader](https://github.com/systemd/systemd/blob/v257.9/src/libsystemd/sd-journal/sd-journal.c#L2392-L2410),
[runtime journal recreation](https://github.com/systemd/systemd/blob/v257.9/src/journal/journald-server.c#L1470-L1487).

## Reproduction experiments

1. [Three independent executions](https://app.buildbuddy.io/invocation/ac743375-b5cb-4109-aee8-67243e9ac78e)
   of `//agentplane/system_tests/suites:test_vm` on base `23547bc724049d8d144ba1b2861833b4e91899b6`.
   The only patch adds `systemd.log_level=debug systemd.log_target=console loglevel=8` to
   each VM's kernel arguments. Test-result caching and flaky retries are disabled.
2. [Runtime-journal console collection](https://app.buildbuddy.io/invocation/a189a207-ccce-41dd-9526-03d6a0064330):
   two independent VM executions plus guest-root layout checks. Replace the temporary
   kernel arguments with shutdown-only manager logging, and start a runtime-directory-only
   journal follower at boot. Native persistent journal retention is unchanged.

   Both VM executions and both layout checks passed. All six guests emitted the shutdown
   proof. The follower captured journald's late `Received client request to relinquish`
   record after the early volatile journal directory was removed and recreated.

3. [Independent shutdown watchdog](https://app.buildbuddy.io/invocation/3d2f8852-9353-456e-9959-418555c97da4):
   three VM executions and three collector unit-test executions. The collector checks passed.
   One VM execution passed. Another failed the Codex OOM scenario when its exec-server
   transport disconnected; all three guests still emitted proof and powered down. The third
   wedged during Claude guest boot, before Runtime or ACPI handling started: Linux reported
   repeated RCU stalls, CPU 1 in `pv_native_safe_halt`, zero timer ticks on that CPU and
   `Possible timer handling issue on cpu=1`. Its other two guests powered down. That is a
   separate early-boot failure, not a reproduction of the observed shutdown phase.
4. [Raw serial retention plus boot-time manager debugging](https://app.buildbuddy.io/invocation/b1161c32-b13a-4109-8ee5-839a3a42120b):
   three VM executions with the watchdog, virtiofs logs and independent raw serial capture.
   All three VM executions passed (508.43, 520.75 and 545.20 seconds), as did all three
   collector checks. Raw serial files and CRI logs contained proof and final powerdown for
   every guest; all 27 virtiofs container logs were retained. The temporary boot debug
   arguments are excluded from the proposed diagnostics.
5. [Passive watchdog](https://app.buildbuddy.io/invocation/3a213ee3-78ba-4527-9c76-d21778fb2540):
   five VM executions on diagnostic commit `67f41df08af87a53cc3bd12910152cb5e8d8363c` with
   manager log-level/target changes and all pending-job RPCs removed. Kernel arguments
   remain ordinary. The independent sampler adds PID 1 CPU counters, descriptor mappings
   and a process listing. This tests whether extra manager traffic or debug output changes
   reproduction. Running.

Experiment 1 did reproduce a shutdown failure in its second execution: the Codex OOM guest
stopped Runtime at 18:55:50 UTC, unmounted `/state/workspaces` by 18:55:52 and stopped journal
flush at 18:55:53. PID 1's last retained record was `mount-monitor-dispatch` leaving its rate
limit. It never launched the `/state` unmount helper in the retained log. KubeVirt forced
QEMU down at 18:56:17. The virtiofs daemons exited after that; the console sidecar remained
alive until 18:56:25. The first execution instead failed baseline namespace cleanup after
all guests had emitted proof; the third passed.

The last mount-monitor message precedes its expiry callback in systemd, so it does not
identify a blocked syscall. The callback itself requeues mount jobs. PID 1 also performs
synchronous console writes: boot-time debug output may amplify serial backpressure. The
watchdog's `/proc/1/syscall` and stack should distinguish that from mountinfo reads, an
unmount helper waiting on FUSE, or a userspace manager loop. Pending-job RPC failure alone
is insufficient because the manager bus closes during healthy final shutdown.

[KubeVirt v1.8.2 serial file creation](https://github.com/kubevirt/kubevirt/blob/3203e9d1ce77af32f6a9ce72b9f954830666f72c/pkg/virt-launcher/monitor.go#L70-L88)
and its [file-tail sidecar](https://github.com/kubevirt/kubevirt/blob/3203e9d1ce77af32f6a9ce72b9f954830666f72c/cmd/virt-tail/main.go#L60-L138)
rule out a stopped tail reader imposing pipe backpressure on QEMU: the producer writes a
regular file. This does not exclude guest UART or host filesystem I/O blocking.

The working-tree patches for these runs were applied over
`23547bc724049d8d144ba1b2861833b4e91899b6`; these are experiment invocations, not CI proof
for a committed diagnostic head. No successful retry establishes a functional repair.

## Upstream lead checked

[systemd #30573](https://github.com/systemd/systemd/issues/30573) ends a log gap at a similar
mount-monitor message, but it has no identified root-cause fix. Its linked
[mitigation](https://github.com/systemd/systemd/commit/37ce3fd2b7dd8f81f6f4bca2003961a92b2963dc)
changes handling of an asynchronous bus-match timeout; it does not establish a synchronous
PID 1 block. The v257.9 call starts in D-Bus service setup, outside the mount callback. Our
failed guest had already stopped `dbus.service` and closed `dbus.socket` approximately two
seconds before its last record. The superficial log match does not justify applying that
mitigation as a shutdown repair.
