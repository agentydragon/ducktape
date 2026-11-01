# Environment Runtime

The Environment Runtime is the authority for one Sandbox environment's identity, bootstrap,
environment-owner lock, durable quiesce marker, Gateway control listener, and lifecycle of its
Harness Host and embedded Execution API. It serves `EnvironmentRuntime.Connect` to the Host
on a private Unix socket and the `EnvironmentRuntime` API to the existing Runner Gateway. The Host
owns Session records, journals, native harnesses and its separate owner lock.

## Retained state

Runtime opens `StateDir/.agentplane-environment-runtime-owner` and holds it locked for its lifetime.
Harness Host separately locks `StateDir/harness-host/.agentplane-harness-host-owner`. This is a
direct cutover from the old `.agentplane-runner-owner` lock paths: an old process does not contend
on the new paths, so all pre-rename Sandbox workloads must stop before the new binaries use their
state volumes. Old lock files may remain; Session records and journals do not move.

The durable seal remains `StateDir/sessions/sealed`, with the existing incarnation UID bytes as
its payload. Runtime reads, removes, and writes this marker using the old filename, directory,
payload and durability semantics. Session records, journals, native continuations and History ACK
positions remain under their existing `StateDir/sessions` paths; Runtime does not move them.

## Lifecycle boundary

Each Host connection begins with `HostHello` and then reports the exact Runtime boot ID, Host boot
ID and monotonically increasing environment generation it applied. Runtime sends a
`LifecycleIntent` containing the Sandbox UID, incarnation UID, mode and seal UID. The initial mode
is `INACTIVE`. Session RPCs are forwarded only after the Host reports `ready` for the exact binding.
Requests carry the three binding values as metadata; Host checks them inside its admission barrier.

For quiesce and replacement, Runtime first asks Host to stop admissions and drain native work.
`native_stopped` is reported only after admitted setup, Session calls, shared app-server work and
native-process finalizers settle. Runtime then stops and fences the embedded Execution API and publishes
`execution_fenced` for that same generation. Host can seal and flush Session journals only after
both facts match the requested binding. Runtime treats the History seal acknowledgement as a
separate required boundary. It never infers a seal or flush from process exit or an Execution fence.

Runtime tracks in-flight public admissions synchronously so a newly admitted Session invalidates
an idle-quiesce token before Host facts are sampled. A canceled Gateway waiter does not cancel
accepted bootstrap work or leave a canceled drain half-applied: a reconnect must finish the
current Host native stop and Execution fence before reopening, even when the incarnation UID is
unchanged. Replacement never starts a successor Execution API lifetime before the previous one is
fenced.

SIGTERM follows the same Host drain and Execution fence path. When shutdown does not have an
incarnation seal request, Runtime sends `CLOSING` with an empty seal UID; this drains local work
without claiming a durable History seal. Runtime waits for accepted bootstrap work to settle before
releasing its legacy owner lock. A failed bootstrap is a settled durable failure and does not by
itself make process cleanup fail.
While bootstrap is running, Runtime reports the `Bootstrap` autosuspend blocker; it clears when the
bootstrap reaches a terminal state.

The VM Host unit, transient process units and resource slices do not receive implicit shutdown-target
stop jobs. Runtime owns their stop order, draining Host and fencing Execution before stopping Host;
each native transient process is bound to Runtime or Host. The
Runtime unit holds state and workspace mounts until those stops finish. Its systemd stop timeout is
unlimited because accepted bootstrap work is joined to durable settlement; the lifecycle fence itself
has a 150-second deadline.

VM service cleanup and native supervision share one typed systemd observation and fencing contract.
A stopped loaded unit with an explicitly cleared `ControlGroup`, no main/control PID and no pending
job supplies manager cleanup evidence; the previous `InvocationID` may remain. A retained cgroup
path always requires recursive kernel emptiness, including after failed stops. `StopUnit` acceptance
and stop-job completion alone do not authorize replacement. Missing/malformed properties, changed
invocations, failed required completion evidence and uncertain starts keep admission closed. Each
owner must still fence its native transient units separately before lifecycle acknowledgement.

