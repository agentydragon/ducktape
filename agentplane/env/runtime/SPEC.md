# Environment Runtime

The Environment Runtime is the authority for one Sandbox environment's identity, bootstrap,
environment-owner lock, durable quiesce marker, Gateway control listener, and lifecycle of its
Harness Host and embedded Execution API. It coordinates Host through direct lifecycle and Session
calls and serves the public `EnvironmentRuntime` API to the existing Runner Gateway. The Host
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

Each Host lifetime reports the exact Runtime boot ID, Host boot ID and monotonically increasing
environment generation it applied. Runtime applies a
`LifecycleIntent` containing the Sandbox UID, incarnation UID, mode and seal UID. The initial mode
is `INACTIVE`. Session RPCs are forwarded only after the Host reports `ready` for the exact binding.
Runtime passes a copied binding with each direct Session call; Host checks it at ingress and inside
its admission barrier. Intent and report snapshots are copied across the ownership boundary.

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

Execution calls admitted through Runtime's public Gateway-facing gRPC server pass its control
interceptors, epoch and scope checks, and admission accounting before Runtime calls the embedded
adapter directly. An OpenSession stream uses the public server stream with an epoch-cancelled
context; cancellation and adapter shutdown settle its workers before native backend fencing.

SIGTERM follows the same Host drain and Execution fence path. When shutdown does not have an
incarnation seal request, Runtime sends `CLOSING` with an empty seal UID; this drains local work
without claiming a durable History seal. Runtime publishes a generation-bound Closing request to
its COSI lifecycle engine. Its aggregate coordinator advances only after the matching Host native
stop, Execution call drain and backend fence, optional exact History seal acknowledgement, Host join,
and accepted bootstrap settlement. Blocking steps run in Runtime-owned workers. The aggregate's
ready-to-join status is not a successful Close: Runtime then joins its engine and workers, fences
the Pod PID namespace when applicable, and releases its legacy owner lock last. A failed or uncertain
fence keeps that lock; canceling a Close caller only stops that caller's wait. A failed bootstrap is
a settled durable failure and does not by itself make process cleanup fail.
While bootstrap is running, Runtime reports the `Bootstrap` autosuspend blocker; it clears when the
bootstrap reaches a terminal state.

The VM transient process units and resource slices do not receive implicit shutdown-target stop
jobs. Runtime drains Host and fences Execution before stopping Host; all native transient processes
are bound to its service lifetime. The Runtime unit holds state and workspace mounts until those stops finish. Its systemd stop timeout is
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
Host native processes retain the 5-second policy.
The embedded Execution API allows 90 seconds to fence its native backend. Callers cannot change a process's stop grace
after launch.

## Process topology and fence proof

| Mode                              | Runtime and Host                                                                                   | Execution API                                                                                                               | Fence evidence                                                                                                                                                                                                                                              |
| --------------------------------- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Production Pod (`pid1`)           | Runtime is PID 1; embedded Host shares its child reaper.                                           | Embedded Execution claims the Codex companion in a separate PID-1 container.                                                | Replacement requires Host `native_stopped`, adapter shutdown and a fresh idle companion generation. A fenced backend loss can restart the API lifetime; unproven loss fails Runtime. Abnormal Runtime exit requires the outer container fence.              |
| VM (`systemd`)                    | Runtime and Host share the Runtime service; Host native services use `agentplane-workloads.slice`. | Embedded Execution supervises Codex transient units in `agentplane-executors.slice`, bound to the Runtime service lifetime. | Backend loss permits same-incarnation API recovery only after the native unit is stopped and its process domain is proven empty. Failed or uncertain fences fail Runtime. The 150-second lifecycle fence deadline covers the API's 90-second backend fence. |
| Explicit local/RBE (`subprocess`) | Host is embedded; native processes use subprocess supervision.                                     | The API is embedded; companion topology remains externally configured.                                                      | The API joins its companion fence. This mode does not claim production Pod cgroup containment.                                                                                                                                                              |

Native supervision retains systemd manager references, exact invocation IDs and cgroup paths until
the corresponding process-domain fences are proven.

The VM management unit is `agentplane-environment-runtime.service`. Runtime keeps its legacy scope
identity; Host uses `harness-host` and Codex transient units use `execution-service`. All bind to
the Runtime unit and `/run/agentplane-environment-runtime-lifetime`. Runtime does not enable or start the Codex companion unit;
Pod orchestration owns that separate container.

The fixed VM service commands use the mounted configuration at
`/run/agentplane/config/environment-runtime.json` and launch identity at
`/run/agentplane/sandbox/launch.json`. `--vm-resource-properties <environment-runtime.json>` remains the
systemd prestart interface for calculating the environment resource envelope.

## Configuration and startup

Runtime accepts `--config <environment-runtime.json> --launch-file <launch.json>`. Resolved supervision is
explicit: `pid1` requires a Pod PID namespace, `systemd` requires the VM `SystemdLauncher`, and
`subprocess` is selected explicitly for local/integration runs. Runtime never silently falls back
between them. The same immutable config files are passed to Host; Runtime constructs the
per-incarnation Execution API config in memory.

Runtime and Host call each other directly in all supervision modes. Runtime checks its embedded
Execution owner, backend lifetime and serving state for Host readiness; the Execution health socket remains an
Execution-local startup check. The Pod execution companion retains its separate external socket.

On startup Runtime locks the legacy owner inode before opening the old Session directory, fences
previous Host and Execution owners, starts Host,
and waits for an exact-binding initial `INACTIVE`, `ready`, `native_stopped` report before opening
Gateway control. A Gateway incarnation is not admitted until the previous incarnation has completed
its lifecycle and process fences.
