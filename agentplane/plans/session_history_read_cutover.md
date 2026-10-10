# App raw history read handoff (opt-in code; migration in flight)

The [DAG migration lane](task_dag.md#1-finish-the-history-migration-before-expanding-persistence)
separates `THREAD_ARCHIVE_BACKFILL`, `THREAD_ARCHIVE_INGEST`, `THREAD_ARCHIVE_READ_CUTOVER`,
`THREAD_ARCHIVE_UI_CUTOVER`, `THREAD_ARCHIVE_OWNERSHIP` and `APP_RAW_HISTORY_RETIRE`.
Bulk import and a catch-up pass completed; live handoff remains in progress. This document does
not claim any live switch has been enabled.
New service input/metadata tables and unrelated app schema changes wait for the ownership capstone.

`ReadSessionEvents` is bounded to 1000 entries and the explicitly configured
Sandbox Service `history_reader_accounts`. A public UUID is not authorization.
The app's service identity is the only configured reader; notification service
and sandboxes cannot call it. The app's `history_reads_enabled` switch is off
by default. When enabled, `/events`, `/events/stream` and expanded raw observation entries read the service and
read a fixed committed service prefix rather than chasing the independently advancing app cursor.
A requested resume cursor beyond the service prefix remains an explicit error; reads never silently
fall back to app rows. Chronological observation metadata also comes from the service through
`ReadSessionObservations`: bounded forward/backward seeks under the same app-only reader
allowlist, without sending native payloads to the app. Deploy that RPC before enabling app
history reads. Thread folds and feed state still come from the app.
This is therefore a staged **read migration**, not permission for agents to
read Sessions or permission to delete the app raw tables.

Before enabling: import existing rows via #9460 (including deleted sandboxes),
resolve any active legacy rows with NULL Sandbox UID, prove shadow copier
parity and handoff under concurrent writes, deploy the service reader first,
then enable the app read switch. Confirm paging, SSE replay/resume, old native
frames, and inability to read raw history with a non-app ServiceAccount.
A follow-on change must make the service the durable write authority and
move remaining app raw observation _metadata_ reads without breaking app-only folds.
Until then keep app event ingestion and do not remove its tables.

## Executable preflight and concurrent-write gate

Use the [catch-up/handoff runbook](../sandbox_service/session_history/CUTOVER.md) and its bounded,
read-only verifier for fixed-watermark archive samples and bounded runner overlap evidence.
The operator explicitly chose not to repeat a full historical scan: import receipts, all-Session
watermarks and selected boundary windows are the migration evidence, with residual risk of an
undetected interior mismatch. Sample success must not be reported as full historical parity. The verifier
does not assign legacy UIDs or implement the consumer handoff. A one-time cursor match while app
and service ingest independently does not make the fail-closed read switch safe: coordinate the
app read/projection cursor with the service's committed prefix before switching. Keep runner
execution and at least one ingestion path active; fence only the old app consumer at its recorded
final cursor under a reviewed handoff. No global quiet period or automatic stale-table rollback.

## Draft app consumer handoff primitives (not a rollout switch)

The draft adds a service-watermark-bounded raw reader and `HistoryProjector.project_batch`.
The projector resumes the existing `ThreadCheckpoint`, folds at most 128 service Events under
an app ingestion lease, and advances UI state/checkpoint atomically without writing app `Event`
rows. Replays resume from the committed UI position; fold failures leave the checkpoint unchanged
and cannot block the independent service raw ingester. The existing app lease coordinator can now schedule it with the default-off
`history_projection_enabled` setting, including retained Threads whose Sandbox is gone. It never
fences on startup. No deployed flag changes in this draft.

Before wiring or enabling it:

1. Review/test the new per-Thread durable fence. `fence_raw_ingestion` locks the Thread mapping,
   drains in-flight app raw/feed-state writes, and records their final cursor. Database triggers
   reject later legacy writes even after a lease is reacquired by an old binary. This adds a
   nullable metadata column and triggers only; no automatic fence/data rewrite. Downgrade refuses
   to remove an active fence. The projector waits for service coverage of that final raw cursor.
2. Validate the default-off supervisor under owner takeover and mixed replicas. It shares the
   existing sandbox lease coordinator rather than competing for a second lease, discovers fenced
   retained Threads independently of live runner discovery, and retries from their UI checkpoints.
   Both raw ingestion and service projection update the model-activity timestamp in their
   checkpoint transaction; retry/tool-output-only batches do not advance it. Fold failures are
   logged and isolated per Thread; durable UI lag/error presentation is still
   needed. Existing raw feed tasks are stopped for fenced Threads, not for their unfenced siblings.
3. Chronological observation metadata now has a bounded service reader. Move Thread/feed cursors/lifecycle away from app raw
   `Event` rows. Preserve UI checkpoint/source/epoch and existing Thread URLs. The raw SSE path now
   waits for a terminal app suffix instead of spinning or prematurely ending while service lags;
   this is not yet service-owned lifecycle evidence.
4. Test owner takeover, replica restart and migration interruption end-to-end, then perform the
   reviewed cutover with app raw tables retained. Never enable both competing projection paths.

This draft is useful before shadow convergence, but does not satisfy the archive-ownership gate.

## Thread metadata handoff and completion cleanup

`ThreadHistorySummary` stores only last-event time and the last completed-turn enum number;
the existing `ThreadCheckpoint` remains the cursor authority. The explicit writer fence seeds
these fields from three per-Thread indexed lookups (tail cursor, latest timestamp, latest turn
completion) while draining legacy writes. No startup historical backfill is introduced. Service
projection updates this summary in the same transaction as the UI checkpoint, and fenced Thread
list/get/rename reads no longer derive those fields from frozen raw rows. Unfenced Threads keep
the old path during the mixed rollout. Feed attachment/lifecycle is still a separate remaining
handoff: this summary does not make old `FeedState` authoritative after raw ingestion stops.

Operator completion requirement: after verified cutover, delete temporary migration flags,
configuration plumbing, legacy writer/read branches, backfill/catch-up Job declarations and
rollout gates. Remove temporary preflight RBAC after final checks. Retain useful regression tests,
schema migration history and a concise completion record. Keep old raw data, spools and PVCs;
flag/code cleanup is not authority to delete retained history.

## Service feed lifecycle prerequisite

Service history retains a protobuf attachment snapshot only after its published prefix is
committed. A bounded probe of a stopped runner records EOF only on the runner's successful
stream end; timeout, transport error, missing Sandbox, or a newer Event is not EOF. Newer
snapshot cursors supersede older ones; delayed writers cannot rewind them, and conflicting
snapshots at one cursor fail closed. An EOF bit means confirmed closure at the attached cursor,
not a claim about later runner connectivity. Imported histories without a snapshot remain
explicitly unknown. `ReadSessionEvents` returns this optional metadata with its captured
watermark under the existing app reader authorization.

This schema/RPC addition is a prerequisite, not the app lifecycle switch. The app still needs
to adopt service snapshots at covered projection cursors, fold later lifecycle Events, preserve
retained legacy terminal evidence, and expose projection lag/failure independently of runner EOF.

### Projected lifecycle adoption (implementation in progress)

The service lifecycle prerequisite (#9610) is deployed: two Ready replicas, schema
`0003_history_feed_state`, and retained snapshots observed with a metadata-only query.
The app follow-up stores lifecycle in `ThreadHistorySummary`, seeded from legacy
`FeedState` at the raw-writer fence. Service snapshots are adopted only once their
cursor is covered by the app fold; newer Events advance that snapshot atomically
with the checkpoint. Empty suffixes can confirm EOF. Missing snapshots preserve
retained terminal evidence for deleted histories. A confirmed resume suppresses
old-cursor EOF until newer evidence arrives, without writing frozen `FeedState`.

The follow-up remains under test, not a cutover. Projection failure/lag presentation,
new-Session handoff, mixed-replica acceptance and removal of temporary flags and
legacy paths remain unfinished. No raw Event rewrite or full verification scan.
