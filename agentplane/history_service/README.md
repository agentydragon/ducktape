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

During shutdown the fold stops and hands back its leases while existing ingress calls drain. Entries
already acknowledged remain committed in raw history, and a restarted fold projects any remaining
history from its checkpoint.

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

## Read and follow one Session

`FollowSession` reads raw runner entries in cursor order and can keep sending them as they commit.
Go callers use the generated `historypb.HistoryServiceClient`, as for the other History RPCs.
The browser continues following fold tables through Electric.

Each request chooses exactly one start:

- `after_cursor`: exclusive, with zero naming the beginning;
- `back { kind, count }`: includes the count-th most recent retained entry of that kind, or starts
  at the beginning if there are fewer;
- `tail`: only entries committed after the opening snapshot.

An empty `kinds` filter sends every observation. `EventKind` numbers match the fields of the runner's
`Event.observation` oneof. Ingest stores that number in `session_event.kind` (zero for no known
observation); `(session_id, kind, cursor)` indexes filtered reads and back starts. The separate kind
backfill migration must deploy only after every History replica writes the column.

`entries` and `caught_up` frames report `through_cursor`, the highest cursor covered by sent entries
or the filter. Correction frames retain the last such cursor. Resume with `after_cursor` equal to the
last received `through_cursor`: entries have no gaps or duplicates. The initial `caught_up` marks the
committed tail of its read snapshot, including when every new entry was filtered out. Later
`caught_up` frames follow cursor progress or a correction; an idle wake sends no frame. With
`follow = false`, the stream ends successfully after its initial `caught_up`. With `follow = true`,
an idle stream ends with `reconnect_required` when `watch_lease_s` expires or the replica drains;
reconnect with fresh authorization. If the lease or drain ends during a write, the stream ends
without another frame. Bare EOF before either terminal frame is a failure.

`settled` carries the same `SettledDeltas` as `ReadSessionEvents` and is sent only for an empty kind
filter or one containing `text_delta`, `tool_arguments_delta`, `tool_output_delta` or `native`; all
other kinds are retained. The settlement and its completion entry commit atomically. Follow pages
include settlements whose completion cursor is after the previous cursor and through that page's
cursor, read from the same snapshot as its entries. A page carries at most 128 settlements even
when the filter skips many completion entries. The server sends those settlements before any page
frame advances `through_cursor` past their completion. If a connection ends after a correction but
before progress, resuming from the prior cursor can repeat that correction. Settlements completed at
or before the requested starting cursor are not replayed. `removed` tells readers to discard a
deliberately deleted range; later catch-ups may report a removal below the starting cursor when
cleanup removes an already passed entry. Corrections are idempotent, and readers apply them even
when `through_cursor` does not advance. Raw cursors survive a projection epoch rebuild; the stream
neither resets nor follows the fold's epoch.

Each entries frame holds at most 128 entries or 1 MiB of encoded response, with an oversized entry
sent alone. Filtering and byte bounds are applied in the database before payloads are transferred.
The server reads a bounded entries/settlements page, waits for its frames to finish sending, then
reads the next; it holds no database transaction while sending. It reads all intersecting removal
ranges once per catch-up. Backend reads and waiting for each stream `Send` to clear transport
backpressure have `request_timeout_s` bounds. A bound does not guarantee when a queued frame reaches
the peer.
Each replica subscribes followers to the wakeup hub's
`agentplane_session_history_changes` before the first read. A payload-free notification wakes all
followers; reconnecting the LISTEN connection also wakes them to cover lost notifications.
Cleanup writers must record removed ranges and notify that channel in the same transaction.

Authorization checks `reader_accounts` on opening; the lease bounds continued reading after a
reader is revoked. Errors carry a gRPC status and fixed detail:

| Status              | Cause                                                                             |
| ------------------- | --------------------------------------------------------------------------------- |
| `NOT_FOUND`         | History never recorded the Session.                                               |
| `OUT_OF_RANGE`      | `after_cursor` exceeds the committed tail.                                        |
| `INVALID_ARGUMENT`  | Invalid Session UUID, absent start, zero back count, or unspecified/unknown kind. |
| `PERMISSION_DENIED` | The authenticated caller is outside `reader_accounts`.                            |
| `UNAUTHENTICATED`   | Missing or invalid caller credentials.                                            |

