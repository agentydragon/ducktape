# Native process supervision

COSI controllers own demand, readiness, restart backoff, quiesce and sealing. `Process` owns a
native launch's pipes, PID, exit result and cleanup barrier. The environment chooses its backend:
VMs require systemd; Pods use their existing subprocess supervisor. A manager failure never
switches a VM to the Pod backend.

## VM services

Runner calls systemd's [D-Bus manager API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)
directly over its root-only `/run/systemd/private` socket, without a bus-daemon dependency.
Every Claude process, shared Codex app-server, Codex exec-server, bootstrap/setup script
and credentialed workspace helper is a transient `Type=exec` service. No supervisor subprocess or
`systemd-run` helper is needed in a VM. Systemd creates the process with its resource and identity
policy already applied. Native units use `Restart=no`; COSI decides whether to launch a successor.

The hierarchy is shared by the NixOS guest and the system-test guest:

```text
agentplane.slice                         overall CPU, memory, swap and PID envelope
  agentplane-runner.service              trusted root controller
  agentplane-workloads.slice             aggregate harness/setup budget
    agentplane-native-<owner>-<UUID>.service
  agentplane-executors.slice             aggregate exec-server/tool budget
    agentplane-native-<owner>-<UUID>.service
```

The preflight helper applies the enclosing envelope before Runner starts. Runner configures slice
and per-service budgets through D-Bus. Services run as the configured unprivileged UID/GID, without
capabilities and with `NoNewPrivileges`. Their filesystem is read-only except `/state` and the
guest's shared `/tmp`; sharing temporary files preserves local exec-server transport. The trusted
Runner needs access to the system manager. The unprivileged harness identity has no authority to
start, stop or reconfigure system units.

Separate stdout/stderr and stdin pipes cross D-Bus as file descriptors, preserving native protocol
transport and stderr diagnostics. `ExecStartEx` disables manager argument expansion, preserving
literal dollar signs, specifiers and shell source. The configured environment values are passed
explicitly; systemd adds service metadata but does not inherit Runner-only variables. Login
environment defaults are disabled. Start waits for the manager's completed start job, so a rejected
`execve` is a launch failure. The unit reference retains exit information until cleanup completes.

## Completion and crash fencing

A launch allocates one UUID before sending `StartTransientUnit`. A lost reply or cancellation
triggers cleanup of that same identity. Runner does not create a second unit to retry an uncertain
launch. The native command's effects remain subject to journal reconciliation, never automatic
replay merely because its launch/control RPC failed.

Native units bind to and are ordered after `agentplane-runner.service`. Runner death therefore
stops them, allowing five seconds after `SIGTERM` before forced cleanup. `KillMode=control-group`
contains descendants even when they change process groups. Natural leader exit also triggers a
stop of the whole unit. `Wait` only completes after the unit is inactive/failed and its execution
domain is empty. A disconnected manager leaves cleanup blocked until it can be proven; process
exit alone does not authorize a successor or a journal seal.

The fixed state-owner lock excludes two live Runners. VM services do not inherit that lock.
After acquiring it, a replacement first discovers old units in the scope derived from its explicit
Sandbox UID and state directory, verifies their expected workload/executor slice, and fences them.
Only then can it open Session stores or journals and admit native execution. Other owner scopes
are untouched. This startup barrier also covers surviving setup and workspace helpers.

## Pods and tests

Pods retain the internal supervisor's parent-liveness pipe, inherited owner descriptor and process
group cleanup. Deliberately escaped process groups are outside that per-harness boundary. The Pod
exec-server's separate container provides its stronger execution-domain fence. This change adds
neither systemd nor privileges to containers.

The system-test guest boots real systemd and a system bus with the same static runner/slice units.
Before Runner starts, `runner_systemd_fixture` exercises credentials, separate streams, kernel
resource limits, lost/canceled launch replies, detached descendants, parent-unit death, ordinary
stop and scoped startup fencing. Existing VM scenarios then exercise storage, actual harnesses,
tool OOM and suspend/resume through the platform. The fixture's test wrapper and read-only observer
are test-only; they add no production control endpoint.

A future VM backend can implement the same launch/observe/stop/fence boundary with direct cgroups.
Running Runner as VM PID 1 would additionally require guest boot, reaping, signal handling and
recovery responsibilities currently supplied by systemd.
