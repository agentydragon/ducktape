# Environment Runtime

Environment Runtime owns one Sandbox environment lifetime: identity, bootstrap, environment lock,
incarnation seal, Gateway-facing control, Session management, retained records, journals, native
harness controllers, History delivery and Execution. Components have direct dependencies and
independent COSI observations inside Runtime's single engine. The public `EnvironmentRuntime` API
retains both environment and Session operations. A future independent Session domain and storage
model remain separate design work.

## Durable state and recovery

Runtime opens `StateDir/runtime-resources.db` with an exclusive lock before preparing native
supervision and holds it until native cleanup, delivery/worker joins and journal closure complete.
Prior process exit or lock acquisition alone does not prove that an abnormal container's native
descendants were fenced.

The selective COSI store in `StateDir/runtime-resources.db` persists these protobuf resources,
including their metadata, versions and finalizers:

- `BootstrapSelections`: the exact selected bootstrap bytes, committed before launching them.
- `BootstrapEvents`: dense ordered stdout/stderr chunks and the terminal result or interruption.
- `SessionRecords`: durable reservations, setup source and native Session identity.
- `IngressAcks`: the last durably acknowledged journal cursor and seal bit.
- `EnvironmentSeals`: the sealed incarnation UID.

Each bootstrap request selects 1–65536 bytes; later requests must match byte for byte. Output is
retained without truncation, in script-runner chunks up to 64 KiB, with no existing-output-prefix
rewrite. Replay orders by embedded sequence and rejects gaps or output after a terminal result.
A selection without a terminal event becomes interrupted on restart, even if launch had not begun;
Runtime never retries that uncertain attempt. Resource count and restart memory scale with output.

Session journals remain SQLite databases under `StateDir/sessions/<id>/`; native continuations
retain their existing paths, and Codex shares `StateDir/codex/`. Journal transactions commit before
publication or corresponding native writes. Model/reasoning changes, terminal receipts and the
effective configuration checkpoint commit together. Native adapters read committed configuration.
Dispatched commands without a committed outcome remain uncertain and are not replayed.

Startup reconstructs retained records and journals before exposing schedulable Session state.
An unsuccessful Session reservation write revokes admission and fails Runtime while holding the
admission lock: the write may have reached durable storage even when COSI's in-memory view is stale.
Only restart may reload that outcome; a retained reservation keeps its original event source and is
recovered without replaying setup or other uncertain native work.
Interrupted setup becomes a durable terminal outcome and is not rerun. Native recovery compares
journal evidence with the harness's own continuation evidence, preserving unknown outcomes when
that evidence is insufficient. See [retained state](docs/retained_state.md) and the
[Session protocol](docs/session_protocol.md).

A restored seal prevents reopening its same incarnation. A successor first completes the prior
sealed delivery and process fences. A seal write/delete error closes admission and blocks identity
transitions until restart reloads durable evidence. Live COSI desires, statuses, requests, admission
counts and operation slots are separate from those durable authorities.

## Admission and component ownership

`EnvironmentRuntimeDesiredStates` is the only environment lifecycle desire. Runtime owns its mode,
incarnation UID, seal UID and monotonically increasing generation. A lifecycle change closes the
admission gate synchronously before desired-state publication. Failed or uncertain publication
keeps that gate closed. A published Running revision becomes accepted only after transition-owned
`ResetDemand`, eligible journal reopening and recoverable-work preparation finish.

RAM-preserving suspension uses a separate admission gate. `PrepareSuspend` atomically checks the
observed idle period and closes new Session, execution and native admissions without sealing or
stopping existing processes. Controller-owned generations make preparation and release retryable;
`ResumeAdmission` retains the released generation so delayed prepares cannot close it again.
Watch reconnects preserve this gate, and Quiesce retires it only after lifecycle admission closes.
The controller persists dispatch fencing before preparation and retains it through checkpoint and
thaw; [control](docs/control.md) specifies the protocol.

Session/app-server controllers register work through Runtime's exact-revision guard. Accepted
registration increments `EnvironmentAdmissionCounts`, executes without holding `Runtime.mu`, and
settles its count after the component operation slot is visible. The resource also carries the
accepted generation so controllers wake after Running preparation. It is an observation of the live
admission boundary, not another desired lifecycle. Registration/publication failure fails Runtime;
controller restart cannot replay an uncertain effect.

