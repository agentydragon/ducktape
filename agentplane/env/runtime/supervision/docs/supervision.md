# Native process supervision

The reusable launchers, COSI process controller and registry live in
[`agentplane/env/runtime/supervision`](../). Runtime owns environment lifetime and constructs
scoped process views for bootstrap/Execution and native harness work. One process controller shares
attempt identities while each domain retains its native supervisor, worker join and error callback.
Adapters and scripts publish `ProcessConfig` and retain `ManagedProcess` handles; their controllers
own readiness, restart and journal decisions. A completed attempt never relaunches on reconciliation.

Each supervisor creates `Process` handles through a private backend interface. `Process` owns
streams, PID and completion result; the backend owns launch, observation and execution-domain
cleanup. `Start` bounds startup independently of process lifetime. `Stop` requests graceful shutdown
and cleanup; `Wait` is intended to return only after exit and cleanup. A canceled wait cancels
observation, not execution. Canceling startup tears down its intent and joins any late launch.

COSI-managed launches use `ProcessDesiredStates`, `ProcessStatuses` and a finalizer per attempt.
Launch callbacks and native handles stay in the registry. Teardown joins an in-flight start and
fences any instance it returns before releasing the finalizer. Owners check remaining process
intents in their process domain before acknowledging cleanup; a reader observing leader exit cannot
hide a failed fence. The Session aggregate drain ignores Runtime-domain attempts, whose cleanup is owned
by the Runtime's barriers.
VM native-process status also carries the latest typed systemd observation for that attempt,
including start-request acceptance or an unconfirmed reply, the expected and observed invocation IDs
and captured cgroup. A lost start reply remains unconfirmed after later manager observations. The
observed exit code or unknown-exit error is retained separately from later unit snapshots and is
published before waiting for the domain fence.
Workspace-helper diagnostics
read this status rather than issuing a second manager query. A manager read error retains the last
snapshot and marks it stale with the error. Observations may lag the manager; they are diagnostic
facts, not evidence that a process domain is fenced. Pod processes have no systemd observation.
Embedded VM Execution registers its exec-server with the Runtime process controller as well.
Execution's managed fence timeout is 90 seconds, independent of the unit's 5-second systemd stop grace.

Runtime's Session aggregate observes exact-generation native cleanup before Execution is fenced
and journals seal. Exact durable History acknowledgements remain a separate barrier. See the
[Runtime contract](../../SPEC.md) and [lifecycle ordering](../../docs/lifecycle.md). Component failure
closes admission and drains native work without creating a seal.

## VM services

