# Runner lifecycle ownership probe

**Design only, 2026-10-29.** Source snapshot: `origin/devel` at `8a723398`. This records the
current ownership boundaries and one bounded follow-up; it does not change Runner behavior. The
implemented lifecycle contract remains in [`docs/lifecycle.md`](../docs/lifecycle.md).

## Current ownership

Runner has three distinct state layers. `RunnerDesiredStates` carries host intent (`Running`,
`Quiescing`, `Closing`), a generation, and the optional incarnation seal UID. The aggregate
`RunnerRuntimes` status records controller progress (`Draining`, `Halting`, `Stopping`, `Fencing`,
`Sealing`, `Flushing`, then `Quiesced` or `Closed`). Each Session separately moves through
`Dormant`, `Preparing`, `Ready`, `Stopping`, and `Halted`; setup has its own durable outcome. These
are separate authorities, not a single shared state machine ([mode and status types](../lifecycle/contracts.go#L25),
[aggregate reconciliation](../runner_controller.go#L47), [Session reconciliation](../session/controller.go#L60)).

Quiesce persists lameduck and the current incarnation UID under `Runner.mu`, then publishes
`Quiescing`. Close first sets its local close latch under the same mutex, and its cleanup goroutine
publishes `Closing`. Both wait for admission handlers, halt Sessions, stop their processes and the
shared Codex app-server, drain executor relays, fence the Codex exec-server, finish bootstrap, and
wait for process finalizers and inspections. With a seal UID, journals seal after those barriers.
Quiesce then waits for exact sealed-cursor ingress acknowledgements; Close goes to `Closed` without
waiting for ingress. If Close follows Quiesce it preserves that seal UID. Incarnation adoption waits
for old native cleanup and seals, resets execution demand while admission stays closed, then reopens
Sessions and publishes `Running` ([Quiesce and adoption](../runner.go#L275), [Quiesce](../runner.go#L350),
[Close](../runner.go#L390), [stage order](../runner_controller.go#L68)).

| COSI state                                                                      | Publisher or exclusive writer               | Authority                                                                                                                      |
| ------------------------------------------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Runner desired state, admission count, journal-seal and ingress-ack mirrors     | Runner host (`runner-host`)                 | Synchronous host intent and observations; durable Session records, journals, seals, and acknowledgements remain authoritative. |
| Runner aggregate status                                                         | `runner-lifecycle` controller               | First unmet shutdown predicate and current stage.                                                                              |
| Session input and inspection-drain input                                        | `session.Manager` host (`runner-host`)      | Demand and observation dependencies; not Session identity or journal history.                                                  |
| Session runtime                                                                 | `session-lifecycle` controller              | Session execution and teardown phase.                                                                                          |
| Bootstrap, Codex app-server, and Codex exec-server inputs and operation results | Owning component host                       | Accepted demand and asynchronous result notification.                                                                          |
| Those components' runtime resources                                             | Their named lifecycle controllers           | Reconciled component phase and outcome.                                                                                        |
| Process desired state and host input revision                                   | Requesting controller or `supervision-host` | Immutable launch intent and asynchronous wakeup.                                                                               |
| Process status                                                                  | `process-lifecycle` controller              | Launch, exit, stop, and cleanup observation; the process finalizer blocks successors until fencing completes.                  |

The Runner and component input publishers use typed COSI resources; each runtime is an exclusive
controller output. Process status has its own exclusive controller. COSI is disposable and carries
no process handles, journal connections, or cancellation functions ([Runner resources](../lifecycle_resources.go#L17),
[component owners](../session/resources.go#L17), [process resources](../supervision/process_resources.go#L15),
[controller registrations](../lifecycle_runtime.go#L95)).

## Admission, cancellation, and joins

`Runner.mu` is the synchronous admission barrier. New Session reservation takes `createMu`, performs
model resolution outside the barrier, then under `Runner.mu` rechecks admission, increments the COSI
admission count, and writes the durable record before unlocking. Command handlers increment that
count under the same lock before store/journal work and decrement it when settled. Thus the count is
in-flight admission handlers, not queued commands or active turns. Quiesce and Close publish their
mode under this lock; the final `Closed` write also holds it while checking the count is zero and the
desired generation is unchanged. Exact-turn interrupt and duplicate-command behavior remain
Session/journal decisions ([Session opening](../admission.go#L24), [command admission](../admission.go#L120),
[admission accounting](../lifecycle_inputs.go#L47), [final close barrier](../runner_controller.go#L291)).

The Runner lifetime context owns accepted work. Quiesce and Close do not cancel it to initiate
teardown. Session halt cancels preparation, joins dispatch and setup, waits for admitted controls,
then interrupts an active turn and allows up to 15 seconds for its journaled completion. Session
process stop and journal sealing occur in later controller stages. Bootstrap, exec-server probes,
app-server initialization/drain, generic process launch/observation/stop, and Session operations
register with the shared lifecycle worker barrier. `Close` cancels the Runner context only after
aggregate `Closed`, waits for the engine to exit, joins shared workers, closes Session journals and
the bootstrap log, and releases the retained state-owner lock last. Native stdout/stderr readers
have a connection-local wait group; Session process-stop reconciliation calls `connection.join()`
before reporting `ProcessStopped`, which aggregate shutdown requires. `session.Manager.Close` then
joins its operation workers before closing journals. A caller cancelling its `Close(ctx)` wait does
not cancel shared cleanup ([Close and joins](../runner.go#L392),
[operation worker ownership](../operation/operation.go#L45), [Session halt](../session/session.go#L253),
[Session close](../session/manager.go#L261)).

The ordering keeps callbacks and lock ownership explicit. Executor relay admission takes
`Runner.mu` before the relay registry mutex. Relay notifications call `Runner.changed`, which takes
`Runner.mu`, only after both locks are released. Component input publishers snapshot host state,
release the component mutex, and then publish; controller runtime-change callbacks likewise happen
after their COSI write. The aggregate controller drains relay channels before it publishes the stage
that permits exec-server fencing. `Run` keeps control and ingress serving until local Close settles,
then cancels and joins those transport workers ([relay admission](../runner.go#L190),
[relay drain](../executorrelay/manager.go#L96), [transport shutdown](../transport.go#L44)).

## Recommended next simplification

Keep the local `Runner.closing` latch. It rejects new control work synchronously before COSI
reconciliation catches up, and it remains fail-closed if publishing `Closing` fails. Do not collapse
it into the aggregate runtime or move lifecycle coordination into another shared package.

Remove the aggregate status read and `Closed` check from `Runner.admit`. That method already rejects
`r.closing` under `Runner.mu` before reading desired state or status. `Close` sets the latch under
that same mutex before the only production path can
publish `Desired.Mode == Closing`; `Quiesce` preserves `Closing` only after that path has run. The
`Closed` branch is therefore unreachable for a normally owned Runner, while the desired-mode read
must remain because it supplies Quiesce's lameduck policy. This removes one COSI read from the
command-admission critical section without changing the local latch or other admission policies
([close latch](../runner.go#L392), [desired-state writer](../lifecycle_runtime.go#L152),
[current redundant branch](../admission.go#L132), [desired-state owner](../lifecycle/contracts.go#L154)).

There is one decision before implementation: `lifecycle.status()` also calls `Runner.fail` when its
COSI read errors ([failure path](../lifecycle_runtime.go#L216)). Removing the read would stop command admission from serving as an incidental
aggregate-status health check. I recommend that split: the local latch and desired mode decide
admission, while aggregate status errors belong to explicit status and shutdown observation paths.
If command admission is meant to detect that failure, keep the observation under an explicit health
check name and remove only the unreachable `Closed` predicate.

The change is gated on an explicit invariant test: hold the Runner admission mutex across the close
transition and prove that a handler which committed its admission count is drained, while a later
command is rejected before durable admission, including when desired-state publication fails. Also
pin the chosen behavior for aggregate-status read failure. Keep the existing close/interleaving
coverage and run `bbr test //agentplane/runner:runner_test`; that target uses the race detector
([existing Close/admission test](../lifecycle_test.go#L358),
[interleaving test](../lifecycle_interleavings_test.go#L38), [race target](../README.md#tests)).

The review decision is tracked as [`RUNNER_ADMISSION_STATUS_DESIGN`](../../plans/task_dag.md#runner_admission_status_design).