The Runtime directly constructs the Session manager, retained store, harness adapters, shared
Codex app-server, controllers and History worker. Components retain their own process handles,
one-off slots, protocol readers and worker joins. App-server operation start/completion advances a
revision on its lifecycle signal independently of demand; demand resets and concurrent exit updates
preserve that revision. Completion publication and local finish share the slot mutex before consumption.
Session/app-server statuses carry the observed
`EnvironmentGeneration`; demand sequences, Execution attempt generations, operation IDs and process
attempt IDs remain distinct identities. Session one-off starts and completions advance the existing
Session reconcile snapshot's `OperationRevision`, independently of execution demand. Completion
publication and local completion hold the slot mutex, so a woken controller consumes the finished
operation after its result is published. One process controller and registry serve separate Runtime
and harness domains without combining their native containment or cleanup proofs.

At the exact Sealing barrier, each Session closes admission for all new command IDs under the
same lock as journal admission and sealing. The Session lifecycle status reports
`AdmissionClosed` for its observed environment generation only after this live gate closes and
accepted commands are settled.
An exact-turn interrupt may enter during halt before that cutoff; an identical admitted command
can return its original receipt afterward. A failed settlement or seal leaves the gate closed.
The live gate reopens only after a successor journal reopen and its acknowledgement and seal
publication succeed for the current Running incarnation; same-incarnation re-entry stays closed.

External authentication, Sandbox/incarnation authorization and Gateway epochs remain in
`runtime/control`. Direct Session admission uses that authority and Runtime's gate. `CreateSession`
acknowledges a durable reservation, without promising process readiness. Model lookup occurs outside
the gate; the reservation rechecks the epoch and accepted revision before its retained write.
Claude model lookup uses the local gRPC `model_config_target` without a bearer; the egress
sidecar authenticates the upstream connection with the Sandbox's SVID.
Accepted setup may finish after new admission closes. Canceling a setup's bootstrap waiter does
not cancel the bootstrap operation. Public Execution mutations are counted and admitted against the
live Runtime-owned executor lifetime; backend loss closes admission without waiting for a COSI event.

## Startup, Quiesce and Close

The COSI Session owner reconciles retained records into live Sessions and reports recovery success
or failure. Runtime waits for the initial inventory, observes exact-generation native cleanup and starts History only after
successful recovery. Public control listens after that startup barrier. Runtime failure or Close
wakes startup waiters even while recovery is unfinished.

Adoption and conditional Quiesce pass a typed in-process request to the Runtime-owned lifecycle
coordinator and wait for that operation's completion. Close observes the coordinator's live COSI
status for its exact closing generation. Transition requests and completions remain private handoffs.
Each blocking boundary runs in an owned worker so component observation continues.
A canceled watch epoch ends future adoption stages but does not release a successor until accepted
Execution starts and their fences settle. Repeated Quiesce preserves a healthy desired generation;
only a failure for that exact generation can trigger retry.

Quiesce selects the durable incarnation seal before publishing the draining revision. Native-stop
completion requires settled admissions/setup, each Session's current-generation `PreFenceSettled`
status, shared app-server cleanup and all harness-domain process finalizers. A Session publishes
that status only after halt, its process stop proof and the resulting inspection writes settle.
Native readers or shared Codex End callbacks are joined before process stop is reported, so late
exit inspection cannot follow a true pre-fence status. Runtime then drains and fences Execution.
Its fence completion names both the Execution attempt and lifecycle generation. Journals seal only
after those exact native and Execution barriers. `PreFenceSettled` is independent of sealing, so the
Session and aggregate barriers do not form a cycle. Each Session lifecycle controller projects a
committed journal seal into its volatile status only after local cleanup and the matching
generation's global barriers permit sealing and its admission cutoff has settled. The projection
names the seal UID and cursor; a missing or stale projection or an unsettled admission cutoff
cannot complete the environment barrier.
The journal remains the durable seal authority, so recovery rechecks it and the fresh cleanup
proofs before publishing status. History sends final entries and seals, persists the
acknowledgements, and satisfies delivery only at each exact sealed cursor. The Session lifecycle
controller observes the retained acknowledgement and reports `Delivered` for the matching committed
seal UID, cursor and environment generation. The environment aggregate completes only when every
Session in the inventory reports delivery for that generation.

