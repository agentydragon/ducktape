# Native process supervision

The reusable launchers, COSI process controller and process registry live in
[`agentplane/env/runtime/supervision`](../). Environment Runtime, Harness Host and
Execution Service each own their component lifetime and inject their own context, state, namespace,
worker tracking and error callback. Native adapters and script workers publish
`supervision.ProcessConfig` through `supervision.ManagedProcess`; owning controllers retain
readiness, restart and journal decisions. A completed attempt never relaunches during reconciliation.

| Owner               | Processes and state                                                                                             |
| ------------------- | --------------------------------------------------------------------------------------------------------------- |
| Environment Runtime | Environment bootstrap, Host/Execution lifecycle, environment generation and legacy state-owner lock             |
| Harness Host        | Shared Codex app-server, Claude Sessions, Session setup/workspace helpers, Session journals and History ingress |
| Execution Service   | Native exec-server and execution requests; the Pod companion owns the native executor's separate PID namespace  |

Each supervisor creates `Process` handles through a private backend interface. `Process` owns
streams, PID and completion result; the backend owns launch, observation and execution-domain
cleanup. `Start` bounds startup independently of process lifetime. `Stop` requests graceful shutdown
and cleanup; `Wait` returns only after exit and cleanup. A canceled wait cancels observation, not
execution. Canceling startup tears down its intent and joins any late launch.

COSI-managed launches use `ProcessDesiredStates`, `ProcessStatuses` and a finalizer per attempt.
Launch callbacks and native handles stay in the registry. Teardown joins an in-flight start and
fences any instance it returns before releasing the finalizer. Owners check remaining process
intents before acknowledging cleanup; a reader observing leader exit cannot hide a failed fence.

Runtime coordinates the process owners over private sockets. Host reports native stop against the
exact Runtime/Host boot IDs and lifecycle generation. Runtime fences Execution before authorizing
Host sealing and waits for the matching seal/History ACK before completing quiesce. The
[Host protocol](../../harnesshost/SPEC.md) and [environment lifecycle](../../docs/lifecycle.md) define those
barriers. Transport loss closes admission and drains native work without inventing a seal.

## VM services

