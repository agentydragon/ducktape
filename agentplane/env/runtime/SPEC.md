# Environment Runtime

The Environment Runtime is the authority for one Sandbox environment's identity, bootstrap,
environment-owner lock, durable quiesce seal, Gateway control listener, and lifecycle of its
Harness Host and embedded Execution API. It coordinates Host through direct lifecycle and Session
calls and serves the public `EnvironmentRuntime` API to the existing Runner Gateway. The Host
owns Session records, journals and native harnesses under Runtime's environment owner.

## Retained state

Runtime stores the exact selected bootstrap script (1–65536 bytes) as a durable
`BootstrapSelections` COSI resource in `StateDir/runtime-resources.db`. It commits the
selection before launching the script and runs those selected bytes. Subsequent requests must
match byte for byte. Each stdout or stderr chunk and the terminal result are separate durable
protobuf `BootstrapEvents` COSI resources in the same database, keyed by their dense sequence.
Replay orders by the sequence field and rejects gaps or events after the terminal event. The final
result or interruption event is the authority for the attempt's outcome; live `BootstrapStatuses`
are a projection of it. A selection without a terminal event is interrupted on restart, including
when the crash happened before process launch; Runtime never retries that uncertain attempt.
Output is retained in full without a byte limit or truncation. Runtime appends each script-runner
chunk (up to 64 KiB) as one resource, so storage writes do not rewrite the existing output prefix.
Resource count and restart memory remain proportional to output volume.
`BootstrapRequests` admission and all process observations are live-only COSI resources.

Runtime opens `StateDir/.agentplane-environment-runtime-owner` and holds it locked for its lifetime.
This single process-lifetime fence covers the embedded Harness Host as well. Pod and subprocess
native harnesses inherit its descriptor. Runtime releases it only after Host cleanup, worker joins
and external process fencing complete; COSI output ownership and finalizers do not replace that
cross-process fence.

The `EnvironmentSeals` COSI resource is a hard cutover from `StateDir/sessions/sealed`. Runtime
never reads or imports that marker. Stop all pre-cutover Sandbox workloads before new binaries use
their state volumes.

Runtime persists a sealed incarnation UID as an `EnvironmentSeals` COSI resource in
`StateDir/runtime-resources.db`. The active incarnation binding remains process-local and becomes
known when Gateway adopts it. A restart restores the sealed UID, so Runtime cannot reopen that
incarnation and must complete its exact Host flush before adopting a successor. Session records are
persisted as `SessionRecords` COSI resources in the same database. Harness Host reserves each record,
then updates model, reasoning effort and native Session identity through field-scoped COSI writes
under Runtime's environment owner. Its in-memory Session copy follows successful durable writes; concurrent changes
to one Session retain write order. If a native frame's record update fails, Host commits the raw frame
and stops receiving so recovery can inspect that outcome. Journals and native continuations remain
under `StateDir/sessions`; the Harness Host's latest History acknowledgement cursor and seal bit are
persisted as `IngressAcks` COSI resources in `StateDir/runtime-resources.db`.
An EnvironmentSeals write or delete error has an uncertain durable outcome: Runtime blocks admission
and refuses further identity transitions until restart reloads the COSI state.
All four durable resource types use COSI's protobuf resource encoding. Reload restores
their owner, version and finalizers from the persisted metadata. Runtime rejects the former
JSON COSI encoding; it does not import old Session or acknowledgement JSON files. Keep the
state volume with the binary version that wrote it when planning this hard cutover.

## Lifecycle boundary

The Runtime-owned Session component reports the exact Runtime boot ID and monotonically increasing
environment generation it applied. Runtime applies a
`LifecycleIntent` containing the Sandbox UID, incarnation UID, mode and seal UID. The initial mode
is `INACTIVE`. Session RPCs are forwarded only after the Host reports `ready` for the exact binding.
Runtime passes a copied binding with each direct Session call; Host checks it at ingress and inside
its admission barrier. Runtime publishes immutable typed Host intent in the shared COSI store. A Host-owned controller
applies each intent once after Session startup and while a synchronous native gate confirms the exact Runtime boot ID. Runtime revokes that gate on Host or Runtime failure, independently of COSI writes. The controller records its exact binding and Execution fence acknowledgement before
Runtime can finish that barrier. Failed or uncertain application is terminal; controller restart does
not replay it. Host owns a typed COSI report resource containing a snapshot of its mutable inventory, idle and
blocker facts, and lifecycle progress. Its controller samples under the Host admission boundary and
publishes the full applied intent alongside the report. That intent is the report's sole binding;
the fact payload does not duplicate it. Runtime observes the resource and wakes
waiters only when its intent application status matches that full intent, including a same-generation
Execution fence update. An older ready sample cannot reopen admission during fence application.
The Session worker has its own completion signal and error. An unexpected completion fails Runtime;
Runtime joins it and the shared engine before closing journals or releasing the owner. Engine shutdown
joins both controllers before owner release.

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
adapter directly. One Runtime-owned lifetime holds the native handle and immutable incarnation UID;
admission reads the adapter from that owner and closes immediately on its backend loss or stop request,
without waiting for a COSI event. The host publishes desired UID/demand and generation-bound start,
loss and fence completion facts in `ExecutionLifecycleSignals`; the controller only applies a fact to its matching
generation. Native handles and caller epochs remain in memory. An uncertain fence retains that
owner with admission closed.
An OpenSession stream uses the public server stream with an epoch-cancelled
context; cancellation and adapter shutdown settle its workers before native backend fencing.

