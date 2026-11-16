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

### `DISPATCHER_WATCH_VERSIONS`

**In progress; coding agent, 2026-11-15 PST. Operator released the build on 2026-11-16 at
06:40 UTC.** PR1 covers storage only.
`WatchDispatches` currently ships deleted rows as base64 protobuf split into `pg_notify` parts
(`dispatcher/terminal.go`), reassembled with a timeout and pending cap, failing every stream on any
malformed part. It uses its own `pq.NewListener` rather than the shared `postgres/wakeups.Hub`,
re-lists each scope on any change, and has no durable replay.

PR1 adds storage groundwork only: an additive migration backfills each dispatch with a version from
a sequence, and database triggers assign a fresh version to every later insert and update, including
writes from older application images. A deletion trigger retains receipt snapshots in tombstones
keyed by command ID and version, with no Sandbox name/UID or dispatch command/routing columns;
the version keeps deletion records distinct if a command ID is reused. Retained admission evidence
may include the original command content. The retained Session row supplies ownership and read
scope. A pruner removes tombstones after 24 hours in bounded batches and
keeps a durable global pruned-through watermark. This phase does not change `WatchDispatches`, add
resume-token fields, or switch clients from the chunked notification path.

After every dispatch-writing replica has the migration-compatible release, the later receiver step
uses payload-free `postgres/wakeups.Hub` notifications to wake versioned database reads. Replay
matching dispatches and tombstones in version order where `version > cursor`, applying the existing
Session read scope to both. A sequence orders allocation, not commit: before checkpoint queries, the
reader takes brief PostgreSQL `SHARE` locks on `dispatch` and `dispatch_tombstone`, which lets any
writer that already allocated a lower version commit before the reader advances its cursor. It
releases those locks and completes its transactions before sending frames; a transaction must never
span stream sends.

The later API carries an opaque, versioned `resume_token` in requests and responses. It contains the
last dispatch version and a filter digest, but no principal binding; every request
still performs normal authorization. A token used with a different filter returns `INVALID_ARGUMENT`.
A cursor behind the retained coverage returns `OUT_OF_RANGE` with resynchronization detail; clients
must surface that coverage loss before starting a fresh snapshot.

Roll out the server and receiver before enabling clients. Enable the App browser watch
(`app/frontend/threads/dispatch_watch.ts`, through the generated facade) and
`session_client/observe_input.go` only after the serving image is pinned on staging. Update
`session_dispatcher/dispatcher/testing_client.py`,
`session_dispatcher/dispatcher/store_integration_test.go`, facade/auth mocks, and the
`system_tests/scenarios/{dispatcher,central_sampler}.py` coverage with the client change.
Notifications has no `WatchDispatches` client: it consumes SubmitInput receipts and reconciles through
History.

The currently pinned rollback image is
`git.allegedly.works/ducktape-ci/agentplane-runner-gateway:devel-20261116062955-a8fc73e`. Keep the
migration installed when rolling back; its database triggers continue stamping writes and retaining
deletions. This image does not prune tombstones or provide versioned replay, so existing watch
limitations remain. Verify that the old image can read rows written after migration before treating
this rollback as supported.

**CLEANUP:** remove the chunked notification encoder/reassembler and its timeout and pending-cap
handling only after every serving replica uses durable versioned watches, all dispatch writers have
versioned storage, and no supported rollback image depends on that path. The current rollback pin
still uses it, so this gate remains open until that pin and the replica rollout no longer require the
legacy behavior.

**Acceptance:** resume after a disconnect and server restart; replay a deleted receipt from its
tombstone; surface retention-driven `OUT_OF_RANGE` before starting a fresh snapshot; force a lower
allocated sequence value to commit after a higher one and prove the checkpoint does not skip it;
verify payload-free Hub wakeups across replicas; and keep the k3s Dispatcher coverage green.

## Central sampler

### `SAMPLER_JOURNAL_DIET` (decision)

Operator to weigh in. Every sampling intent stores the full Responses request body
(`runtime.go`, `Store.Begin`) and nothing reads it beyond its kind, with no deletion, so storage grows
with turns × context. Every streamed provider event is journaled as a `BoundaryRecord` in its own
locking transaction, then again inside the intent's result and again in failure evidence. Options:
store kind plus a conversation reference and journal raw events once; batch per-event writes or keep
only item-done and terminal events; or keep everything and add retention.
