# Standard idioms over hand-rolled mechanisms

A 2026-10-30 read-only audit of Agentplane against standard Kubernetes, gRPC, HTTP, PostgreSQL and
frontend idioms, after the custom watch-cycle freshness turned out to freeze the app's inventory.
This plan holds each accepted item's problem, target and exit;
[the task DAG](task_dag.md#16-standard-idioms-over-hand-rolled-mechanisms) holds state and
dependencies. Items the audit judged justified are not listed: the central egress proxy, the
sampler's Responses stream parser, runner supervision, Electric sync, the local command outbox, CRD
validation, the Policy Controller and gRPC health.

File references are to `devel` at the time of the audit; re-check before starting.

## PostgreSQL

### `DISPATCHER_WATCH_RESUMABLE_SERVER`

**In progress; coding agent, 2026-11-16; resumable server.** The storage prerequisite is complete;
its durable schema and retention contract live in the [Dispatcher specification](../session_dispatcher/dispatcher/SPEC.md#versioned-dispatch-watches).
This step implements versioned `WatchDispatches` reads through payload-free
`postgres/wakeups.Hub` notifications. A fresh snapshot includes current Dispatches and retained deletion
receipts in version order; resumed reads return versions after the cursor. Checkpoints use brief
PostgreSQL `SHARE` locks on `dispatch` and `dispatch_tombstone` to account for allocation/commit
reordering; that transaction reads only the indexed frontier and retention floor. Read the bounded batch
outside the lock in a read-only MVCC snapshot that rechecks retention coverage. No transaction spans
stream sends.

Requests and responses carry an opaque, versioned resume token with the last version and a digest of the
filter, but no principal binding. Every request is authorized normally. A filter mismatch returns
`INVALID_ARGUMENT`; a cursor below the retained global floor returns `OUT_OF_RANGE` with resynchronization
detail. `CaughtUp` follows the initial snapshot and each incremental read, may recur, and carries the
checkpoint frontier even when no matching record was found. Use the shared
`dispatcher/internal/resumetoken` package. When `postgres/wakeups.Hub.Generation()` changes after a listener
reconnect, rescan from the current durable cursor to cover writes during the gap.

The server step does not wait for a staging image pin: the storage migration runs through the normal
serving-binary startup lifecycle, and this step adds no migration. The client cutover remains blocked
until the operator confirms that the serving image is pinned on staging. Only then enable the App
browser watch (`app/frontend/sessions/dispatch_watch.ts`, through the generated facade) and
`action_service/internal/sessionclient/observe_input.go` with resume tokens. Clients must surface coverage loss before taking a
fresh snapshot. Update `session_dispatcher/dispatcher/testing_client.py`,
`session_dispatcher/dispatcher/store_integration_test.go`, facade/auth mocks, and
`system_tests/scenarios/{dispatcher,central_sampler}.py` with that client change. Notifications has no
`WatchDispatches` client; it consumes SubmitInput receipts and reconciles through History. The App and
`action_service/internal/sessionclient/observe_input.go` do not send resume tokens in this server change.

Continue sending chunked terminal notices for older serving replicas. **CLEANUP:** remove the chunked
terminal-notice sender and encoder only after every serving replica uses durable versioned watches and
no supported rollback image depends on that path. The rollback target is
`git.allegedly.works/ducktape-ci/agentplane-runner-gateway:devel-20261116062955-a8fc73e`. PR #16894 CI
checked compatibility of its matching embedded migration provider; it did not run the old image
binary. Keep migration `00024` installed during rollback. The old image resumes legacy watch behavior
and pauses tombstone pruning while it runs.

**Acceptance:** resume after a disconnect and server restart; replay a deleted receipt from its
tombstone; return retention-driven `OUT_OF_RANGE`; prove a checkpoint cannot skip a lower version that
commits late; verify payload-free Hub wakeups across replicas and frontier advancement with no matching
rows; preserve chunked notices for old replicas; and keep the k3s Dispatcher coverage green.

## Central sampler

### `SAMPLER_JOURNAL_DIET` (decision)

Operator to weigh in. Every sampling intent stores the full Responses request body
(`runtime.go`, `Store.Begin`) and nothing reads it beyond its kind, with no deletion, so storage grows
with turns × context. Every streamed provider event is journaled as a `BoundaryRecord` in its own
locking transaction, then again inside the intent's result and again in failure evidence. Options:
store kind plus a conversation reference and journal raw events once; batch per-event writes or keep
only item-done and terminal events; or keep everything and add retention.