Ordinary Close without a seal drains local work and creates no seal. Close preserving a seal waits
for exact durable acknowledgements. Runtime first settles any accepted transition and fence before
publishing Closing; it joins History before journals close, stops/joins the engine, joins workers,
closes journals, fences the Pod PID namespace when applicable and releases its owner last.
The Runtime Close status reports the ordered stage and any failure; it is never persisted.
`ReadyToJoin` is permission to finish those steps, not successful owner release. Coordinator exit
wakes callers before joining workers, preventing a shutdown worker from waiting on a transition
caller that is itself waiting for engine exit. Joined workers and exact fences still determine
whether cleanup is proved; coordinator restart never replays accepted work.

Concurrent Close callers share one accepted worker. Caller cancellation ends only that wait.
Session recovery or History failure closes admission and starts cleanup without manufacturing a
seal. Failed native cleanup, engine failure or failed sealed delivery retains ownership. A terminal
bootstrap error is a settled durable outcome and does not by itself mean cleanup failed.

The [lifecycle contract](docs/lifecycle.md) and [COSI map](docs/cosi.md) describe the barriers and
resource writers. The [public control contract](docs/control.md) describes caller-visible behavior.

## Native supervision

| Mode                    | Runtime and native harnesses                                                         | Execution and replacement fence                                                                                                                                  |
| ----------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Production Pod (`pid1`) | Runtime is PID 1 and owns one reaper; the COSI database excludes concurrent Runtimes | Embedded adapter claims the companion in a separate PID-1 container; replacement requires native cleanup, adapter shutdown and a fresh idle companion generation |
| VM (`systemd`)          | Harness transient services use the `harness` scope in `agentplane-workloads.slice`   | Execution transient services use `execution-service` in `agentplane-executors.slice`; prior units and native domains must be fenced before replacement           |

Abnormal Pod Runtime exit requires the outer container fence. VM native units bind to
`agentplane-environment-runtime.service` and `/run/agentplane-environment-runtime-lifetime` with its
exact invocation. Runtime's management scope identity remains separate from harness/Execution scope
IDs. Every scope identity includes the state directory, Sandbox UID and explicit scope ID.
Runtime declares volatile COSI systemd scopes only after opening its lifecycle store. The scope
controller prepares the invocation marker, applies the aggregate budget and inventories/fences stale
units before publishing readiness. Each process waits for its own scope's ready status. Readiness is
latched for that owner lifetime: controller restarts and Execution retries cannot fence its live units.
The Runtime unit holds state/workspace mounts until native cleanup and accepted bootstrap
settle; its stop timeout is unlimited, while the lifecycle fence deadline is 150 seconds.

Systemd supervision retains exact invocation IDs and cgroup paths until native fencing is proven.
A stopped loaded unit with cleared `ControlGroup`, no main/control PID and no pending job supplies
manager cleanup evidence; a previous `InvocationID` may remain. A retained cgroup path requires
recursive kernel emptiness. Stop acceptance/job completion alone cannot authorize replacement.
Missing properties, changed invocation, manager disconnect and uncertain starts keep admission
closed. Process observations do not replace this proof.

Each process receives its immutable non-negative `StopGrace` at launch. Systemd applies it as
`TimeoutStopUSec`; PID1 supervision applies the same TERM-to-KILL policy. Native harnesses
retain five seconds. Execution permits 90 seconds to fence its backend; its supervised VM process
retains five-second systemd stop grace. Standalone Execution retains its own supervisor.

## Configuration

`agentplane-environment-runtime --config <environment-runtime.json> --launch-file <launch.json>`
requires explicit supervision: `pid1` for a Pod PID namespace or `systemd` with a VM launcher. Runtime constructs per-incarnation Execution config
in memory. The Pod execution companion retains its separate external ownership socket.

VM service configuration is mounted at `/run/agentplane/config/environment-runtime.json`, with
launch identity at `/run/agentplane/sandbox/launch.json`. `--vm-resource-properties` remains the
systemd prestart interface for calculating the resource envelope. Native protocol probing and the
live Execution owner determine readiness independently of a previously published observation.
