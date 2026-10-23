# History Service

Serves retained Session history: the raw runner Event log and its observation metadata, keyed by
public Session ID. It authenticates callers by projected ServiceAccount token (TokenReview) and
serves only the configured reader accounts, today the integration app's and the Notification
Service's. A Session UUID by itself
grants nothing. Consumers use `client.py`; `testing/go_service.py` runs the built Go service over a test
database for their tests, which read the fold's tables through `fold/models.py` and validate the rows
browsers sync with `fold/views.py`.

This service's `migrate` init container runs the history tables' migrations (`migrations/`, Goose). Its
runner ingress and its fold are their only writers.

## While the History Service is down or rolling

Runners keep their harnesses running and every journal entry on their state volume, and push the
backlog once the ingress answers again; nothing is lost while the History Service is away, only
late. Sender-facing commands remain in the Session Dispatcher and reach runners through the Gateway's
runner proxy; the Runner Gateway does not call this service.
What waits: the app's Thread reads and command retries, Notification Service deliveries, and
Sandbox teardown, which deletes nothing until the ingress has acknowledged every seal. Each of those
services' README says how. The Action Service, egress proxy, Policy Controller and LLM ingress do
not call it.

## Runner ingress

`ingress/` serves `RunnerIngress.Push`, the
[runner ingress contract](../docs/runner_control.md#event-ingress): runners push each Session's
journal here, one call per Session. `main.go` serves it on its own listener (`ingress.listen_address`), apart
from the read API, since it takes untrusted input: the network fence admits the egress proxy to that
port alone. Its settings name the token audience and the namespaces whose ServiceAccounts may push.
A Sandbox reaches it only through the egress proxy under the `runner-ingress` policy
(`cluster/cdk8s/agentplane/history_service.py`), which substitutes the Pod's token for the
`agentplane-runner-ingress` audience.

The first accepted announcement binds the Session to the pushing ServiceAccount (namespace, name,
UID) in `session_history`; other accounts get `PERMISSION_DENIED`. Entries commit through
`Store.Append`, which accepts exact replays and refuses gaps and conflicting entries, and each
acknowledgement follows its commit. The ingress queues at most 16 decoded frames and commits
at most 128 entries or 1 MiB of serialized entries per transaction (one oversized entry forms its
own transaction); a full queue applies backpressure to the stream.

## Which Sandbox wrote a Session

The History Service never routes to, reads or manages Sandboxes; it records and reports the
Sandbox a Session was written by. Every Sandbox runs as a ServiceAccount of its own name, so the
first push that binds a writer also records its namespace and name as the Session's Sandbox
(`SessionSummary.sandbox`, absent until recorded). `FindSessions` lists the Sessions whose Sandbox
name equals or contains given text, deleted Sandboxes included. Sandbox UIDs are not recorded.

A Session belongs to one Sandbox for its whole life and never moves to another; its history outlives the Sandbox. Replacing a Pod or VM keeps the Sandbox and its Sessions, and a teardown seal ends that incarnation, not the Session.

## Delta settlement

Each `Store.Append` batch that completes an item drops that
item's streamed delta entries (native frames and their derived deltas) from `session_event`, in
the same transaction, when every frame matches an exact versioned template
(`storage/delta_templates.go`) and the retained completion holds exactly their
concatenation. Anything else in the span keeps every entry. The dropped cursors are recorded as a
`SettledDeltas` with its ranges, and read pages report the settlements that cover or complete in
them; the fold and the app's raw-history readers treat a settled cursor as absent. The runner
journal keeps every entry. On by default; setting `ingress.settle_deltas` to false keeps every
stored entry, for debugging. Why and what it drops:
[delta settlement ADR](../docs/adr_delta_settlement.md).

## Removed ranges

A cleanup migration that deletes entries from `session_event` records their cursors in
`session_event_removed_range` and keeps nothing of their content, so a reader can tell a removed
cursor from a missing one. Read pages report the ranges (`removed`) that intersect them, and
observation pages fold them into `settled`; readers treat both as covered, and the fold treats
them as cursors with no entry. Deviation from a settlement: a removed range may end the committed
prefix, so a page that returns fewer entries than its limit reports the ranges up to
`last_cursor`, and the fold advances its checkpoint over such a suffix. A runner replaying a
journal that still holds a removed entry is accepted without storing it again.

## Thread fold

A retained authenticated runner attachment can establish a view at cursor zero before any native
Event. The empty view has no Event source or applied controls; the first real Event binds its
source, and subsequent Events must match. A writer binding or catalog entry without an attachment
does not establish a view. This lets the composer submit the first input without starting a
harness merely to render the Thread. Existing/nonempty projections still require their verified
Event prefix.

Every replica folds every retained Session into the thread projection
(`threadfold/` computes it, `projection/` runs it; `thread_*` tables, since a Thread is the projection of a
Session: [layering](../docs/thread_layering.md#authorities-and-representations)), in epoch
`history-v1`, from cursor 0: a page per Session per cycle under the per-Session projection leases,
so replicas share the work. The fold writes only those tables. Cycles run back to back while any
Session is behind, and committed history changes wake a caught-up fold. `fold_interval_s` remains
the retry interval after failures and while another replica holds a Session lease. Each cycle logs
how many of its leased Sessions and events it is behind. A page that carries an item's completion
carries its settlement, so the fold skips the settled cursors and drops evidence it recorded for
them before they settled. This fold is the only one: the app reads every Thread from it, and
Electric replicates its tables to browsers.

A Session whose history this fold cannot apply, or whose page hits a lost lease or a database
error, is logged and retried the next cycle while the others advance. Any other error stops the
replica's fold but not its raw reads: it is logged, and the `grpc.health.v1` service
`agentplane.history_service.Fold` turns from SERVING to NOT_SERVING. Probes check only the whole
server; a `Health/Check` for that service on a replica's gRPC port shows it.

Session summaries (`GetSessionSummary`, and `BatchGetSessionSummaries` for up to 200 named Sessions
at once, leaving out any it has no history for) read that fold and report the committed cursor beside
the folded one.

`WatchSessionSummaries` tells a reader when to re-read: each fold commit NOTIFYs the history
database, and every replica forwards that to its watchers as a payload-free `changed`. A stream opens
with one `changed` to cover whatever the caller missed, and ends with `reconnect_required` after
`watch_lease_s`. A request with `opened_after` also gets `session_opened` for each Session recorded at
a later position, the backlog first and then each as its first push binds it, so the app opens a
Thread for Sessions it did not start. The binding draws the Session's `opened_position` under a
transaction advisory lock held to commit, so positions become visible in ascending order and a caller
that reconnects after the last position it handled misses none.

`ReadThreadEntities` serves a window of a Session's fold rows, by entity index or by key, each as
the `ThreadEntityView` JSON browsers sync. A caller holding rows of a replaced epoch gets
`FAILED_PRECONDITION`.
`ReadEntityEvidence` pages the observation cursors an entity was folded from, and
`ReadNativeFrames` the native harness frames linked to one of them, read from the raw log.

Bazel targets: `:history-service` and `:service_image`. Settings come from `AGENTPLANE_HISTORY_SERVICE_*` environment
variables or the YAML file named by `AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE` (`config.go`, `settings.py`).
The read API listens on `listen_address`, where `grpc.health.v1.Health` reports process liveness only.
Both listener settings accept TCP addresses or inherited descriptors as documented by
[`util/netlistener`](../../util/netlistener/README.md).
