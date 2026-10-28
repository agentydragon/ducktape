# Native process supervision

COSI controllers own demand, readiness, restart backoff, quiesce and sealing. They schedule
asynchronous operations whose completion wakes reconciliation. Harness and server code builds
`supervision.ProcessConfig` values containing command, working directory and explicit environment.
The reusable process-lifetime controller, its resources and `Host` registry live in `supervision/`.
Runner injects the lifecycle context, state, namespace, shared worker tracking and error callback into
the host, then registers `Host.Controller()` in the same COSI engine as its other lifecycle
controllers. The host does not start a second runtime. Runner's `launchProcess` adapter in
`lifecycle_runtime.go` supplies the admitted owner and generation to `Host.Launch`;
`supervision.ManagedProcess` publishes
process intent and exposes streams and waits through that controller.

Runner selects a `supervision.Supervisor` during startup while holding state ownership, before
opening Session stores. Each supervisor creates `Process` handles through a private backend
interface. `Process` owns the common streams, PID and completion result; backend implementations
own launch, observation and execution-domain cleanup. `Start` bounds startup independently of
process lifetime. `Stop` requests graceful shutdown and cleanup; `Wait` returns only after exit
and cleanup. A canceled wait cancels observation, not execution. `ManagedProcess` places the COSI
intent and cleanup barrier around that backend process, while callers retain stream access.

VMs prepare systemd supervision, including slice budgets and prior-owner cleanup. Pods use the
subprocess supervisor with its inherited state-owner descriptor. Backend choice stays out of
controllers and per-launch specifications. A manager failure never switches a VM to the Pod
backend. The Pod exec-server container retains its separate control-socket ownership protocol.
The generic COSI process controller is part of the shared `supervision` package and runs in Runner's
existing lifecycle engine. Every Runner-managed
native launch uses `ProcessDesiredStates`, `ProcessStatuses` and a finalizer per attempt: Codex
exec-server, shared Codex app-server, per-Session harnesses, bootstrap/setup scripts and credentialed
workspace helpers. Owning controllers keep readiness, restart and journal policy. Protocol and
script workers use `supervision.ManagedProcess` to publish intent, consume streams and await the cleanup barrier;
they never invoke backend start/stop themselves. Canceling a wait leaves execution owned until an
explicit stop. Canceling startup instead tears down its intent and joins any late launch.
Process resources contain lifecycle values; launch callbacks and native handles stay in the host
registry. Teardown cancellation joins an in-flight start and fences any instance it returns before
releasing the finalizer. A completed attempt never relaunches during reconciliation.
Runner checks every remaining process intent before sealing or closing, so a failed fence cannot
be hidden by a protocol reader reporting the leader's exit. The standalone Pod executor still
supervises its own container-local child; Runner owns it through the container-ownership adapter.

## VM services

Runner uses [go-systemd's D-Bus bindings](https://github.com/coreos/go-systemd/tree/v22.7.0/dbus)
over systemd's root-only `/run/systemd/private` socket, without a bus-daemon dependency. The
bindings own connection setup, job completion tracking and property-change subscriptions.
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
`execve` is a launch failure. `RemainAfterExit=yes` and `CollectMode=inactive` retain successful
and failed exit information until Runner reads it and stops the unit. Ordinary stops signal the
unit first, preserving that evidence before cleanup.
If an external stop collects the unit before observation, Runner reports process loss (code 125)
and checks the saved kernel domain before completing cleanup.

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

Each native launch also requires a marker for its Runner service invocation. Systemd removes the
service's runtime directory before restarting Runner. A start request still queued on an old
connection therefore fails its condition if it arrives after replacement, rather than starting
work after the replacement's inventory scan. A skipped start condition never counts as native exec.

## Pods and tests

Pods retain the internal supervisor's parent-liveness pipe, inherited owner descriptor and process
group cleanup. Deliberately escaped process groups are outside that per-harness boundary. The Pod
exec-server's separate container provides its stronger execution-domain fence. This change adds
neither systemd nor privileges to containers.

### PID 1 and the outer fence

The production Pod template executes Runner directly as container PID 1, with a private PID
namespace. That container boundary kills even descendants that created another session or closed
the inherited state-owner descriptor. The internal supervisor still owns each native leader's
`exec.Cmd.Wait` and retains a trusted copy of the descriptor through group cleanup. Ordinary
completion and `Stop` fence the native group; they do not fence escaped groups. Escaped descendants
can also retain stdout/stderr pipes, so a whole-container exit must not wait indefinitely for their
readers. Non-PID1 integration fixtures rely on the internal supervisor's parent-liveness protocol.

The namespace fence is complete when the outer runtime has observed the container's init exit.
Successful acquisition of the state lock alone does not prove that boundary. Linux's
[`do_exit`](https://github.com/torvalds/linux/blob/v6.12/kernel/exit.c) closes the exiting task's files
before `exit_notify` reaches namespace teardown;
[`zap_pid_ns_processes`](https://github.com/torvalds/linux/blob/v6.12/kernel/pid_namespace.c) then kills
and joins the remaining namespace tasks. An untrusted native may close every inherited descriptor.
Consequently, removing the trusted per-native supervisor needs an explicit startup contract that
proves the prior container's runtime exit before opening stores or admitting native work. Inferring
that contract from `os.Getpid() == 1` would not establish it.

The Sandbox Controller waits for the previous Pod's deletion event before creating its successor
(`converge` in `sandbox_controller/controller.go`). Normal kubelet restarts and graceful Pod deletion
have an outer runtime boundary. An API deletion event after force deletion or loss of a node is not
evidence that the old container stopped. The checked-in node-local storage and fixed inode lock
remain part of the supported contract; neither a Pod UID nor PVC access mode replaces runtime
fencing. Production direct spawning remains gated on specifying and proving this replacement
boundary, explicit backend selection with PID1 validation, and one reaping owner for direct children
and adopted orphans. The systemd backend's startup inventory fence is independent.

`//agentplane/runner:test_pid1_container` runs the current production subprocess backend as true PID 1
in a real Docker container. Its test-only `runner_pid1_fixture` closes the native's inherited owner
descriptor, starts TERM-resistant same-group and escaped descendants, and covers native exit,
Runner TERM and abrupt Runner SIGKILL. It checks ownership while the trusted supervisor is live,
then waits for outer runtime exit, observes descendant death and reacquires the lock before
restarting the same container. JSON evidence and container logs go to undeclared test outputs.
This proves that observed runtime exit is a usable replacement boundary; it does not prove that
lock availability or a Kubernetes API deletion establishes it. It adds no production endpoint or
service and does not select a direct-spawn backend.

The system-test guest boots real systemd and a system bus with the same static runner/slice units.
Before Runner starts, `//agentplane/runner/supervision:runner_systemd_fixture` exercises credentials, separate streams, kernel
resource limits, lost/canceled launch replies, detached descendants, parent-unit death, ordinary
stop and scoped startup fencing. Existing VM scenarios then exercise storage, actual harnesses,
tool OOM and suspend/resume through the platform. The fixture's test wrapper and read-only observer
are test-only; they add no production control endpoint.

A future VM backend can implement the same launch/observe/stop/fence boundary with direct cgroups.
Running Runner as VM PID 1 would additionally require guest boot, reaping, signal handling and
recovery responsibilities currently supplied by systemd.