Incarnation adoption and conditional Quiesce are sequence-bound requests to Runtime's single COSI
lifecycle coordinator, which also owns Close. Each blocking Host, Execution and durable-state boundary publishes its
stage before a Runtime-owned worker performs it; the Gateway waits for the matching terminal
status while holding transition serialization. A canceled WatchState epoch stops subsequent
adoption stages and cannot release a successor until any in-flight Execution start has a proven
fence. A Running intent already published by that epoch remains current until a successor or
Close requests its own drain. A prior requested drain is reconciled against its exact binding
instead of issuing another stop generation. Quiesce seals durably before publishing its intent;
repeated requests preserve a healthy generation. To decide whether a failed Host generation needs
a retry, Runtime verifies the exact Host binding and reads the matching Harness Host desired and aggregate
status generations from the shared COSI store; missing or stale status cannot trigger a retry.
Close cancels an active transition and waits for its settlement before preparing shutdown.

SIGTERM follows the same Host drain and Execution fence path. When shutdown does not have an
incarnation seal request, Runtime sends `CLOSING` with an empty seal UID; this drains local work
without claiming a durable History seal. Runtime publishes a generation-bound Closing request to
its COSI lifecycle engine. Its aggregate coordinator advances only after the matching Host native
stop, Execution call drain and backend fence, optional exact History seal acknowledgement, Host join,
and accepted bootstrap settlement. Blocking steps run in Runtime-owned workers. The aggregate's
ready-to-join status is not a successful Close: Runtime then joins its engine and workers, fences
the Pod PID namespace when applicable, and releases its environment owner lock last. A failed or uncertain
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
after launch. In an embedded VM, the exec-server is a Runtime COSI-managed process; its managed
fence allowance is 90 seconds while its systemd stop grace remains 5 seconds. Standalone Execution
retains its independent process supervisor.

## Process topology and fence proof

Runtime and Harness Host use one COSI process controller and registry in the Runtime namespace.
Each attempt records its owning process domain and keeps that owner's context, worker join and
error boundary. Host native-stop acknowledgement waits for every Host-domain attempt and its
finalizer; Runtime-domain attempts are covered by Runtime's own close and execution barriers.
Neither the COSI namespace nor process-controller sharing changes the native containment domains.

| Mode                              | Runtime and Host                                                                                   | Execution API                                                                                                               | Fence evidence                                                                                                                                                                                                                                              |
| --------------------------------- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Production Pod (`pid1`)           | Runtime is PID 1; embedded Host shares its child reaper.                                           | Embedded Execution claims the Codex companion in a separate PID-1 container.                                                | Replacement requires Host `native_stopped`, adapter shutdown and a fresh idle companion generation. A fenced backend loss can restart the API lifetime; unproven loss fails Runtime. Abnormal Runtime exit requires the outer container fence.              |
| VM (`systemd`)                    | Runtime and Host share the Runtime service; Host native services use `agentplane-workloads.slice`. | Embedded Execution supervises Codex transient units in `agentplane-executors.slice`, bound to the Runtime service lifetime. | Backend loss permits same-incarnation API recovery only after the native unit is stopped and its process domain is proven empty. Failed or uncertain fences fail Runtime. The 150-second lifecycle fence deadline covers the API's 90-second backend fence. |
| Explicit local/RBE (`subprocess`) | Host is embedded; native processes use subprocess supervision.                                     | The API is embedded; companion topology remains externally configured.                                                      | The API joins its companion fence. This mode does not claim production Pod cgroup containment.                                                                                                                                                              |

Native supervision retains systemd manager references, exact invocation IDs and cgroup paths until
the corresponding process-domain fences are proven.
The process controller publishes the latest attempt-scoped systemd observation for VM-native
processes, including a persistent unconfirmed start-reply state and a retained observed or unknown
exit outcome. A failed manager read preserves the previous snapshot and records the error; these
observations do not establish a process-domain fence.

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
Execution owner, backend lifetime and serving state for Host readiness. Native protocol probing
establishes Execution readiness. The Pod execution companion retains its separate external socket.

On startup Runtime locks its environment owner inode before opening the Session directory, starts
Host, and waits for an exact-binding initial `INACTIVE`, `ready`, `native_stopped` report before
opening Gateway control. During incarnation adoption, Execution fences its prior native owner before
the new execution lifetime is admitted. A Gateway incarnation is not admitted until the previous
incarnation has completed its lifecycle and process fences.
