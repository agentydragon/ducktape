# Boot monitor notification barrier, 2026-11-02

The OOM guest in [candidate2](https://app.buildbuddy.io/invocation/1ef7a3a3-bbde-4a7d-9df2-e2d3eed6ea55)
completed its scenario and shutdown, but teardown correctly rejected its missing
`AGENTPLANE_UDEV_MONITOR_READY` serial receipt. The full native journal records
PID1 starting the monitor successfully, followed by `systemd-notify` reporting
`Failed to invoke barrier: Connection timed out`. Under `set -e`, that failure
terminated the wrapper before it printed the receipt. The FIFO also stopped being
drained while the command waited. The boot reporter independently completed its
required sections; this was a monitor notification failure.

Use `systemd-notify --no-block` after the existing bound-listener header predicate.
The guest's systemd 257.9 [manual](https://github.com/systemd/systemd/blob/v257.9/man/systemd-notify.xml)
documents this case: a manager-spawned parent with sufficient privileges can send
with its own credentials without waiting for the short-lived helper's barrier.
The [implementation](https://github.com/systemd/systemd/blob/v257.9/src/notify/notify.c)
uses the parent credentials and still rejects notification send failures.

Here the root shell is PID1's tracked main process and remains alive draining the
monitor FIFO. Both netlink listener headers precede notification. `Type=notify`
and the ordering before `systemd-udev-trigger.service` still make PID1 gate
coldplug on readiness; neither the header predicate nor the collector assertions
change. Re-evaluate attribution if the unit gains an unprivileged user or loses
its persistent parent process.
