# History Service

Serves retained Session history: the raw runner Event log and its observation metadata, keyed by
public Session ID. Readers authenticate with a projected ServiceAccount token (TokenReview),
and can read only Sessions whose retained execution principal has the same
ServiceAccount namespace, name and UID. This read authority does not depend on a live Sandbox.
Configured backend readers retain their explicit Pod-bound, service-wide read path; a ServiceAccount
token alone does not grant it. A Session UUID by itself grants nothing. Consumers use `client.py`;
`testing/go_service.py` runs the built Go service over a test database. Tests can inspect the fold's
storage through `fold/models.py` and use the generated Thread entity messages for semantic reads.

An optional, separate `delegation` setting names the Actions actor, caller namespaces, whether it may
assert Identities (`identity_callers`, which requires `policy_namespace`) and exact full History RPC
method names that admit delegated reads. The shared workload-auth middleware rechecks the asserted
caller's current ServiceAccount or Identity UID; a delegated Identity reads only the Sessions recorded
with that incarnation, as its own workload does. Delegated Actions calls retain ordinary execution-principal
read scope even if the actor is also a configured backend reader; actor Pod proof does not widen the read.
The receiver never looks up Actions or stores its credentials. Watch and follow leases still require a new
admission when the client reconnects.

The service binary embeds Goose migrations and applies them during normal startup before serving
or starting the fold. `history-service --migrate-only` initializes fixture databases using the same
lifecycle. Its runner ingress and fold are the runtime writers.
The `migration_timeout_s` setting defaults to 600 seconds and includes advisory-lock waiting;
the startup probe must also cover connection retry and serving startup. Migration-only mode accepts
the same setting through `AGENTPLANE_HISTORY_SERVICE_MIGRATION_TIMEOUT_S` without serving settings.

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