The launchers use [go-systemd's D-Bus bindings](https://github.com/coreos/go-systemd/tree/v22.7.0/dbus)
over the system API bus. The bindings own connection setup, job
completion tracking and property-change subscriptions. Native launches are transient `Type=exec`
services; they need no supervisor subprocess or `systemd-run` helper. Systemd applies resource and
identity settings before exec. Native units use `Restart=no`; their owner decides whether a
successor is permitted.

The NixOS guest and system-test guest share this hierarchy:

```text
agentplane.slice                         overall CPU, memory, swap and PID envelope
  agentplane-environment-runtime.service environment lifecycle, Sessions and Execution API
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

`DeclareSystemdScope` publishes COSI intent with an explicit service unit, lifetime directory and
scope ID. The scope controller prepares the marker, configures the slice and fences stale units
before publishing ready status; the process controller gates launches on that status. Runtime uses
`environment-runtime`. Native harnesses use `harness` and Execution uses `execution-service`; their
native scopes remain independent. All three bind their native units to
`agentplane-environment-runtime.service` and require its invocation marker under
`/run/agentplane-environment-runtime-lifetime`.

Service death stops its native units. `KillMode=control-group` contains descendants even when they
change process groups. Natural leader exit also stops the unit. `Wait` requires the unit to be
inactive/failed and its execution domain empty. A disconnected manager leaves cleanup blocked;
process exit alone does not authorize a successor or journal seal. Runtime joins Session and Execution
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

Runtime opens the COSI backing database and holds its exclusive lock before declaring native
supervision scopes. Each component's scope controller
discovers old units in its scope derived from the Sandbox UID, state directory and explicit scope ID,
verifies their expected slice, and fences them before admitting new native work. Other scopes are
untouched.

The database lock excludes a second Runtime before its VM scope controller can fence units; the VM path does
not take a separate owner flock. COSI controller outputs arbitrate within each Runtime's engine,
not between those engines. Neither the database lock nor a COSI resource alone proves that a
surviving native process has stopped.

Systemd removes the owner's lifetime directory before restarting its service. A start request
queued on an old connection then fails its invocation condition if it arrives after replacement,
including after the replacement's inventory scan. A skipped condition never counts as native exec.

The marker is what distinguishes invocations; simpler systemd mechanisms do not (checked against
the v257.9 and v260.5 `systemd.unit`/`systemd.exec` man pages):

- `BindsTo=` and `After=` on the stable Runtime service name bind a late old request to the
  successor. The VM fixture's `held_old_invocation_is_fenced_after_successor_inventory` case keeps a
  no-condition control showing this.
- `ConditionEnvironment=INVOCATION_ID=...` reads the manager's environment, not the sender's
  invocation.
- `RuntimeDirectoryPreserve=no` recreates the same directory for the successor; only the
  invocation-named marker inside it differs.

A per-invocation owner unit name would give systemd a distinct dependency identity but replaces the
stable Runtime service ownership. The probe holds a start request on a live sender; it does not prove
delivery after the sender dies. The fixture's owner keeps its RuntimeDirectory under
`/run/agentplane/execution-service` because the Runtime unit's `ProtectSystem=strict` makes the rest of
`/run` read-only.

## Pods and tests

### Why there is no per-process supervisor

The former subprocess backend existed for tests outside a private PID namespace. It added a
supervisor, liveness and confirmation pipes, and an inherited owner lock for every native process;
it could not prove cleanup when the helper died before reporting the native PID. Production already
uses the Pod PID namespace or systemd's service and cgroup fence. Tests now exercise those same
boundaries. The COSI database lock prevents concurrent Runtime ownership but is not a native-process
fence. A replacement must observe the outer container fence or fence the prior systemd units.

### PID 1 and the outer fence

Pods explicitly select `pid1`, invoke Environment Runtime directly and disable shared process
namespaces. Runtime must be PID 1 and its process domains share one reaper. The Execution API is
embedded; its Pod companion runs separately as PID 1 and owns the native exec-server namespace.

PID 1 launches each child in its own process group. The open COSI database excludes a concurrent
Runtime; the private PID namespace and its outer runtime fence contain descendants after PID 1
death. Stop closes stdin and sends TERM, then KILL after its grace budget. After observing leader
exit, PID 1 signals the remaining group and reaps adopted children. It keeps the leader unreaped
through signaling so the numeric group ID cannot be reused before targeting.

Escaped groups require whole-container teardown. They can retain stdout/stderr pipes. Actual
Runtime graceful shutdown joins accepted script and protocol readers; an escaped writer can
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
`/proc/self/task/*/children`; only unregistered children use orphan `wait4`. Unexpected wait or
discovery failures terminate PID1 and leave the outer runtime to fence the namespace.

The outer fence is complete when the container runtime observes its init exit. Acquiring a state
lock alone does not prove that boundary. Linux's
[`do_exit`](https://github.com/torvalds/linux/blob/v6.12/kernel/exit.c) closes files before
`exit_notify` reaches namespace teardown;
[`zap_pid_ns_processes`](https://github.com/torvalds/linux/blob/v6.12/kernel/pid_namespace.c) then
kills and joins remaining tasks.

Sandbox Controller waits for the prior Pod's deletion event before creating its successor
(`converge` in `sandbox_controller/controller.go`). Normal kubelet restarts and graceful Pod deletion
supply the outer fence. An API deletion event after force deletion or node loss does not prove the
old container stopped. Container teardown or an external node/storage fence must precede
replacement. Node-local storage and the COSI database's fixed inode lock remain part of the supported contract;
neither Pod UID nor PVC access mode replaces runtime fencing.

### Acceptance coverage

`//agentplane/env/e2e:test_pid1_container` exercises PID 1 in real private PID namespaces. It
starts TERM-resistant same-group and escaped descendants and observes container exit and descendant
death before restarting. The production cases verify COSI database lock exclusion through shutdown
and cover clean TERM, Session-managed native processes and abrupt bootstrap death. They do not claim
graceful completion with escaped pipe holders. JSON evidence and logs go to undeclared test outputs.

`//agentplane/env/e2e:test_executor_container` exercises the separately packaged Execution companion,
native executor loss/replacement, shared app-server continuity and coordinated quiesce. Session and
tool effects are never replayed as part of process replacement.

The system-test guest boots real systemd with the same owner/slice units. Before Runtime starts,
`//agentplane/env/runtime/supervision:environment_runtime_systemd_fixture` exercises credentials, separate streams, kernel
resource limits, lost/canceled launch replies, detached descendants, parent-unit death, ordinary
stop and scoped startup fencing. VM scenarios exercise storage, actual harnesses, tool OOM and
suspend/resume. The fixture wrapper and observer add no production control endpoint.
