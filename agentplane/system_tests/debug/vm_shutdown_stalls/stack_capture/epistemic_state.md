# Guest stack capture during VM shutdown stalls

Assessment on 2026-11-07. Goal: obtain enough guest and host state during the
missing-shutdown-proof interval to discriminate among possible blockers, without
assuming that an RCU wait explains the historical failures.

## Evidence and limits

- The historical shutdown-stall VMs are gone, and their retained logs did not
  include PID 1's kernel stack or QEMU CPU state in the quiet interval. See the
  [historical census](../historical_census.md).
- A separate [pre-Ready failure](https://app.buildbuddy.io/invocation/c4d8ad7d-afbd-4d14-8a93-f54b968fce8f)
  captured PID 1 waiting in cgroup/RCU code. Its connection to late shutdown
  stalls is unknown; see [the analysis](../five_run_full_suite_rcu.md).
- Current CI has an ACPI-triggered guest watchdog that samples PID 1 and, every
  tenth loop iteration, selected shutdown helpers. It also retains raw guest
  serial output and collects delayed QEMU thread/register snapshots on the host.
  The loop has variable-duration operations, so ten iterations is not a fixed
  ten seconds. Guest scheduling, a kernel lockup, blocked console output, or
  truncation can prevent useful guest output. Host thread state is not a guest
  kernel task stack, and QMP inspection can perturb the VM.
- The lightweight [direct-VMI policy A/B](../standalone_kubevirt/README.md#policy-ab-outcome)
  produced three orderly policy-on shutdowns and three deliberately induced
  policy-off failures with shutdown helpers in FUSE waits. This is a useful
  positive control for testing capture, not a reproduction of the historical
  CI failure. The direct VMI lacks the full application and sidecar topology.
- The [pinned guest kernel package](../../../../../devinfra/bazel/artifacts.MODULE.bazel)
  is Debian `6.12.115-2` (`6.12.115+deb13.1-cloud-amd64`); the package downloaded
  from its pinned snapshot URL matched SHA-256
  `c26573a3b5dca6bd3bf2b06a13b81cbb845c04eb6e35d7ad57f4b885746153e4`.
  Its `/boot/config-6.12.115+deb13.1-cloud-amd64` has
  `CONFIG_MAGIC_SYSRQ=y`, `CONFIG_MAGIC_SYSRQ_SERIAL=y`,
  `CONFIG_MAGIC_SYSRQ_DEFAULT_ENABLE=0x01b6`, and `CONFIG_KALLSYMS=y`.
  It also has `CONFIG_DEBUG_INFO=y`, which does not establish that the matching
  unstripped `vmlinux` is available for offline analysis.

## Options

1. **Existing watchdog:** already deployed and inexpensive to reuse. It supplies
   targeted snapshots, but scans only selected tasks and requires guest and
   console progress. Preserve it as a baseline.
2. **One-shot guest SysRq:** Linux `w` reports tasks in uninterruptible sleep;
   `l` asks for active CPU backtraces. `w` will miss an interruptible or epoll
   wait, so it complements rather than replaces PID 1 sampling. A privileged
   write to `/proc/sysrq-trigger` bypasses the keyboard/serial enable mask.
   Kernel messages must reach and survive the retained serial path. See the
   [Linux 6.12 SysRq documentation](https://www.kernel.org/doc/html/v6.12/admin-guide/sysrq.html).
   Printing a large task dump can itself perturb shutdown, and a userspace
   timeout does not necessarily bound time spent in a kernel print operation.
3. **Host-initiated serial SysRq:** potentially useful if an independent guest
   worker cannot run, but KubeVirt's ability to forward a serial BREAK followed
   by the command is untested here. The compiled default `0x01b6` excludes dump
   bit `0x8`, so serial commands `w` and `l` would be disabled if that default
   remains in effect. The running guest's `/proc/sys/kernel/sysrq` was not
   measured. Verify forwarding and runtime mask in an isolated guest before
   relying on this path; do not change cluster-wide configuration.
4. **Guest memory dump:** a possible last resort if the guest cannot print but
   QEMU remains responsive. [QEMU's QMP documentation](https://www.qemu.org/docs/master/interop/qemu-qmp-ref.html#command-dump-guest-memory)
   describes a potentially lengthy dump; the exact supported path for our
   KubeVirt/QEMU and its completion within shutdown grace are unverified. It
   needs storage, compatible analysis tooling and matching kernel symbols, and
   can expose all guest memory, including secrets. Treat the dump as sensitive.

## Recommended next experiment

Use the direct-VMI harness first, retaining the current watchdog and host
observations. Add an **opt-in**, independently scheduled guest diagnostic that
attempts a single privileged `w` dump after proof remains absent during
shutdown; evaluate `l` separately if useful. Record trigger, completion or
failure, timestamps, VMI identity, and whether serial output is complete. A
tentative 10–15 second delay must be checked against actual guest and host
timing, proof receipt, and the direct VMI's 30-second termination grace. It is
an experiment parameter, not a verified safe production threshold. Skip the
trigger if proof has arrived. Do not assume a guest timer will run during a
guest-wide stall.

Run a policy-on control and the intentionally policy-off FUSE-stall control.
Success means: the normal guest still shuts down without triggering the dump;
the stalled guest retains interpretable task stacks and clear attempt/result
markers before domain destruction; and the output can be correlated to the
same VMI and compared with the existing watchdog and host snapshots. Measure
output volume and any delay introduced. A missing or partial dump is itself a
result, not proof of a particular blocker. Then evaluate serial BREAK and a
memory dump only if the first approach leaves a material visibility gap.

This assessment inspected configuration and existing evidence; it did not
trigger SysRq, verify BREAK forwarding, collect a memory dump, or establish a
new reproduction of the original flake. FUSE waits, cgroup/RCU waits, systemd
userspace waits, and lost observer/console progress remain distinct possibilities.
