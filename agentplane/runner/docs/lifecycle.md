# Runner lifecycle ownership

This describes the lifecycle controllers and their native operation hosts in this package. COSI Runtime schedules in-process work; the existing Session store, journals, ingress
acknowledgements, and state-owner lock remain the durable authorities.

## State and owners

Each Runner creates one namespaced, in-memory COSI runtime. The runner and COSI use the same Zap
logger, configured by the executable for JSON on stderr. Its resources contain comparable intent
and progress values. They do not contain process handles, adapters, cancellation functions, journal
connections, or other native state.

| Resource or effect                       | Publisher or owner                             | Meaning                                                                                                 |
| ---------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Runner desired state and input           | Runner host                                    | Lifecycle mode, incarnation seal UID, generation, and active admission count.                           |
| Runner runtime                           | Runner lifecycle controller                    | Runner-wide stage and terminal controller error.                                                        |
| Session input                            | Runner/Session host                            | Demand and snapshots of journal cursor, setup, process, active turn, and queued or running inspections. |
| Session runtime                          | Session QController                            | Execution phase, attempt, resume flag, process incarnation, and desired-state generation.               |
| Codex exec-server input and runtime      | Exec-server host and QController               | Demand, process intent, readiness/health deadlines, restart backoff and stop outcome.                   |
| Process desired state                    | Requesting lifecycle controller                | Immutable owner, generation, start deadline and cleanup timeout for one launch.                         |
| Process status                           | Process QController                            | Launch, native exit, stopping and completed cleanup; a finalizer protects each execution domain.        |
| Process input                            | Process host                                   | Revision-only wakeups for asynchronous launch, exit and stop observations.                              |
| Codex app-server input and runtime       | App-server host and QController                | Shared app-server demand, exec-server dependency deadline, process incarnation and stop outcome.        |
| Bootstrap input and runtime              | Runner host and Bootstrap QController          | Accepted one-shot bootstrap demand and its running or terminal outcome.                                 |
| Operation result                         | Runner/Session host                            | Wakes the owning controller when an asynchronous host operation completes.                              |
| Journal seal and ingress acknowledgement | Runner/transport host                          | In-memory mirrors of the durable journal seal and persisted ingress acknowledgement.                    |
| Native process and journal effects       | Runner, Session, Codex app-server, and journal | Startup, dispatch, interruption, process exit, inspection, sealing, and persistence.                    |

Controller roles cover Runner, Session, native process lifetime, Codex exec-server, Codex app-server, and Sandbox bootstrap.
The renamed resource kinds are `RunnerDesiredStates.agentplane`,
`CodexExecServerInputs.agentplane`, `CodexExecServerRuntimes.agentplane`,
`CodexAppServerInputs.agentplane`, and `CodexAppServerRuntimes.agentplane`.
The process controller names are `codex-exec-server-lifecycle` and
`codex-app-server-lifecycle`, matching the upstream `codex exec-server` and
`codex app-server` commands. Each controller exclusively writes its own runtime or status resource. Requesting controllers share the process desired-state output kind and own their individual attempt resources. The Session QController can reconcile up to
`session_reconcile_concurrency` Sessions at once (default 16); this controls reconcile workers,
not how many Sessions can be admitted or stored. Shared service controllers use one reconcile worker;
process attempts are keyed independently. Hosts publish input changes, process observations and
operation results, while stdout and transport workers remain independent
so native replies and ingress acknowledgements can make progress while a controller is waiting.
Operation slots stay owned until their result is consumed, preventing controller retries from
repeating an in-flight native effect.

COSI state is disposable. It is reconstructed for each Runner from the existing state directory,
Session records, journals, lameduck marker, seals, and persisted acknowledgements. Session and Runner
intent resources live for the runtime lifetime; completed operation-result resources are removed
only after their controller consumes the result. Process attempts have individual lifetimes: the
process controller adds a finalizer before launch and removes it only after startup has settled and
the native execution domain is fenced. Failed fencing retains the finalizer and blocks a successor.
`Close` stops controllers and joins their workers before the runtime and its in-memory state are discarded.