The launchers use [go-systemd's D-Bus bindings](https://github.com/coreos/go-systemd/tree/v22.7.0/dbus)
over systemd's root-only `/run/systemd/private` socket. The bindings own connection setup, job
completion tracking and property-change subscriptions. Native launches are transient `Type=exec`
services; they need no supervisor subprocess or `systemd-run` helper. Systemd applies resource and
identity settings before exec. Native units use `Restart=no`; their owner decides whether a
successor is permitted.

The NixOS guest and system-test guest share this hierarchy:

```text
agentplane.slice                         overall CPU, memory, swap and PID envelope
  agentplane-environment-runtime.service environment lifecycle, Host and Execution API
  agentplane-workloads.slice             aggregate harness/setup budget
    agentplane-native-<owner>-<UUID>.service
  agentplane-executors.slice             aggregate exec-server/tool budget
    agentplane-native-<owner>-<UUID>.service
```

The preflight helper applies the enclosing envelope before Runtime starts. Launchers configure
slice and per-service budgets through D-Bus. Native services run as the configured unprivileged
UID/GID without capabilities and with `NoNewPrivileges`. Their filesystem is read-only except
`/state` and the guest's shared `/tmp`; sharing temporary files preserves local exec-server
transport. The trusted owners can access the system manager. The unprivileged harness identity
cannot start, stop or reconfigure system units.

Separate stdout/stderr and stdin pipes cross D-Bus as file descriptors. `ExecStartEx` disables
manager argument expansion, preserving literal dollar signs, specifiers and shell source.
Environment values are explicit; systemd adds service metadata without inheriting the owner's
private environment. Login environment defaults are disabled. Start waits for the completed start
job, so a rejected `execve` is a launch failure. `RemainAfterExit=yes` and `CollectMode=inactive`
retain exit information until the owner reads it and stops the unit. Ordinary stops signal the
unit before cleanup. If an external stop collects the unit before observation, the launcher reports
process loss (code 125) and checks the saved kernel domain before completing cleanup.

### Invocation and scope fencing

Every launch allocates one UUID before `StartTransientUnit`. A lost reply or cancellation triggers
cleanup of that same identity. An uncertain launch never creates a second unit to retry its effects.
Native effects remain subject to journal reconciliation.

`PrepareForOwner` takes an explicit service unit, lifetime directory and scope ID. Runtime uses the
legacy Runner scope ID. Host uses `harness-host` and Execution uses `execution-service`; their
native scopes remain independent. All three bind their native units to
`agentplane-environment-runtime.service` and require its invocation marker under
`/run/agentplane-environment-runtime-lifetime`.

Service death stops its native units. `KillMode=control-group` contains descendants even when they
change process groups. Natural leader exit also stops the unit. `Wait` requires the unit to be
inactive/failed and its execution domain empty. A disconnected manager leaves cleanup blocked;
process exit alone does not authorize a successor or journal seal. Runtime joins Host and Execution
cleanup and proves their respective native process domains empty during shutdown and replacement.

Runtime and native supervision share typed unit snapshots, stop-job observation and process-domain
fencing in `env/runtime/supervision`. A single D-Bus `GetAll` reads Unit and Service properties together;
missing or malformed properties cannot establish cleanup. `StopUnit` acceptance and job completion
are separate from the execution-domain fence. If a recorded cgroup exists, its recursive
`cgroup.events` population must clear (or the cgroup must disappear), including after a failed stop.
On the pinned guest systemd, a stopped loaded unit with no main/control PID or pending job can
also report an explicitly cleared `ControlGroup`: normal manager cleanup clears that property only
after recursive emptiness, even when the prior `InvocationID` remains. A recorded domain still
requires its kernel check. An empty path on its own proves nothing. Exact invocation changes and
unresolved starts after transport loss keep admission blocked. A packaged service's empty cgroup
does not prove cleanup of the separately managed native units owned by that service.

Runtime holds `<state_dir>/.agentplane-environment-runtime-owner`. Host holds
`<state_dir>/harness-host/.agentplane-harness-host-owner`; Execution holds its own owner under
`<state_dir>/execution`. These new Runtime and Host lock paths do not fence an old process using
`.agentplane-runner-owner`, so stop old workloads before reusing a state volume. After acquiring an
owner lock, its launcher discovers old units in the
scope derived from the Sandbox UID, state directory and explicit scope ID, verifies their expected
slice, and fences them before admitting new native work. Other scopes are untouched. Session
records and journals keep their existing paths.

Systemd removes the owner's lifetime directory before restarting its service. A start request
queued on an old connection then fails its invocation condition if it arrives after replacement,
including after the replacement's inventory scan. A skipped condition never counts as native exec.

## Pods and tests

### PID 1 and the outer fence

`environment.supervision` explicitly selects `pid1` or `subprocess` for Runtime. Production Pods
select `pid1`, invoke Environment Runtime directly and disable shared process namespaces. The PID1
backend rejects a Runtime whose PID is not 1. Runtime and its embedded Host share one PID1 reaper.
The Execution API is also embedded; its Pod companion runs separately as PID1 and owns the native
exec-server namespace. Non-PID1 integration fixtures select Runtime's `subprocess` backend. VM
systemd supervision does not fall back to a Pod backend.

PID1 launches each child in its own process group. The owner descriptor is inherited, although
native runtimes may close inherited descriptors. The subprocess supervisor retains a trusted
owner-descriptor copy and a parent-liveness pipe. Owners keep their locks through ordinary process
cleanup and journal closure. `Stop` closes stdin and sends TERM, then KILL after its grace budget;
a canceled stop escalates and leaves the cleanup barrier owned. Leader exit kills the remaining
process group. `Wait` requires the group to disappear; a failed fence retains the finalizer.

Escaped groups require whole-container teardown. They can retain stdout/stderr pipes. Actual
Runtime/Host graceful shutdown joins accepted script and protocol readers; an escaped writer can
hold it until the outer runtime's termination grace expires and SIGKILL ends the container. Replacement
after an abnormal Runtime exit requires the outer containment fence.

The companion's PID1 reaper has no environment state-owner descriptor. Its live ownership
connection owns the native executor. It closes stdin after launch and forwards stdout/stderr
asynchronously. On ordinary lease release, PID1 stops exec-server, kills any remaining processes
in its private PID namespace (including escaped process groups), and waits until only PID1 remains
before publishing the next idle generation. This is safe because the companion and its workload use
the same unprivileged UID without privilege escalation. An unexpected exec-server exit triggers
that same sweep and closes the ownership lease so Runtime can recover; if the namespace scan finds
different credentials or the process sweep cannot complete, PID1 exits and the container runtime
fences the entire namespace. Escaped output readers are therefore joined only after either the
in-container process sweep or the outer container fence proves them gone.

One process-wide reaper owns every PID1 child and adopted orphan. No PID1 child uses `exec.Cmd.Wait`.
Start's exec handshake and registration share a mutex with reaping. Managed exits use
`waitid(WNOWAIT)`: the zombie leader reserves the process group identity while KILL is sent, then
targeted `wait4` consumes its status. Orphan discovery reads every runtime thread's
`/proc/self/task/*/children`; only unregistered children use orphan `wait4`. Mixing active
subprocess wait owners with PID1 reaping is rejected. Unexpected wait/discovery failures terminate
PID1 and leave the outer runtime to fence the namespace.

The outer fence is complete when the container runtime observes its init exit. Acquiring a state
lock alone does not prove that boundary. Linux's
[`do_exit`](https://github.com/torvalds/linux/blob/v6.12/kernel/exit.c) closes files before
`exit_notify` reaches namespace teardown;
[`zap_pid_ns_processes`](https://github.com/torvalds/linux/blob/v6.12/kernel/pid_namespace.c) then
kills and joins remaining tasks. Abrupt PID1 death also kills an internal supervisor; its retained
lock descriptor is not an independent whole-container fence.

Sandbox Controller waits for the prior Pod's deletion event before creating its successor
(`converge` in `sandbox_controller/controller.go`). Normal kubelet restarts and graceful Pod deletion
supply the outer fence. An API deletion event after force deletion or node loss does not prove the
old container stopped. Container teardown or an external node/storage fence must precede
replacement. Node-local storage and the fixed inode lock remain part of the supported contract;
neither Pod UID nor PVC access mode replaces runtime fencing.

### Acceptance coverage

`//agentplane/env/e2e:test_pid1_container` runs both backend fixtures in real private PID namespaces.
It closes the native's inherited owner descriptor and starts TERM-resistant same-group and escaped
descendants. It checks lock exclusion, observes container exit and descendant death, then reacquires
the lock before restarting the container. The production cases invoke the combined Runtime and cover
clean TERM, Host-managed native processes and abrupt bootstrap death. They do not claim graceful completion
with escaped pipe holders. JSON evidence and logs go to undeclared test outputs.

`//agentplane/env/e2e:test_executor_container` exercises the separately packaged Execution companion,
native executor loss/replacement, shared app-server continuity and coordinated quiesce. Session and
tool effects are never replayed as part of process replacement.

The system-test guest boots real systemd with the same owner/slice units. Before Runtime starts,
`//agentplane/env/runtime/supervision:environment_runtime_systemd_fixture` exercises credentials, separate streams, kernel
resource limits, lost/canceled launch replies, detached descendants, parent-unit death, ordinary
stop and scoped startup fencing. VM scenarios exercise storage, actual harnesses, tool OOM and
suspend/resume. The fixture wrapper and observer add no production control endpoint.
