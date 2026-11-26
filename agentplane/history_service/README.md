# History Service

Serves retained Session history: the raw runner Event log and its observation metadata, keyed by
public Session ID. An Identity reads Sessions retained under that exact Identity name and UID,
independently of the original Sandbox remaining. Exact service SVID readers have an explicit
service-wide read path. A workload SVID without an Identity has no read scope, even when a retained
writer-account tuple matches its ServiceAccount. A Session UUID by itself grants nothing.
Consumers use `client.py`; `testing/go_service.py` runs the built Go service over a test database.
Tests can inspect the fold's storage through `fold/models.py` and use the generated Thread entity
messages for semantic reads.

The required `rpc.auth` enables SPIFFE client trust; `policy_service` and
`ingress.writer_namespaces` are required for live workload credential checks.
`reader_service_callers` grants named service SPIFFE IDs the backend-wide read scope. The independent
`service_delegation` setting grants exact service actors read methods as a live effective Identity;
those calls use only that Identity incarnation's retained Sessions. Service certificates do not
produce a Kubernetes account or Pod proof, and a delegated service actor does not get the direct
service reader scope.
The receiver never looks up Actions or stores its credentials. Watch and follow leases still require a new
admission when the client reconnects.

Managed Pod, VM and Firecracker CLI environments set `AGENTPLANE_HISTORY_URL` to the shared local
platform gRPC listener, `http://<local gateway>:3130`. The listener routes History RPCs through an
mTLS CONNECT to the History Service; the sidecar presents the Sandbox workload SVID, and this
service scopes the read to the Identity name and UID in that credential. No projected History token
or `reader_service_callers` grant is needed for this path.

The service binary embeds Goose migrations and applies them during normal startup before serving
or starting the fold. `history-service --migrate-only` initializes fixture databases using the same
lifecycle. Its runner ingress and fold are the runtime writers.
The `migration_timeout_s` setting defaults to 600 seconds and includes advisory-lock waiting;
the startup probe must also cover connection retry and serving startup. Migration-only mode reads
this setting from the same required settings file.

## While the History Service is down or rolling

Runners keep their harnesses running and every journal entry on their state volume, and push the
backlog once the ingress answers again; nothing is lost while the History Service is away, only
late. Sender-facing commands remain in the Session Dispatcher and reach runners through the Environments
Runtime proxy; no Sessions or Environments binary calls this service.

During shutdown the fold stops and hands back its leases while existing ingress calls drain. Entries
already acknowledged remain committed in raw history, and a restarted fold projects any remaining
history from its checkpoint.

The app's Thread reads and command retries, Action Service session orchestration, and Notification
Service deliveries wait for History. Sandbox teardown deletes nothing until the ingress has
acknowledged every seal. The egress proxy, Policy Controller and LLM ingress do not call it.

## Runner ingress