## Session writer and execution Sandbox

The History Service never routes to, reads or manages Sandboxes; it records and reports the
execution Sandbox associated with a Session. Every Sandbox runs as a ServiceAccount of its own name, so the
first push that binds a writer records the namespace, name and UID of the Sandbox which controls
that ServiceAccount (`SessionSummary.sandbox`, absent until recorded). History gets the UID from the
ServiceAccount's controller ownerReference. The nullable UID lets a later push by the same writer
fill an older record; a different UID is refused. `FindSessions` supports both name searches and
exact UID lookup, including deleted Sandboxes. Legacy rows and ServiceAccounts without a Sandbox
controller owner remain without a UID.

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
`history-v2`, from cursor 0: a page per Session per cycle under the per-Session projection leases,
so replicas share the work. The fold writes only those tables. Cycles run back to back while any
Session is behind, and committed history changes wake a caught-up fold. `fold_interval_s` remains
the retry interval after failures and while another replica holds a Session lease. Each cycle logs
how many of its leased Sessions and events it is behind. A page that carries an item's completion
carries its settlement, so the fold skips the settled cursors and drops evidence it recorded for
them before they settled. This fold is the only one: the app reads every Thread from it, and
Electric replicates its tables to browsers.

For the supported `history-v1` to `history-v2` upgrade, the leased projector clears only that
Session's derived fold rows, resets its checkpoint to cursor 0, and replays retained History events.
Other epoch transitions, including downgrades, are refused. Raw events, feed state, and sidebar
metadata remain intact. A `session` `execution_progress` observation in phase `blocked` stores its
JSON `reason` in `controls.blocked_reason`; other execution-progress observations remain silent.
Existing Sessions replay page by page, so a blocked reason is available after the fold reaches its
retained event.

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
with one `changed` to cover whatever the caller missed, and an idle stream ends with
`reconnect_required` after `watch_lease_s` or a replica drain. A lease/drain during a write ends the
stream without another frame. A request with `opened_after` also gets `session_opened` for each Session recorded at
a later position, the backlog first and then each as its first push binds it, so the app opens a
Thread for Sessions it did not start. The binding draws the Session's `opened_position` under a
transaction advisory lock held to commit, so positions become visible in ascending order and a caller
that reconnects after the last position it handled misses none.

`ReadThreadEntities` serves a window of a Session's fold rows, by entity index or by key, each as
the `ThreadEntityView` JSON browsers sync. A caller holding rows of a replaced epoch gets
`FAILED_PRECONDITION`.
`ReadEntityEvidence` pages the observation cursors an entity was folded from, and
`ReadNativeFrames` the native harness frames linked to one of them, read from the raw log.
`native_frame_read_batch_size` (default `200`, set by the YAML key or
`AGENTPLANE_HISTORY_SERVICE_NATIVE_FRAME_READ_BATCH_SIZE`) bounds the linked sequences in each raw-history
query. It is independent of the public `ReadNativeFrames` page limit of `200`: storage chunks for one request
share a repeatable-read snapshot and committed-history watermark.

Bazel targets: `:history-service` and `:service_image`. Settings come from `AGENTPLANE_HISTORY_SERVICE_*` environment
variables or the YAML file named by `AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE` (`config.go`, `settings.py`).
The read API listens on `listen_address`, where `grpc.health.v1.Health` reports process liveness only.
Both listener settings accept TCP addresses or inherited descriptors as documented by
[`util/netlistener`](../../util/netlistener/README.md).

## Optional central producers

`ingress.sampler_accounts` explicitly admits central sampler accounts (empty by default). Their
`sampler_announce` authenticates the producer separately from the executor Sandbox and keeps both
identities fixed. Native announcements cannot claim central metadata. The same cursor, exact-replay,
writer fencing and commit-before-ack rules apply. Central outboxes retain evidence in sampler
PostgreSQL while History is unavailable. Raw boundary and execution-progress observations remain
retained evidence; source-linked normalized Events drive the ordinary Thread fold. History does
not own the provider continuation. See the [sampler contract](../x/central_sampler/SPEC.md).
