# VM shutdown stalls

For a smaller direct-QEMU software-emulation experiment and its retained logs, see
[tcg_reproduction.md](tcg_reproduction.md). It does not run the full KubeVirt stack.
[libvirt_monitor/README.md](libvirt_monitor/README.md) describes opt-in per-VM QMP logs.

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
   reproduction. Three executions passed. Two hit the existing 600-second pytest limit;
   all 15 guests emitted proof and powered down. In run 2 the timer included 378 seconds
   of stack setup, then guest-ready waits of 157–200 seconds. It fired while sessions and
   initial turns were still progressing. A provisioner API timeout surfaced on teardown;
   its original time is not recorded, so it cannot be assigned as the cause of the cutoff.

   Two run-1 guests produced no watchdog sample: the bounded start RPC could expire before
   starting the service. The proposed collector now starts the observer at boot, blocked
   on a FIFO. ACPI wakes it before changing manager logging. Keeping its FIFO reader open
   also accepts repeated power-button notifications without needing a responsive PID 1.

6. [Host QEMU inspection](https://app.buildbuddy.io/invocation/39f1fe6c-190f-4c91-a58c-e108c93ba9f3):
   three VM executions using source `b0f6c79819206746b3401a45bea7b2a3fcaf552f`. Restore
   original ACPI handling and disable the new journal-console and watchdog services;
   preserve host-side journal, serial and virtiofs retention. A host thread saves QEMU
   process/thread kernel state, then reads status, vCPU registers and APIC/IRQ state using
   libvirt's QMP interface. Boot samples validate the probes; shutdown samples begin five
   seconds after ACPI if proof is absent. QMP can briefly synchronize with vCPUs, so these
   probes are not assumed to be free of perturbation.

   All three executions passed (409.50, 391.79 and 364.75 seconds), and all nine guests
   emitted proof and powered down. Boot samples retained host thread stacks and both
   vCPUs' registers; the session libvirt socket worked without a privileged URI fallback.
   No guest remained without proof long enough to trigger a shutdown sample. This QEMU
   supports the register, APIC and IRQ probes but has no `info clocks` command.

7. [Boot debug with host probes](https://app.buildbuddy.io/invocation/6e2cb691-5550-4ee0-98e8-28a65a3c8bc7):
   ten requested executions using `5ad76994400bcf6705f5aa98428debb683e1f0bd`. Keep the
   original ACPI path and disabled new guest services from experiment 6; restore experiment
   1's boot debug arguments. Healthy shutdowns exercised register and APIC probes before
   the domain disappeared, including a halted CPU 1 that subsequently powered down.
   A halted vCPU alone therefore does not identify a wedge. One execution failed its first
   supervision fixture attempt, passed after guest service restart, then surfaced a
   provisioner API timeout at teardown; all three guests still emitted proof and powerdown.
   Nine executions passed and this one failed; all 30 guests emitted proof and powerdown.
   Three infrastructure attempts failed separately and were retried, encountering the
   execution-VM failures recorded below.
8. [Delayed QMP control](https://app.buildbuddy.io/invocation/8f11029e-2525-49a6-b764-902cdad946dd):
   five requested executions using `a332a112481b226b0951a9fbc63ecabba864177a`. Remove all
   boot monitor queries and collect passive host thread state at five seconds after ACPI,
   then at four-second intervals. Delay QMP until at least 15 seconds without proof. This
   separates passive observations from monitor queries that may wake QEMU/vCPUs. Completed
   healthy shutdown samples reached proof without issuing any monitor query.

   Four executions passed. Run 5 hit the existing 600-second pytest limit, then timed out
   waiting for Sandbox cleanup. Its last live snapshot remained `Quiescing`, waiting to
   retry runner quiescence. VM 1 never recorded an ACPI request, proof or powerdown; its
   Sandbox and loop device remained, so the fixture correctly refused recycling. The
   other two guests emitted proof and powerdown. All 14 guests that received ACPI across
   these five executions completed shutdown, and no QMP query ran. This failure is before
   guest shutdown, not a reproduction of the original post-ACPI quiet interval.

   VM 1 had passed supervision and become ready by 20:43:58 UTC, then completed both
   session setups by 20:44:20. Its command was admitted at 20:44:20.677; the last native
   session event was harness initialization at 20:44:23.443. Runtime's connected condition
   became false at 20:45:14, and Gateway logs retained repeated TLS handshake timeouts.
   Suspend and deletion were accepted at 20:46:10–11, but six runner-quiescence RPCs through
   20:51:11 returned `Unavailable`. The controller therefore retained the unquiesced VMI.
   The guest journal ends at 20:44:38 and serial at 20:44:56; neither identifies why control
   traffic stopped responding. The final VM report now also captures host/QEMU state for
   launchers without proof, so this earlier gate does not have to reach ACPI for collection.

The journal/serial diagnostics merged in [PR #14629](https://github.com/agentydragon/ducktape/pull/14629).
The FIFO observer merged in [PR #14633](https://github.com/agentydragon/ducktape/pull/14633)
at head `78775a077cd7c6fd93f13373b6e43cd47407b0db`. Its CI synthetic merge commit
`0b601a7f201c9d28fa3fe465a2eb79c96548c10` has the same tree as that head. All three guests
recorded observer start, FIFO notification, PID 1 samples, shutdown proof and powerdown;
all three retained native journals passed `journalctl --verify`.

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

Experiments 1–4 used working-tree patches over `23547bc724049d8d144ba1b2861833b4e91899b6`;
experiments 5–6 identify their source separately above. Experiment invocations are distinct
from the diagnostic PR's CI. No successful retry establishes a functional repair.

## Upstream lead checked

[systemd #30573](https://github.com/systemd/systemd/issues/30573) ends a log gap at a similar
mount-monitor message, but it has no identified root-cause fix. Its linked
[mitigation](https://github.com/systemd/systemd/commit/37ce3fd2b7dd8f81f6f4bca2003961a92b2963dc)
changes handling of an asynchronous bus-match timeout; it does not establish a synchronous
PID 1 block. The v257.9 call starts in D-Bus service setup, outside the mount callback. Our
failed guest had already stopped `dbus.service` and closed `dbus.socket` approximately two
seconds before its last record. The superficial log match does not justify applying that
mitigation as a shutdown repair.

## RBE execution-VM failures

Experiment 7 also encountered failures of the outer BuildBuddy Firecracker VM, whose
Linux 5.15.0 kernel hosts k3s and QEMU. These are separate from the inner guest's shutdown:

- Run 4's original attempt panicked at kernel uptime 436 seconds on CPU 9 in scheduler
  softirq processing, at `update_blocked_averages+0x273/0x740`, dereferencing a noncanonical
  pointer. Its REAPI status was `UNAVAILABLE`, with the Firecracker socket gone.
- Run 1's original attempt lost its VM health check. Its retained tail contains repeated
  balloon-stat descriptor errors but no panic record.
- Run 1's first infrastructure retry panicked at uptime 319 seconds on CPU 8, at
  `update_load_avg+0x92/0x5c0`, called from `update_blocked_averages+0x577/0x740`.
  It also dereferenced a noncanonical pointer. Both panic banners identify the same
  Linux 5.15.0 build from October 15, 2026.

The two panic instruction sequences are consistent with a corrupted scheduler entity's
child runqueue pointer in upstream [Linux v5.15 scheduler source](https://github.com/torvalds/linux/blob/v5.15/kernel/sched/fair.c).
They do not identify the corruption writer. Exact mapping requires the deployed kernel's
binary/checksum and debug information; identifying the writer additionally requires a
vmcore or equivalent provider-side evidence. No kernel repair follows from these traces.

Those attempts returned empty test output trees, but their REAPI `serverLogs` retain
`vm_log_tail.txt`; recover it from `bb execution get <executionId> --output=json`. The
artifact must be distinguished from the inner guest's retained serial file. Infrastructure
retries are also distinct from pytest/Bazel flaky-test retries.

The original failed invocation and experiment 1 run 2 also retain outer VM log tails.
Neither tail contains a kernel panic, and their recorded pause/resume events occur after
KubeVirt forcibly stopped QEMU. Those logs do not establish a link from the provider panics
to the shutdown stalls, or exclude host scheduling delays.

The historical shutdown failures have no retained PID 1 syscall/kernel-stack or QEMU CPU
samples from the quiet interval. Their exact blocker remains beyond that evidence. The
new guest observer and host snapshots are intended to cross that boundary on recurrence;
passing experiments remain validation of collection rather than a shutdown repair.

## Uncached control after observer merges

On devel commit `57305db4d39334d78cfdaec858791dda69325a4a`,
[six uncached full-suite runs](https://app.buildbuddy.io/invocation/f7a6fc69-875c-4cf1-994e-2fb36e1ddda5)
with the merged guest and host observers produced five passes and one failure. The failure
does **not** reproduce the late-shutdown stall: the original `systest-vm-2` launcher aborted
during guest boot at 21:34:17 UTC with QEMU 10.1.0's `iotlb_to_section` assertion
`section_index < d->map.sections_nb`. Its raw serial ended during PCI enumeration, before
an ACPI shutdown request. A replacement incarnation completed the scenario, but the suite
correctly rejected the original launcher's missing shutdown proof. Its report-phase host
sample found the launcher already gone; that is not a shutdown-phase QEMU sample. This is a
second observation of the boot assertion described in [the CPU comparison](../vm_cpu_budgets.md),
now with two vCPUs. It does not establish a shared cause with the late-shutdown stall.
The retained original launcher's [raw serial](run6-serial.log.gz), [CRI console](run6-console.log.gz),
[launcher log](run6-launcher.log.gz), and [report-phase host sample](run6-report.log.gz) are
compressed alongside this report without changing their bytes; the invocation also retains the
replacement and other guests' artifacts.

One execution also suffered a separate BuildBuddy infrastructure failure and was retried:
the outer Firecracker VM's Linux 5.15.0 kernel panicked at `update_blocked_averages+0x273/0x740`
on a noncanonical address. Its execution is
`/uploads/042cc757-65fd-418a-9e2f-4257de885375/blobs/497c5e7828dcf152cac098248b56b7ef1304146918065aacdd0f6c1118a5ad73/1852`;
`bb execution get` exposes its `vm_log_tail.txt` server log. This matches the _location_ of
one previously recorded provider panic, not proof of its corruption writer or of a relation
to the guest shutdown failures. The six test results include the infrastructure retry.

The earlier passing [devel CI invocation](https://app.buildbuddy.io/invocation/56121559-725f-58e0-ad04-ef20045b9f46)
provides a healthy comparison for the guest and QEMU observations. The Codex OOM guest's raw
serial shows `mount-monitor-dispatch` entering and leaving rate limiting, followed by the
shutdown proof and powerdown. Its host snapshot at 21:16:10.028 UTC captured a running QEMU
domain shortly before proof; one vCPU was halted in the register sample. These individual
observations therefore do not establish a stuck manager or vCPU.