## COSI conventions and package structure

The package keeps runtime wiring, typed resources, host input publication, and asynchronous
operation ownership in `lifecycle_runtime.go`, `lifecycle_resources.go`, `lifecycle_inputs.go`,
and `operations.go`. Controllers have their own `*_controller.go` files, including resource specs
where specific to that controller. `process_controller.go` and `process_host.go` own one-shot
native lifetime and its asynchronous observations. Launch callbacks and native handles stay in the
host registry; COSI resources contain no streams or environment values. Admission and status have
separate files.

Resources use upstream [`typed.Resource`](https://github.com/cosi-project/runtime/blob/v1.16.2/pkg/resource/typed/typed_resource.go).
Their Go spec determines their resource kind and namespace, so reads and writes cannot accidentally
pair a spec with another kind. Specs contain only comparable values; `DeepCopy` is a value copy.
The upstream [`safe` helpers](https://github.com/cosi-project/runtime/tree/v1.16.2/pkg/safe)
provide typed reads, conflict-safe modification, and resource watches. Controller writes go through
the controller runtime to enforce exclusive output ownership. Host input publication uses the
explicit `runner-host` owner. Resource-only waits use COSI watches; predicates spanning native
handles and journals retain the Runner observation notification.

QControllers declare primary inputs, mapped dependencies, exclusive outputs, and concurrency.
Deadlines use COSI requeue scheduling rather than sleeping inside reconciliation. Operation results
name their consuming controller explicitly, rather than deriving routing from operation names.
Native operation slots remain claimed through completion publication and until consumption;
completion does not replay an effect or substitute for durable journal evidence. A process launch
identity is immutable and starts at most once. A terminal process status stays terminal through
reconciliation; restart policy creates a new generation explicitly.

The upstream [`task.Runner`](https://github.com/cosi-project/runtime/tree/v1.16.2/pkg/task)
restarts failed tasks. That suits restartable background services, but not native writes with
uncertain outcomes, so native effects keep the owned one-shot operation bridge. `qtransform` is
useful for direct resource transformations; these state machines instead coordinate several inputs
and asynchronous effects. Individual process attempts use resource teardown and finalizers for
the cleanup barrier; Session and Runner inputs live for the runtime lifetime.

These in-memory resources need no schema code generation. Upstream protobuf resource specs and
registration become useful if resources later need persistence or a remote COSI API; adding that
serialization now would duplicate the existing durable journal contract. COSI's default expvar
controller and queue counters are available for future diagnostics; this runner does not add an
HTTP metrics endpoint.

## Lifecycle transitions

Runner desired modes are `Running`, `Quiescing`, and `Closing`. The Runner controller advances progress
through `Running`, `Draining`, `Halting`, `Stopping`, `Fencing`, `Sealing`, `Flushing`, and `Quiesced`, or to
`Closed` for orderly close. Session execution is tracked separately as `Dormant`, `Preparing`,
`Ready`, `Stopping`, or `Halted`. One-shot setup remains its own durable state and is not inferred
from execution readiness.

New execution demand advances a dormant Session into one preparation attempt. Its operation records
whether it is resuming and which process incarnation it prepares. The controller accepts completion
only while the desired state still permits execution and that process remains live. Dispatch and inspection
also have Session-owned operation slots. The Session controller starts accepted setup exactly once
and joins it during halt; the Bootstrap controller owns the Sandbox-wide one-shot script. Their
waiters only observe completion and cannot cancel the accepted work. Pending and in-flight inspection both block later ordinary
dispatch; inspection runs away from stdout so it can wait for native history replies.

## Codex exec-server and app-server

The Codex exec-server controller creates one process intent on demand, probes readiness, and
schedules health checks and bounded restart backoff with COSI requeues. The shared process
controller owns launch, exit observation and cleanup through the existing supervision or Pod
container-ownership adapter. Three consecutive health failures request process teardown. Native
exit also triggers cleanup; a successor requires a new intent after the old attempt is fenced.
Start/stop callbacks are never retried by reconciliation. A failed fence retains its intent and
finalizer and fails Runner shutdown. Probe operations remain with the exec-server controller.

The Codex app-server controller waits for an exec-server-ready observation before starting the one app-server.
Concurrent Session requests share startup and its 30-second exec-server dependency deadline. Its host
retains the initial Session for startup-frame journaling and records process exit only after both
readers and every hosted Session's exit observations settle. Shutdown consumes the stop operation's
result before reporting Stopped, including when the process pointer was already cleared by readers.

## Quiesce and close

`Quiesce` checks the optional idle token and persists lameduck with the current incarnation UID while
holding the Runner admission barrier. New Session creation and new ordinary command IDs are then
refused. An already admitted duplicate retains its journaled result, and an exact-turn interrupt can
still be admitted. A Session reservation written before the desired state changes remains in the durable
inventory. The Runner controller waits for command-admission handlers already inside the barrier to
finish before teardown begins.

The Runner controller loads the durable Session inventory and asks each Session controller to
halt. A Session cancels and joins preparation, joins command
and control dispatch, interrupts an active turn, and waits up to 15 seconds for the turn to end.
After every Session is halted, the Session controllers stop their dedicated harnesses and the Codex app-server
controller stops the shared app-server once. The Runner then advances to Fencing, allowing the
Codex exec-server controller to fence the configured exec-server. Each stage advances from observed controller
outcomes; there is no central shutdown worker. It waits for any outstanding inspections after
process stop so process exit can resolve native RPC waiters and the resulting observations can be
committed before sealing. Bootstrap also finishes before sealing or close can complete.

For Quiesce, each journal then fails commands that were never dispatched and seals at its final
cursor under the current incarnation UID. The Runner advances to `FLUSHING` only after every seal is
published. It advances to `QUIESCED` only when the persisted ingress acknowledgement for every
Session is marked sealed and matches that journal's exact final cursor. A lower cursor or a missing
acknowledgement leaves the Runner flushing; a repeated Quiesce reports current progress.

`Close` changes the same desired state to `Closing`, so it shares halt, process-stop, inspection, and any
already requested seal work rather than starting a second teardown. If Quiesce began first, Close
preserves its seal UID. If Close began first, a concurrent Quiesce records lameduck and the seal UID
while leaving the desired state in `Closing`. Close waits until setup/bootstrap, native processes,
inspections, and required seals have settled; it then cancels the controllers, joins lifecycle and
Session workers, closes journals, and releases the state-owner lock last. Close does not wait for
ingress acknowledgement. If it supersedes Quiesce before the exact final cursors are acknowledged,
the journals remain sealed and durable, but the Runner can close while Quiesce is still reporting
`FLUSHING`.

An ordinary `Close` without a Quiesce seal stops the processes but leaves journals reopenable. On a
new authenticated incarnation, the host durably reopens journals sealed by a different incarnation
before accepting work; the same-incarnation lameduck marker continues to reject it. If a replacement
incarnation arrives while teardown is in progress, adoption waits for native cleanup and journal
seals, then resets old execution demand before publishing Running. It does not wait for the previous
incarnation's ingress acknowledgement.

## Validation and rollback

The primary automated target is `bbr test //agentplane/runner:runner_test`, which runs with the race
detector and contains the lifecycle controller cases. Validate public quiesce and status behavior
with `bbr test //agentplane/sandbox_controller:sandbox_controller_test`. For native recovery and
shared-process behavior, use `bbr test` with the affected runner targets: `//agentplane/runner:test_restart`,
`//agentplane/runner:test_turns`, `//agentplane/runner:test_initialize`,
`//agentplane/runner:test_attach`, and `//agentplane/runner:test_codex_shared_process`.
These targets are validation guidance, not a claim that they passed in this documentation change
or that a deployment was verified.

The migration does not change RPC or journal schemas. A rollback can restore the previous lifecycle
scheduler together with its callers and this implementation description; it must leave Session
records, journal contents, seals, and acknowledgements intact. Existing journal reopen rules remain
the only way to reopen a seal.
