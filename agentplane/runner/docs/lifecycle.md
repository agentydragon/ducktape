# Runner lifecycle ownership

This describes the lifecycle controllers and their native operation hosts in this package. COSI Runtime schedules in-process work; the existing Session store, journals, ingress
acknowledgements, and state-owner lock remain the durable authorities.

## State and owners

Each Runner creates one namespaced, in-memory COSI runtime. The runner and COSI use the same Zap
logger, configured by the executable for JSON on stderr. Its resources contain comparable intent
and progress values. They do not contain process handles, adapters, cancellation functions, journal
connections, or other native state.

| Resource or effect                       | Publisher or owner                         | Meaning                                                                                                 |
| ---------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| Runner goal and input                    | Runner host                                | Lifecycle mode, incarnation seal UID, generation, and active admission count.                           |
| Runner runtime                           | Runner lifecycle controller                | Runner-wide stage and terminal controller error.                                                        |
| Session input                            | Runner/Session host                        | Demand and snapshots of journal cursor, setup, process, active turn, and queued or running inspections. |
| Session runtime                          | Session QController                        | Execution phase, attempt, resume flag, process incarnation, and goal generation.                        |
| Executor input and runtime               | Executor host and Executor QController     | Demand, start/readiness/health deadlines, restart backoff, fencing and stop outcome.                    |
| Codex input and runtime                  | Codex host and Codex QController           | Shared app-server demand, executor dependency deadline, process incarnation and stop outcome.           |
| Bootstrap input and runtime              | Runner host and Bootstrap QController      | Accepted one-shot bootstrap demand and its running or terminal outcome.                                 |
| Operation result                         | Runner/Session host                        | Wakes the owning controller when an asynchronous host operation completes.                              |
| Journal seal and ingress acknowledgement | Runner/transport host                      | In-memory mirrors of the durable journal seal and persisted ingress acknowledgement.                    |
| Native process and journal effects       | Runner, Session, Codex server, and journal | Startup, dispatch, interruption, process exit, inspection, sealing, and persistence.                    |

There are five controller roles: Runner, Session, Executor, Codex app-server, and Sandbox bootstrap.
Each exclusively writes its own runtime resource. The Session QController can reconcile up to 16
Sessions concurrently; the other QControllers each reconcile one shared instance. The host
publishes input changes and operation results, while stdout and transport workers remain independent
so native replies and ingress acknowledgements can make progress while a controller is waiting.
Operation slots stay owned until their result is consumed, preventing controller retries from
repeating an in-flight native effect.

COSI state is disposable. It is reconstructed for each Runner from the existing state directory,
Session records, journals, lameduck marker, seals, and persisted acknowledgements. Session and Runner
intent resources live for the runtime lifetime; completed operation-result resources are removed
only after their controller consumes the result. No resources with outstanding work are deleted, so
finalizers are unnecessary. `Close` stops controllers and joins their workers before the runtime and
its in-memory state are discarded.

## Lifecycle transitions

Runner goals are `Running`, `Quiescing`, and `Closing`. The Runner controller advances progress
through `Running`, `Draining`, `Halting`, `Stopping`, `Fencing`, `Sealing`, `Flushing`, and `Quiesced`, or to
`Closed` for orderly close. Session execution is tracked separately as `Dormant`, `Preparing`,
`Ready`, `Stopping`, or `Halted`. One-shot setup remains its own durable state and is not inferred
from execution readiness.

New execution demand advances a dormant Session into one preparation attempt. Its operation records
whether it is resuming and which process incarnation it prepares. The controller accepts completion
only while the goal still permits execution and that process remains live. Dispatch and inspection
also have Session-owned operation slots. The Session controller starts accepted setup exactly once
and joins it during halt; the Bootstrap controller owns the Sandbox-wide one-shot script. Their
waiters only observe completion and cannot cancel the accepted work. Pending and in-flight inspection both block later ordinary
dispatch; inspection runs away from stdout so it can wait for native history replies.

## Shared executor and Codex process

The Executor controller starts the configured executor on demand, probes readiness, and schedules
health checks and bounded restart backoff with COSI requeues. Failed probes are observations rather
than terminal startup failures. Three consecutive health failures or a native exit cause fencing
before a successor is allowed. A failed fence remains a barrier to a new process. Native process and
container ownership helpers perform bounded operations; they do not run a second lifecycle policy loop.

The Codex controller waits for an executor-ready observation before starting the one app-server.
Concurrent Session requests share startup and its 30-second executor dependency deadline. Its host
retains the initial Session for startup-frame journaling and records process exit only after both
readers and every hosted Session's exit observations settle. Shutdown consumes the stop operation's
result before reporting Stopped, including when the process pointer was already cleared by readers.

## Quiesce and close

`Quiesce` checks the optional idle token and persists lameduck with the current incarnation UID while
holding the Runner admission barrier. New Session creation and new ordinary command IDs are then
refused. An already admitted duplicate retains its journaled result, and an exact-turn interrupt can
still be admitted. A Session reservation written before the goal changes remains in the durable
inventory. The Runner controller waits for command-admission handlers already inside the barrier to
finish before teardown begins.

The Runner controller loads the durable Session inventory and asks each Session controller to
halt. A Session cancels and joins preparation, joins command
and control dispatch, interrupts an active turn, and waits up to 15 seconds for the turn to end.
After every Session is halted, the Session controllers stop their dedicated harnesses and the Codex
controller stops the shared app-server once. The Runner then advances to Fencing, allowing the
Executor controller to fence the configured executor. Each stage advances from observed controller
outcomes; there is no central shutdown worker. It waits for any outstanding inspections after
process stop so process exit can resolve native RPC waiters and the resulting observations can be
committed before sealing. Bootstrap also finishes before sealing or close can complete.

For Quiesce, each journal then fails commands that were never dispatched and seals at its final
cursor under the current incarnation UID. The Runner advances to `FLUSHING` only after every seal is
published. It advances to `QUIESCED` only when the persisted ingress acknowledgement for every
Session is marked sealed and matches that journal's exact final cursor. A lower cursor or a missing
acknowledgement leaves the Runner flushing; a repeated Quiesce reports current progress.

`Close` changes the same goal to `Closing`, so it shares halt, process-stop, inspection, and any
already requested seal work rather than starting a second teardown. If Quiesce began first, Close
preserves its seal UID. If Close began first, a concurrent Quiesce records lameduck and the seal UID
while leaving the goal in `Closing`. Close waits until setup/bootstrap, native processes,
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