`ingress/` serves `SessionJournalIngress.Push`, the
[runner ingress contract](../docs/runner_control.md#event-ingress): runners push each Session's
journal here, one call per Session. Sandbox Runtime opens end-to-end mTLS through the sidecar
and central proxy's exact-authority CONNECT passthrough to `rpc.listen_address`.
History presents `spiffe://agentplane/history-ingress`, verifies the client SVID against the
Agentplane workload CA with an explicit clientAuth EKU, and resolves its Sandbox UID and accepted
credential generation from live Sandbox status. The caller remains that Sandbox's controller-owned
ServiceAccount, including its canonical UID. A deleted Sandbox, stale credential or missing bound
Pod is refused before the ingress handler. No header selects the caller and the proxy asserts none.
The runner-ingress projected-token audience is absent from Sandbox templates and proxy audiences.

`rpc` configures the shared native gRPC listener, its server TLS files and explicit authentication modes.
With SPIFFE enabled, `Push` admits Sandbox SVIDs and configured sampler bearer accounts; bearer-only
listeners retain namespace-scoped writer admission. A ServiceAccount reads only with a bearer of the read
audience, never with an SVID. Presenting a certificate together with bearer or delegation metadata is refused.
The first accepted announcement binds the Session to the pushing ServiceAccount (namespace, name,
UID) in `session_history`; other accounts get `PERMISSION_DENIED`.

With the optional `policy_namespace` set, `Push` also admits a Sandbox SVID whose WorkloadCredential
names an Identity in that namespace (absent, such a writer is refused). The record must be in
`ingress.writer_namespaces` and controlled by a Sandbox: the Session binds to the Identity
incarnation (name, UID) and is pinned to that Sandbox, so a successor Identity of the same name, the
same Identity running another Sandbox, or a ServiceAccount writer is refused, and an Identity writer
cannot take a ServiceAccount-bound Session. The read API admits a Sandbox SVID naming an Identity
and serves it exactly the Sessions retained as executing as that Identity incarnation. An Identity runner's announced `execution_identity` must
be its own Identity, and its `execution_principal`, when present, the ServiceAccount its Sandbox
controls. The writer kinds are exclusive in `session_history` (`history_writer_one_kind`), so a
replica predating Identity writers cannot bind a ServiceAccount over one. Entries commit through
`Store.Append`, which accepts exact replays and refuses gaps and conflicting entries, and each
acknowledgement follows its commit. The ingress queues at most 16 decoded frames and commits
at most 128 entries or 1 MiB of serialized entries per transaction (one oversized entry forms its
own transaction); a full queue applies backpressure to the stream.

An admitted Push stream does not retain writer authority for its lifetime. Before binding or
reopening a journal, each durable entry batch or attached-snapshot write, and the final seal write,
History revalidates the original proof through shared workload authentication and requires the same
caller. An idle acknowledgement and a heartbeat with no pending batch also revalidate, so an idle
connection cannot keep a revoked credential alive indefinitely. If an accepted credential is cleared
or its Sandbox is removed, the next such check ends the stream before that write; a still-accepted
credential remains valid while its Sandbox is finalizing so Runtime can record its final seal.
Revocation is observed at these checks: a Sandbox status change racing a check and its immediately
following storage transaction can still let that transaction finish, while every later write boundary
checks again. Previously committed entries remain durable when a stream is refused.

## Read and follow one Session

`FollowSession` reads raw runner entries in cursor order and can keep sending them as they commit.
Go callers use the generated `historypb.HistoryServiceClient`, as for the other History RPCs.
The browser continues following fold tables through Electric. Unary reads used by the browser also
have generated ProtoJSON HTTP bindings (`/v1/sessions/...`); the integration app forwards those
same-origin requests to this service with its projected History token.

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

Normal read scope is applied in SQL before pagination, projection, or evidence lookup. It covers
summary reads and batches, Sandbox discovery, raw event and observation pages, Thread entities,
entity evidence, native frame lookup, follows and their removed ranges, and opened-Session watch
events. Normal watch positions are ordinals over the caller's retained Sessions, so a cursor does
not reveal gaps from another principal; configured backend readers retain the existing service-wide
opening cursor. Each RPC is admitted by the workload-auth resolver, whose accepted TokenReview verdict is
cached only for its configured short TTL. Watch/follow streams end at their lease so continued reads
require a newly authorized call. Configured `reader_accounts` remain restricted to their actual
Pod-bound proof for the backend-wide policy. Errors carry a gRPC status and fixed detail:

| Status              | Cause                                                                             |
| ------------------- | --------------------------------------------------------------------------------- |
| `NOT_FOUND`         | The Session is missing or outside the caller's execution-principal scope.         |
| `OUT_OF_RANGE`      | `after_cursor` exceeds the committed tail.                                        |
| `INVALID_ARGUMENT`  | Invalid Session UUID, absent start, zero back count, or unspecified/unknown kind. |
| `PERMISSION_DENIED` | The RPC method is not admitted by this service's method policy.                   |
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

An Identity writer's Sandbox is the controller owner of its WorkloadCredential.

A Session belongs to one Sandbox for its whole life and never moves to another; its history outlives the Sandbox. A nullable retained execution Identity (`Attached.execution_identity`, the Identity incarnation the Dispatcher recorded) is retained from an Identity runner running as it or a configured central sampler relaying it, never from a ServiceAccount runner, which cannot vouch for it; it is immutable like the execution principal, and a read scoped to an Identity incarnation lists exactly the Sessions retained as executing as it, whether or not their Sandbox still exists. The read API admits an Identity SVID (with `policy_namespace` set) and scopes it to that Identity incarnation. Separately, a nullable retained execution principal records the exact ServiceAccount incarnation under which the Session runs. New Dispatcher Session records carry that identity through runner and central-sampler admission into History. It is independent of the authenticated journal writer, creator, and Sandbox identity. The first trusted association is immutable: conflicting owners fail, and an absent legacy owner is never inferred from the Sandbox name or current ServiceAccount. Old announcements without the identity cannot erase a pinned owner; old rows and announcements without trustworthy identity remain unavailable to normal same-principal reads. Rolling back the service leaves these additive owner columns in place because the association cannot be reconstructed. Replacing a Pod or VM keeps the Sandbox and its Sessions, and a teardown seal ends that incarnation, not the Session.

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
`history-v1`, from cursor 0. A `session_history` trigger queues a Session in `session_fold_pending`
when it is created or its committed prefix or feed state changes, so writers of any release mark it;
each cycle claims the queued Sessions no other replica leases (`FOR UPDATE SKIP LOCKED`, then the
per-Session projection lease) and folds a page of each, so work per cycle follows the Sessions that
changed. A Session leaves the queue, and its lease is handed back, once a fold catches it up with no
newer change. The fold writes only those tables and the queue. Cycles run back to back while any
Session is behind, and committed history changes wake a caught-up fold. `fold_interval_s` remains
the retry interval after failures and while another replica holds a Session lease. Each cycle logs
how many Sessions it claimed and how many Sessions and events it left behind. A page that carries an item's completion
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
with one `changed` to cover whatever the caller missed, and an idle stream ends with
`reconnect_required` after `watch_lease_s` or a replica drain. A lease/drain during a write ends the
stream without another frame. A request with `recorded_after` also gets `session_recorded` for each
Session History records at a later position, the backlog first and then each as its first push binds
it, so the app can create a Thread view for Sessions it did not start. The binding draws the Session's
`recorded_position` under a transaction advisory lock held to commit, so positions become visible in
ascending order and a caller that reconnects after the last position it handled misses none. Migration
9 renames the persisted ordinal without changing its values, sequence ownership, ordering or indexes.

`ReadThreadEntities` serves a window of a Session's fold rows, by entity index or by key, as typed
`ThreadEntity` values. Common identity and cursor metadata sits on the entity; its oneof identifies
view state, item, confirmed input, lifecycle Event, or command. Payload references keep their
64-bit positions as `uint64`. Electric remains the row and payload synchronization source for the
browser. A caller holding rows of a replaced epoch gets `FAILED_PRECONDITION`.
The entity table keeps its indexed identity, ordering and pending columns; `state` is ProtoJSON of
that entity's oneof variant, including its payload references. The migration preserves existing
entities and payload chunks, converting rows in one transaction. Deployments must stop old History
writers and old app instances for the schema and consumer cutover: old binaries cannot use the new
row shape. The conversion locks the entity table until it commits, so migration time grows with the
number of retained entities. A malformed row aborts and rolls back the entire conversion.
`ReadEntityEvidence` pages the observation cursors an entity was folded from, and
`ReadNativeFrames` the native harness frames linked to one of them, read from the raw log.
`native_frame_read_batch_size` (default `200`, set by the YAML key or
`AGENTPLANE_HISTORY_SERVICE_NATIVE_FRAME_READ_BATCH_SIZE`) bounds the linked sequences in each raw-history
query. It is independent of the public `ReadNativeFrames` page limit of `200`: storage chunks for one request
share a repeatable-read snapshot and committed-history watermark.

Bazel targets: `:history-service` and `:service_image`. Settings come from `AGENTPLANE_HISTORY_SERVICE_*` environment
variables or the YAML file named by `AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE` (`config.go`, `settings.py`).
The read API and journal ingress share `rpc.listen_address`, where `grpc.health.v1.Health` reports process liveness only.
The listener accepts TCP addresses or inherited descriptors as documented by
[`agentplane/net/listener`](../net/listener/README.md).

## Optional central producers

`ingress.sampler_accounts` explicitly admits central sampler accounts (empty by default). Their
`sampler_announce` authenticates the producer separately from the executor Sandbox and keeps both
identities fixed. A central execution principal is retained only from the Dispatcher-provided
identity on the admitted sampler path; the executor account name is not used to guess its UID.
Native announcements cannot claim central metadata. The same cursor, exact-replay,
writer fencing and commit-before-ack rules apply.

When the Agentplane chart configures a central sampler, it also enables
`ingress.sampler_identity_callers`. Direct History deployments must opt in explicitly.

`ingress.sampler_identity_callers` (requires `policy_namespace`, sampler accounts and the SPIFFE
listener, on which `Push` admits Identity writers) additionally lets those accounts push as
delegating actors under the [delegated authorization contract](../docs/delegated_authorization.md),
naming the Identity a Session executes as and the Session itself. The middleware verifies the
sampler's token and grant and reads the Identity live, so a stale or missing incarnation is refused
before the handler; the sampler stays the writer, and the Identity is retained as the Session's
execution Identity only when the announcement names the same one and the same Session, in a
`sampler_announce`. A sampler pushing as itself keeps relaying the recorded Identity as above.
Announcing a central Session never implies this grant. Central outboxes retain evidence in sampler
PostgreSQL while History is unavailable. Raw boundary and execution-progress observations remain
retained evidence; source-linked normalized Events drive the ordinary Thread fold. History does
not own the provider continuation. See the [sampler contract](../central_sampler/SPEC.md).

The SessionSpec placement cutover's History migration 8 moved a legacy archived
`SessionSpec.harness` value into `historical_harness` on retained feed and summary snapshots when
the old record had no exact placement to report. It did not invent an environment identity or
change raw events, cursors or event ordering. New live openings use the typed placement in
`SessionSpec`; historical evidence remains available.

Migration 15 is the selected Goose baseline. It creates the current schema on a fresh database and
adopts a completed version-14 database after validating its schema. Adoption preserves existing rows,
schema object identities, and Goose history. A deployed database below version 14 must first run the
previous History image through migration 14 and finish that rollout; version 15 fails closed for
older or partial predecessors. Migration 14 removes the temporary command-operation bridge, so
starting version 15 while an older rollout is still active is unsupported.

Use a version-15-compatible image for fresh databases. Their ledger records version 15 without
versions 11-14, so a version-14 image would reject that ledger as missing out-of-order migrations.
On a database adopted from version 14, the prior migration rows remain present and the version-14
Goose provider sees no pending migrations after version 15. That check covers migration startup;
runtime rollback to an older image is unsupported.