Each managed process receives a non-negative `StopGrace` in its launch configuration; `Stop(ctx)`
uses that fixed value for the process lifetime. Systemd sets `TimeoutStopUSec` when it starts a
transient process unit, while the subprocess helper receives the same duration over its private
launch arguments and owns TERM-to-KILL escalation. PID1 applies the same launch-time deadline.
Host native processes retain the 5-second policy and the Runtime-managed Host process uses 30 seconds.
The embedded Execution API allows 90 seconds to fence its native backend. Callers cannot change a process's stop grace
after launch.

## Process topology and fence proof

The executable names below are fixed package contracts. Their systemd definitions live in
`../images/systemd/` for the VM service units.

| Mode | Runtime and Host | Execution API | Fence evidence |
| --- | --- | --- | --- |
| Production Pod (`pid1`) | Runtime is container PID 1 and supervises Harness Host as a subprocess. | Runtime serves the API in-process and claims the Codex companion in a separate PID-1 Kubernetes container. | Replacement requires Host `native_stopped`, adapter shutdown and a fresh idle companion generation. A fenced backend loss can restart the API lifetime in the same Runtime process; unproven loss fails Runtime. An abnormal Host exit fails Runtime so the container runtime fences the whole Pod process cgroup. |
| VM (`systemd`) | Runtime and Host are separate systemd units. Runtime starts Host over D-Bus and retains its exact invocation ID and cgroup path. | Runtime serves the API in-process and supervises Codex transient units in `agentplane-executors.slice`, bound to the Runtime unit lifetime. | Backend loss permits same-incarnation API recovery only after the native unit is stopped and its process domain is proven empty. Failed or uncertain fences fail Runtime. The Runtime's 150-second lifecycle fence deadline covers the API's 90-second backend fence. |
| Explicit local/RBE (`subprocess`) | Runtime starts Host as a child process. It is a test/development mode and does not imply PID 1 containment. | Runtime serves the API in-process; companion topology remains externally configured. | The API joins its companion fence. This mode does not claim production Pod cgroup containment. |

Runtime pins each systemd service unit over D-Bus before start and holds its manager reference,
exact invocation ID and cgroup path until its process-domain fence is proven.

VM units are `agentplane-environment-runtime.service` and `agentplane-harness-host.service`.
The Runtime keeps the legacy `agentplane-runner` scope identity during cutover; Host uses `harness-host`,
and Codex transient units keep `execution-service` while binding to the Runtime unit lifetime.
The Runtime and Host lifetime directories are `/run/agentplane-environment-runtime-lifetime` and
`/run/agentplane-harness-host-lifetime`. Runtime does not enable or start the Codex companion unit;
Pod orchestration owns that separate container.

The fixed VM service commands use the mounted configuration at
`/run/agentplane/config/environment-runtime.json` and launch identity at
`/run/agentplane/sandbox/launch.json`. Runtime also accepts optional
`--harness-host-binary` for tests; production resolves that sibling binary from its mounted disk.
`--vm-resource-properties <environment-runtime.json>` remains the
systemd prestart interface for calculating the environment resource envelope.

## Configuration and startup

Runtime accepts `--config <environment-runtime.json> --launch-file <launch.json>`. Resolved supervision is
explicit: `pid1` requires a Pod PID namespace, `systemd` requires the VM `SystemdLauncher`, and
`subprocess` is selected explicitly for local/integration runs. Runtime never silently falls back
between them. The same immutable config files are passed to Host; Runtime constructs the
per-incarnation Execution API config in memory.

For Pod and explicit subprocess modes, Runtime and Host use short private sockets directly under
`StateDir` (`r.sock`, `h.sock`, `e.sock`) so test and deployment state paths remain within the
Unix-domain socket path limit. Execution keeps its ownership lock under `StateDir/execution` and
binds the configured `e.sock` path. VM mode retains the existing explicit `/state/runtime`,
`/state/harness-host` and `/state/execution` socket locations used by its systemd unit arguments.

On startup Runtime locks the legacy owner inode before opening the old Session directory, fences
previous Host and Execution owners, binds its private lifecycle UDS with mode `0600`, starts Host,
and waits for an exact-binding initial `INACTIVE`, `ready`, `native_stopped` report before opening
Gateway control. A Gateway incarnation is not admitted until the previous incarnation has completed
its lifecycle and process fences.