`internal/ingress/` serves `SessionJournalIngress.Push`, defined in the
[public protobuf API](api/ingress/session_ingress.proto), with behavior described by the [runner
ingress contract](../docs/runner_control.md#event-ingress): runners push each Session's
journal here, one call per Session. Sandbox Runtime opens end-to-end mTLS through the sidecar
and central proxy's exact-authority CONNECT passthrough to `rpc.listen_address`.
History presents `spiffe://agentplane/history-ingress`, verifies the client SVID against the
Agentplane workload CA with an explicit clientAuth EKU, and resolves its Identity, WorkloadCredential,
and Sandbox owner. The Identity comes from PolicyService (`policy_service`, mutual TLS as History: a
`WatchIdentities` view with read-through `GetIdentity` on a miss); the WorkloadCredential and Sandbox come from live
Kubernetes records. A deleted Sandbox, stale credential, changed Identity
incarnation, or missing bound Pod is refused before the ingress handler. No header selects the caller
and the proxy asserts none.

`rpc` configures the shared native gRPC listener and server TLS files. History admits live workload SVIDs
for Identity reads and runner `Push` calls, plus configured service SVIDs for component reads and
sampler writes. The listener requires an SVID. A workload certificate cannot be combined with
authorization metadata. For runner writes the WorkloadCredential must name an Identity PolicyService serves and belong to a Sandbox in `ingress.writer_namespaces`. See the
[Session writer binding](#session-writer-and-execution-sandbox) for announcement and reconnect checks.

`Store.Append` accepts exact replays and refuses gaps and conflicting entries. Each acknowledgement
follows its commit. The ingress queues at most 16 decoded frames and commits at most 128 entries
or 1 MiB of serialized entries per transaction (one oversized entry forms its own transaction).
A full queue applies backpressure to the stream.

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

A replacement Runtime can continue a sealed journal if its announcement names a different
incarnation and reaches the committed seal cursor. History drops the prior seal and acknowledges
that cursor as unsealed. An announcement from the sealing incarnation, or one without an
incarnation UID, receives the existing seal. Runtime-generated seals carry the incarnation UID.

## Read and follow one Session

`FollowSession` reads raw runner entries in cursor order and can keep sending them as they commit.
Go callers use the generated `historypb.HistoryServiceClient`, as for the other History RPCs.
The browser continues following fold tables through Electric. Unary reads used by the browser also
have generated ProtoJSON HTTP bindings (`/v1/sessions/...`); the integration app forwards those
same-origin requests to History over gRPC using its App service SVID. The App SVID has an exact
service-reader grant; browser-session authorization remains at the App boundary.

Each request chooses exactly one start:

- `after_cursor`: exclusive, with zero naming the beginning;
- `back { kind, count }`: includes the count-th most recent retained entry of that kind, or starts
  at the beginning if there are fewer;
- `tail`: only entries committed after the opening snapshot.

An empty `kinds` filter sends every observation. `EventKind` numbers match the fields of the runner's
`Event.observation` oneof. Ingest stores that number in `session_event.kind` (zero for no known
observation); `(session_id, kind, cursor)` indexes filtered reads and back starts.

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
sent alone. Indexed cursor/kind/size metadata selects entries and applies byte bounds before their
payloads are loaded; packed blocks are decompressed only for selected entries.
The server reads a bounded entries/settlements page, waits for its frames to finish sending, then
reads the next; it holds no database transaction while sending. Each removal has a per-Session
position, drawn under the Session's history row lock so positions commit in order. A catch-up reads
the removals after the last position the stream reported, stopping before the first one that starts
past its cursor, so each stream reports each removal once. Backend reads and waiting for each stream `Send` to clear transport
backpressure have `request_timeout_s` bounds. A bound does not guarantee when a queued frame reaches
the peer.
Each replica subscribes followers to the wakeup hub's
`agentplane_session_history_changes`, keyed by Session ID, before the first read. A notification
carries the Session ID and wakes only that Session's followers; reconnecting the LISTEN connection
wakes all of them to cover lost notifications. Cleanup writers must record removed ranges and notify
that channel with the Session ID in the same transaction.

Normal read scope is applied in SQL before pagination, projection, or evidence lookup. It covers
summary reads and batches, Sandbox discovery, raw event and observation pages, Thread entities,
entity evidence, native frame lookup, follows and their removed ranges, and opened-Session watch
events. Normal watch positions are ordinals over the caller's retained Sessions, so a cursor does
not reveal gaps from another principal; configured backend service readers retain the existing
service-wide opening cursor. Each RPC resolves its workload credential against live Kubernetes
records. Watch/follow streams end at their lease so continued reads require a newly authorized call.
Backend reads are granted by `reader_service_callers`; there is no `reader_accounts` setting, and the
strict settings model refuses the key. Errors carry a gRPC status and fixed detail:

| Status              | Cause                                                                             |
| ------------------- | --------------------------------------------------------------------------------- |
| `NOT_FOUND`         | The Session is missing or outside the caller's retained Identity scope.           |
| `OUT_OF_RANGE`      | `after_cursor` exceeds the committed tail.                                        |
| `INVALID_ARGUMENT`  | Invalid Session UUID, absent start, zero back count, or unspecified/unknown kind. |
| `PERMISSION_DENIED` | The RPC method is not admitted by this service's method policy.                   |
| `UNAUTHENTICATED`   | Missing or invalid caller credentials.                                            |

## Session writer and execution Sandbox

The first accepted push binds the Session to its writer and records the execution Sandbox in
`session_history`. For Runtime the writer is its authenticated Identity incarnation (name and UID),
and the Sandbox is the controller owner of its WorkloadCredential. For a central Session the writer
is the authorized sampler's service SVID, and the Sandbox comes from the sampler announcement.
Another writer or Sandbox UID gets `PERMISSION_DENIED`. The `history_writer_one_kind` constraint
keeps legacy ServiceAccount, Identity, and service writer bindings mutually exclusive.

History serves the stored Sandbox reference through `SessionSummary.sandbox`. It supports
`FindSessions` by Sandbox name or UID even after the Sandbox is deleted. Older ServiceAccount
writers derived the UID from the account's Sandbox controller ownerReference. A matching writer
may fill a missing UID on a legacy record.

The retained execution Identity (`Attached.execution_identity`) is the exact incarnation recorded
by Dispatcher. A Runtime announcement that supplies it must match the authenticated Identity. A
sampler announcement that supplies it must carry delegated authority for that Identity and Session.
Writer and creator ServiceAccounts remain historical provenance.

The first trusted execution Identity is immutable and scopes Identity reads. Conflicting owners
fail. Omitting the Identity cannot erase a pinned owner, and a later announcement cannot assign
one to an unassociated legacy Session. Configured platform readers can still read that history.

## Incremental storage compaction

For the APB writer rollout, see [Verify new journal blocks](../eventblock/rollout.md).

`ingress.compact_deltas: true` losslessly packs native frames and streamed deltas into bounded
compressed blocks during ingress, before an item completes. The default is false. Original events,
native JSON, cursors and evidence references are reconstructed by every reader, including after the
flag is disabled. Existing completion-based settlement applies independently to both storage forms.

`ingress.compact_execution_evidence: true` separately packs BoundaryRecord and ExecutionProgress
events. It defaults to false so this reader support can roll out to every History replica first;
older versions cannot read those block kinds. Keep the gate off during mixed-version deployment.
The online historical repack covers every concrete event kind using the configured per-kind block
profile, so run it only after every reader has the new block support. Disabling the gate does not
unpack existing blocks. The [read contract](api/history/README.md) defines pagination, replay,
atomicity and rollout requirements.

This packs retained events; it does not share bodies across raw history and projection tables or
backfill old Sessions. Ordinary insertion/deletion still generates WAL.

### Online historical repack and legacy inventory

Deploy reader support to every History replica before starting an apply run.
The maintenance command loads the service YAML named by
`AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE` plus `AGENTPLANE_HISTORY_SERVICE_DATABASE_URL`. It uses
the same `ingress.event_block_profile` and `ingress.event_block_target_bytes_by_kind` settings as
the writer, including when live event-block writes are disabled. It does not invoke harnesses or
providers.

Preview one Session as a canary before applying it:

```sh
AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE=/etc/agentplane-history/config.yaml \
AGENTPLANE_HISTORY_SERVICE_DATABASE_URL="$HISTORY_DATABASE_URL" \
history-service compact-history --session "$SESSION_ID" --dry-run
```

The command requires exactly one scope (`--session UUID` or `--all`) and one mode (`--dry-run`
or `--apply`). Dry-run uses a read-only repeatable-read snapshot and writes no journal or progress
state. It simulates the configured packer over ordinary rows and decoded legacy and versioned
blocks in cursor order. Its totals include source format counts, candidate block counts, logical
event bytes and estimated packed bytes.

Apply holds the opening `recorded_position`, each Session cursor watermark, and run cursors in
memory only. It enumerates `session_history` directly, including inactive Sessions, in pages of 100. Candidate decoding and compression happen outside the append lock. A short transaction then
acquires the Session row with a 250 ms lock timeout and
a 5 second transaction budget (`--batch-transaction-budget`), revalidates the exact source and
settlement/removal state, and atomically installs up to four output blocks (`--max-blocks-per-txn`). The whole
packing transaction is bounded to 256 frames and 8 MiB of block input. Set the flag to 1
to install a single block per commit. PostgreSQL `transaction_timeout` and the transaction context enforce the budget;
an expired batch rolls back and retries the same candidate up to three times. Existing blocks are
unpacked in bounded units before their frames are merged with ordinary rows and repacked, which
handles sparse cursors, interleaved kinds, partial block boundaries and mixed formats without
nested block spans. The unpack transaction compares raw block metadata and bytes to its prepared
candidate and inserts already-validated raw frames without decoding the source payload under lock.

Each batch logs `lock_wait`, `lock_wait_budget`, `lock_hold`, `budget` and `outcome` separately.
Repacker database connections use `application_name=history-repack` for canary observation. A
synthetic integration canary stalls one unpack transaction for 750 ms with a 2 second budget and
verifies that a concurrent append waits only for that transaction. It records the measured wait,
hold and append wait in the test output; this PR did not run a live apply canary.

The database stores no repack run or per-Session progress records. Each bounded batch commits its
event-row/block replacement atomically. If the process stops, the next invocation rescans Sessions
from the beginning; previously committed blocks and ordinary rows remain valid inputs, so the scan
can safely repeat work. A rescan may rewrite blocks committed by an earlier process and therefore
generate additional WAL. Within one process, Session watermarks and cursors advance in memory.
Busy Sessions and batches that exhaust their budget are deferred while the command scans the rest,
then retried in three bounded sweeps. If contention remains, the command exits with a busy or budget
error; a later invocation starts a fresh safe scan. Avoid overlapping all-session runs because they
can increase lock contention and repeat work, even though each batch revalidates its source under
the Session lock.

The default processing limit is 1 MiB of source payload per second. Use
`--bytes-per-second N` to tune it. `--pause-file PATH` pauses before the next batch while the file
exists and resumes when it is removed. A live control file can also be set with `--guard-file PATH`:

```json
{ "updated_at": "2026-11-26T12:00:00Z", "replica_lag_bytes": 0, "free_bytes": 10737418240, "pause": false }
```

Missing, malformed or stale guard data pauses the job. Set
`--max-replica-lag-bytes N` and `--min-free-bytes N` to pause at those thresholds; both require a
guard file. Refresh `updated_at` from the operator's existing replica-lag and free-space monitoring.
Keep a pause file or set `pause: true` when the source metrics are unavailable.

After the canary apply, inspect aggregate inventory:

```sh
AGENTPLANE_HISTORY_SERVICE_DATABASE_URL="$HISTORY_DATABASE_URL" history-service inventory-history
```

The JSON reports ordinary row counts and bytes plus total, versioned, legacy and invalid block
counts. Legacy cleanup is ready only when `legacy_blocks` and `invalid_blocks` are both zero.
Invalid blocks are counted separately instead of being mistaken for legacy gzip.
Run another all-session dry-run, then schedule `compact-history --all --apply` in bounded windows.
Use [the manual Job template](ops/compact-history-job.yaml) and the
[inventory Job template](ops/inventory-history-job.yaml) with the deployed image. Watch database
free space, WAL archiving and replication lag throughout. Ordinary vacuum makes freed space
reusable; repacking does not shrink relation files.

For a one-Session canary on PostgreSQL 18.1, sample the repacker backends while the command keeps
its connections open:

```sql
SELECT pid, (pg_stat_get_backend_wal(pid)).wal_bytes AS wal_bytes
FROM pg_stat_activity
WHERE application_name = 'history-repack'
ORDER BY pid;
```

Record the initial and final `wal_bytes` for every repacker PID, allow backend statistics to flush,
and capture the final sample before those connections close. Sum per-PID deltas for committed work.
If a PID appears after the initial sample or disappears before the final sample, mark WAL attribution
incomplete rather than substituting a cluster LSN delta. Cluster WAL and replica lag remain pressure
signals. Agentplane does not need to be quiesced for this one-Session measurement.

### Live ingress packing probe

`bb run //agentplane/history_service/ops:probe_ingress -- --target HOST:PORT --ca-file PATH
--writer-cert-file PATH --writer-key-file PATH --reader-cert-file PATH --reader-key-file PATH
--reader-uri spiffe://agentplane/app --session-id UUID` writes a dedicated synthetic
162-event journal through the deployed ingress, seals it, and checks exact protobuf readback of the
whole journal and a page starting at cursor 18. It starts no harness and calls no provider.
Supply an explicit new Session UUID and certificate/key files for `spiffe://agentplane/central-sampler`
(the configured ingress actor) and an exact `reader_service_callers` service (App by default).
Each connection verifies the History server URI using the workload trust bundle; a localhost
port-forward can supply the target. The shared TLS client validates the local certificate URI and
reloads rotating files. This tool sends no bearer metadata and has no token fallback. Supply files
through the operator's authorized maintenance environment; keep private keys in their mounted files.
Retrying the same UUID resumes from the acknowledged cursor or reads the
already sealed journal. The synthetic Session remains in History with `history-compaction-probe`
executor metadata.

Readback alone does not prove packing is enabled: inspect `session_event_block` for the probe's
Session UUID, require at least one block, and check that the sum of block cursor-array lengths plus
ordinary row count equals 162. This also separates live packing from a historical maintenance pass.

## Delta settlement

Each `Store.Append` batch that completes an item drops that
item's streamed delta entries (native frames and their derived deltas) from `session_event`, in
the same transaction, when every frame matches an exact versioned template
(`internal/storage/delta_templates.go`) and the retained completion holds exactly their
concatenation. Malformed frames, failed commands, harness exits and recovery boundaries keep the
span whole. Harness stderr remains stored at its original cursor but does not prevent settlement
of otherwise redundant deltas. The dropped cursors are recorded as a
`SettledDeltas` with its ranges, and read pages report the settlements that cover or complete in
them; the fold and the app's raw-history readers treat a settled cursor as absent. The runner
journal keeps every entry. On by default; setting `ingress.settle_deltas` to false keeps every
stored entry, for debugging. Why and what it drops:
[delta settlement ADR](../docs/adr_delta_settlement.md).

### Historical settlement

The same image supports `history-service settle-history --session UUID --dry-run` and `--apply`.
Use `--all` instead of `--session UUID` to visit retained Sessions in recording order. Both the scope
and mode are required. This command needs only `AGENTPLANE_HISTORY_SERVICE_DATABASE_URL`; it does
not start the service, run migrations, restart runners or call providers.

Each Session is scanned in pages of at most 128 entries / 1 MiB, with an oversized entry alone.
The scan retains completion cursors and the last observation referencing each source cursor, not
event bodies. Memory grows with the number of source references in one Session. Each eligible
completion uses the ordinary settlement checks, including the 20,000-entry span limit. A later
observation citing a chunk keeps that item's deltas. Dry runs use read-only transactions and log eligible logical entries and uncompressed protobuf
payload bytes, including packed entries; these are not physical storage savings estimates.

Apply commits one completion per transaction, with a 30-second deadline and a 1-second lock timeout.
It checks eligibility in a read-only transaction before taking any write lease; ineligible or
already settled completions leave the projector alone.
It acquires the projection lease and locks the Session against appends. A changed prefix or a live
projection lease stops that Session's pass; `--all` visits the other Sessions before exiting nonzero.
Rerun after the Session quiets or the fold catches up. Earlier commits remain applied. Recorded
settlements are skipped on rerun.
The transaction removes the deltas and their projection evidence, records settlement metadata and
cleanup ranges, and wakes History followers. Cleanup ranges correct readers already past the old
completion; item bodies, checkpoints and stderr stay intact. A projector holding an old fetched
page loses its lease fence and cannot restore deleted evidence.

For staging, copy [the manual Job template](ops/settle-history-job.yaml), replace its image with the
deployed History image containing this command, and create it with `kubectl create -f`.
The template starts with a dry run of the measured reasoning Session. Inspect the Job's logs,
then create a new Job with `--apply`. Verify History page/sequence reads, completion/stderr retention,
and projection evidence before replacing the Session scope with `--all`. The Job uses the existing
History ServiceAccount, network-policy label and database Secret; no new RBAC or permanent workload.
It is not included in Flux. Kubernetes retries are safe; cancel by deleting the Job. Cancellation
keeps already committed settlements. Reverting the code does not restore deleted chunk boundaries.
Ordinary PostgreSQL vacuum makes freed space reusable; this command does not shrink relation files.

## Removed ranges

A cleanup that deletes entries from `session_event` records their cursors in
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
(`internal/threadfold/` computes it, `internal/projection/` runs it; `thread_*` tables, since a Thread is the projection of a
Session: [layering](../docs/thread_layering.md#authorities-and-representations)), in epoch
`history-v1`, from cursor 0. A `session_history` trigger queues a Session in `session_fold_pending`
when it is created or its committed prefix or feed state changes, so writers of any release mark it;
each cycle claims the queued Sessions no other replica leases (`FOR UPDATE SKIP LOCKED`, then the
per-Session projection lease) and folds a page of each, so work per cycle follows the Sessions that
changed. A Session leaves the queue, and its lease is handed back, once a fold catches it up with no
newer change. The fold writes only those tables and the queue. Cycles run back to back while any
Session is behind, and committed history changes wake a caught-up fold. `fold_interval_s` bounds the wait when no history-change signal arrives, including while another
replica holds a Session lease. Each cycle logs
how many Sessions it claimed and how many Sessions and events it left behind. A page that carries an item's completion
carries its settlement, so the fold skips the settled cursors and drops evidence it recorded for
them before they settled. This fold is the only one: the app reads every Thread from it, and
Electric replicates its tables to browsers.

A permanent projection failure, including saved lifecycle JSON that cannot be decoded strictly,
leaves the checkpoint and retained history unchanged. Under its lease, the fold records the generic
stalled diagnostic and `FAILED` operational state at the last verified checkpoint, then logs the
backend cause once and parks that pending version in memory. The lease stays renewed; unrelated
history changes and periodic cycles do not refold the parked version. A new pending version or
lease owner (including a replica restart) attempts it again once. The pending row remains, so a
new reader image can re-evaluate it. A failure before the first materialized checkpoint has only
the backend diagnostic; the fold does not invent a view or Event source to report it.

Lost leases, database failures and failures to retain the diagnostic remain retryable on the next
cycle. Other Sessions advance independently. A panic stops the replica's fold but not its raw
reads: it is logged, and the `grpc.health.v1` service `agentplane.history_service.Fold` turns from
SERVING to NOT_SERVING. Per-Session failures leave that health service SERVING. Probes check only
the whole server; a `Health/Check` for that service on a replica's gRPC port shows it.

Session summaries (`GetSessionSummary`, and `BatchGetSessionSummaries` for up to 200 named Sessions
at once, leaving out any it has no history for) read that fold and report the committed cursor beside
the folded one.

`WatchSessionSummaries` in selected mode follows up to 200 named Sessions in the caller's read scope. It
subscribes before reading and sends a full snapshot first, then changed summaries and removals.
Notifications are wakeups, not data: after listener recovery the service rereads the selected rows.
A reconnect starts with a new snapshot and fresh authorization.

`WatchSessionSummaries` tells a reader when to re-read: each fold commit NOTIFYs the history
database, and every replica forwards that to its watchers as a payload-free `changed`. A stream opens
with one `changed` to cover whatever the caller missed, and an idle stream ends with
`reconnect_required` after `watch_lease_s` or a replica drain. A lease/drain during a write ends the
stream without another frame. A request with `recorded_after` also gets `session_recorded` for each
Session History records at a later position, the backlog first and then each as its first push binds
it, so the app can create a Thread view for Sessions it did not start. The binding draws the Session's
`recorded_position` under a transaction advisory lock held to commit, so positions become visible in
ascending order and a caller that reconnects after the last position it handled misses none.

`ReadThreadEntities` serves a window of a Session's fold rows, by entity index or by key, as typed
`ThreadEntity` values. Common identity and cursor metadata sits on the entity; its oneof identifies
view state, item, confirmed input, lifecycle Event, or command. Payload references keep their
64-bit positions as `uint64`. Electric remains the row and payload synchronization source for the
browser. A caller holding rows of a replaced epoch gets `FAILED_PRECONDITION`.
The entity table has indexed identity, ordering and pending columns; `state` is ProtoJSON of
that entity's oneof variant, including its payload references.
`ReadEntityEvidence` pages the observation cursors an entity was folded from, and
`ReadNativeFrames` the native harness frames linked to one of them, read from the raw log.
`native_frame_read_batch_size` (default `200`, set by the YAML key) bounds the linked sequences in each raw-history
query. It is independent of the public `ReadNativeFrames` page limit of `200`: storage chunks for one request
share a repeatable-read snapshot and committed-history watermark.

Bazel targets: `//agentplane/history_service/cmd:history-service` and `//agentplane/history_service:service_image`. All non-secret settings come from the required YAML file named
by `AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE` ([`cmd/config.go`](cmd/config.go), `settings.py`). Generic `AGENTPLANE_HISTORY_SERVICE_*`
environment variables do not override it. The database URL remains an explicit Secret input in
`AGENTPLANE_HISTORY_SERVICE_DATABASE_URL`.
The read API and journal ingress share `rpc.listen_address`, where `grpc.health.v1.Health` reports process liveness only.
The listener accepts TCP addresses or inherited descriptors as documented by
[`agentplane/net/listener`](../net/listener/README.md).

## Optional central producers

`ingress.sampler_service_actor` admits one exact service SPIFFE ID as a central producer and
delegating actor. A direct service push can announce a central Session without an execution Identity.
To push a Session with an Identity, the actor must include typed Session provenance; History checks
that the envelope names the canonical announcement UUID and that its live effective Identity matches
the announcement. `session_history` retains the service SPIFFE ID as writer provenance and the
effective Identity separately. It does not create or rewrite legacy ServiceAccount writer rows.
Central outboxes retain evidence in sampler PostgreSQL while History is unavailable. Raw boundary
and execution-progress observations remain retained evidence; source-linked normalized Events drive
the ordinary Thread fold. History does not own the provider continuation. See the
[sampler contract](../sampler/SPEC.md).

Migration 18 is the selected Goose baseline. It creates the current schema on a fresh database and
adopts a completed version-17 database after validating its schema. Adoption preserves existing rows,
schema object identities, and Goose history. A deployed database below version 17 must first complete
the previous image's audit rewrite (migration 17, run with writers stopped;
[details](../postgres/migrate/README.md#sampler-and-history-audit-rewrite)); version 18 fails closed
for older or partial predecessors.

Use a version-18-compatible image for fresh databases. Their ledger starts at version 18, so a
version-17 image would reject it as missing out-of-order migrations. On a database adopted from
version 17, the prior migration rows remain present and the version-17 Goose provider sees no
pending migrations after the baseline. That check covers migration startup; runtime rollback to an
older image is unsupported.

### Versioned event blocks

After every History reader supports versioned blocks, ingress can seal events of
any concrete kind. These YAML fields configure the new writer:

```yaml
ingress:
  event_block_writes: true
  event_block_profile: { target_bytes: 65536, target_entries: 32, codec: zstd, level: 3 }
  event_block_target_bytes_by_kind: { BOUNDARY_RECORD: 2097152 }
```

Events share a block only within one session and kind. Ingress adds the crossing
event before sealing, including an oversized singleton if it fits the decoder
limit. Incompressible groups can remain ordinary rows. The new writer supersedes
the legacy packing switches when enabled. Reader behavior is independent of the
write settings; changing them does not rewrite existing blocks.
