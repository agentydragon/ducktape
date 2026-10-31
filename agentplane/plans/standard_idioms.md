# Standard idioms over hand-rolled mechanisms

A 2026-10-30 read-only audit of Agentplane against standard Kubernetes, gRPC, HTTP, PostgreSQL and
frontend idioms, after the custom watch-cycle freshness turned out to freeze the app's inventory
(`KUBERNETES_NATIVE_WATCH_HEALTH`). This plan holds each accepted item's problem, target and exit;
[the task DAG](task_dag.md#16-standard-idioms-over-hand-rolled-mechanisms) holds state and
dependencies. Items the audit judged justified are not listed: the central egress proxy, the
sampler's Responses stream parser, runner supervision, Electric sync, the local command outbox, CRD
validation, the Policy Controller and gRPC health.

File references are to `devel` at the time of the audit; re-check before starting.

## Work queues

Hand-rolled full-scan loops where a keyed, rate-limited work queue (client-go `workqueue`, or a
controller-runtime Reconciler) or a PostgreSQL dirty-row queue claimed with `SKIP LOCKED` is the
standard shape.

### `DISPATCHER_KEYED_DELIVERY`

Blocked on the [merge design](dispatcher_gateway_merge.md). The Dispatcher turns any Sandbox event into
one signal and on each one, and every 10 s, lists all Sandboxes, scans pending work and acquires a
Lease per busy Sandbox (`runner_gateway/dispatcher/delivery.go`), detecting deletions by diffing UIDs.
In the merged service, key delivery by Sandbox on a work queue fed by informer events.

## PostgreSQL

### `DISPATCHER_WATCH_VERSIONS`

Blocked on the [merge design](dispatcher_gateway_merge.md). `WatchDispatches` ships deleted rows as
base64 protobuf split into `pg_notify` parts (`dispatcher/terminal.go`), reassembled with a timeout
and a pending cap, failing every stream on any malformed part, with no replay, through its own
`pq.NewListener` rather than the shared `postgres/go/wakeups.Hub`; every stream re-lists its scope on
any change. Use a version column bumped from a sequence on every write, short-lived tombstones keyed
by command ID with no Sandbox address (which satisfies "no row addresses a deleted Sandbox UID"),
payload-free notifications through `wakeups.Hub`, and `WHERE version > last` resumption.

## Central sampler

### `SAMPLER_JOURNAL_DIET` (decision)

Operator to weigh in. Every sampling intent stores the full Responses request body
(`runtime.go`, `Store.Begin`) and nothing reads it beyond its kind, with no deletion, so storage grows
with turns × context. Every streamed provider event is journaled as a `BoundaryRecord` in its own
locking transaction, then again inside the intent's result and again in failure evidence. Options:
store kind plus a conversation reference and journal raw events once; batch per-event writes or keep
only item-done and terminal events; or keep everything and add retention.
