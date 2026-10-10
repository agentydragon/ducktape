# App raw history read handoff (opt-in code; migration in flight)

## Current rollout status (2026-10-09 PDT)

Staging handoff is committed for all 55 retained Sessions. Indexed post-checks confirmed
matching inventories, 55 summaries, raw watermarks equal to their fixed fences, service
coverage of every fence, and app checkpoint coverage for every nonempty Session. The live
canary's raw cursor remained 118104 while its app projection advanced to 118777. Recent
logs from both app replicas contained no projection-stalled or reconciliation-failure
messages after the batch. These checks do not claim full historical payload parity.

The six empty Sessions are explicitly not a verification blocker per operator instruction;
leave their records intact. Cleanup is unfinished: remove flags, legacy paths, migration
jobs and temporary grants, verify normal new-Session/read behavior, and validate testing's
state before changing its defaults. Do not delete retained app data or runner storage.

Cleanup rollout order: first deploy unconditional service-backed app wiring while accepting
the old configuration keys with true defaults. Only after that image is live remove the keys
from both Settings and generated manifests. Removing manifest keys first would restart an
older image with false defaults and stop projection. The acceptance fields are temporary and
must be deleted in the second change, not retained as configurable legacy-path switches.

Testing preflight: 142 app Sessions and 142 service histories, matching IDs, with service
coverage of every captured app watermark. No testing Threads were fenced by this check.

The sections below retain the staged migration design and earlier evidence; their default-off
flags and pre-handoff descriptions are historical, not the current staging state.

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

### Projection failure reporting (implementation in progress)

Service read/validation/fold failures retain a bounded diagnostic in the existing
materialized view's operational state, without changing the retained runner
attachment or EOF. Error publication requires the current sandbox lease and the
same projection cursor as the failed attempt. A successful batch (including an
empty suffix without a runner snapshot) clears the projection diagnostic. Backend
exception text is not exposed to callers. There is no materialized diagnostic
before the first checkpoint, and same-cursor concurrent attempts still require
coordinator serialization; these are not new runner terminal states.

New-session handoff and explicit projection lag presentation remain unfinished.
EOF is not inferred from a service outage or projection failure.

### New-session admission (implementation in progress)

With service projection enabled, new app Threads require a canonical public Session
ID and a successful metadata-only service history lookup. Creation atomically writes
the zero raw-writer fence and empty projection summary. Existing unfenced Threads,
including an old replica winning a concurrent creation, remain on their current path
until explicit handoff; opening a Thread never silently migrates its retained history.
Discovery must return these existing mappings, not reject them and stop the coordinator.
An unregistered legacy ID is isolated to its own discovery entry rather than stopping
other Sessions in the Sandbox. Reconciliation checks
the durable fence after discovery, not just its earlier inventory snapshot, so it
cannot start a legacy Follow for a just-created service projection. Existing flags
remain off until coordinated cutover; this adds no new temporary flag.

## Coordinated staging switch

Prerequisites deployed: service lifecycle (#9610), projected app lifecycle (#9620),
projection error reporting (#9622), and new-session fencing (#9626). Deploy the
mixed-discovery repair (#9634) before merging the staging configuration switch.
The switch enables service reads and the projector together; existing unfenced
Threads keep their raw writer until individually handed off. New service Sessions
start with a zero fence. Testing retains its previous configuration.

Execution order:

1. Confirm the repair image on both Ready app replicas and the service ingester's
   continued progress. Compare only indexed per-Thread watermarks and Session
   inventories. Previously accepted bounded verification remains accepted.
2. Deploy the staging configuration and verify both replicas have it. Do not fence
   old writers before a ready projector exists. During mixed replicas, durable
   fences reject legacy writes regardless of which replica holds the lease.
3. Run the existing transactional `fence_raw_ingestion` per retained Thread, with
   bounded lock/statement timeouts and resumable per-Thread receipts. It seeds
   projection metadata and captures a fixed final raw cursor. Runners continue;
   their service-owned archive does not depend on the app handoff transaction.
4. Check service and UI checkpoint coverage of each final raw cursor, retained
   terminal state, and continued live projection. Repeat inventory to include
   creations concurrent with handoff. A failed fold retains the last good view;
   investigate rather than clearing fences or pretending EOF.
5. Once all Threads are fenced and service-backed reads/projection are verified,
   delete temporary flags, legacy paths, migration jobs and temporary grants.
   Do not delete retained data. After fences, disabling the projector alone is
   not a safe rollback: it would strand service-owned suffixes.

The staging-switch PR must include generator-produced manifest output. A source-only
preparation draft is not ready for merge or a claim that live flags changed.

## Runtime cleanup acceptance

Migration agent evidence, 2026-10-09 (PDT): staging's 55 retained Sessions have
handoff receipts and checkpoint coverage. The flags and import Jobs are retired.
Testing subsequently completed the same handoff: 142 matching app/service histories,
142 fences, 142 preserved summaries and zero nonempty projections behind their final
raw cursor. Indexed watermarks showed service coverage of every retained prefix;
recent app logs showed no projection-stalled or reconciliation errors.

The operator accepted new-session/live-use behavior after creating the staging
`haku` Session (`31d54d02-d197-4fc7-b9be-37b4a177ec2d`). Read-only checks found service
and app projection cursors both at 42,086, app raw cursor zero, a zero-origin fence,
and active projection state with no feed error. This is staging live-use evidence,
not a claim of a new testing Sandbox acceptance run. Together with testing's retained
handoff and the operator's acceptance, it releases the runner-copy deletion hold.

Runtime cleanup remains split into mandatory service-backed archive readers (#9659)
and removal of the app runner-copy loop (#9660). Retained-schema regression setup
moves to test-only helpers, not another production fallback. The service projection
coordinator remains lease-fenced and discovers new Sessions. Review, CI on the current
heads and post-merge rollout checks remain required; passing tests are not rollout.

No full-history verification scan was repeated. No retained records, tables or runner
storage were deleted. Further removal of raw writers, old feed-state fallbacks and
table models remains separate work; these PRs do not claim complete raw-table retirement.

### Runtime retirement rollout evidence

The merged #9660 image (`52b4e8e`) was checked Ready in testing (1/1) and staging
(2/2). Testing still had 142 matching fenced histories and service coverage. The live
staging `haku` Session advanced to cursor 54,132 in both service and app projection,
with app raw cursor zero, active state and no feed error. App logs from all three
replicas showed no projection-stalled, reconciliation-failed or error lines in the
checked ten-minute window.

The following single code cleanup removes remaining runtime raw writer helpers,
uses the app projection checkpoint for its local cursor, and reads lifecycle/Thread
metadata solely from ThreadHistorySummary. Retained-schema test setup moves to
test-only helpers. This does not remove table models, historical migrations, retained
records or the handoff tool; tooling and grant retirement remain separate work.

## Post-cutover schema cleanup

Follow-up runtime cleanup retires `history_handoff.py` and its migration-only tests.
The coordinator selects Sessions by `ThreadHistorySummary`, not the old raw-writer
fence. The projector still checks service coverage of the app checkpoint and fences
commits with the replica lease; it no longer consults the migration barrier. Tests
seed service evidence through the app's gRPC peer rather than a retained raw writer.
This is code retirement, not a claim of deployed schema removal.

TODO(session-schema-cleanup): follow the runtime/test port in #9670 with an explicit
schema-cleanup PR after the new readers are deployed. This is migration completion
work, not an indefinitely deferred task. Preserve public Session/Thread UUIDs and
runner storage; use bounded checks, not another full-history scan.

- Rename app `EventLog` / `event_log` to reflect an app-side Session reference rather
  than ownership of a raw archive. Choose the final name with the schema change and
  update foreign keys, queries and documentation together.
- **Sandbox Service is authoritative for the runner locator.** App command dispatch,
  resume, discovery and reconciliation should use the public service Session UUID.
  Remove the app's physical runner-locator copy (`event_log.session_id`) and its
  `(sandbox, session_id)` uniqueness constraint after replacing current consumers.
  Audit legacy HTTP filters/links and identifier translation explicitly; preserve
  mappings in Sandbox Service, not by inventing another app-owned routing map.
  Runtime cleanup is staged before the column drop: discovery/Open and find use
  public UUIDs; Thread views expose that same UUID and `/threads?session_id=` filters
  by it. Private runner locators are no longer HTTP filter aliases (invalid UUIDs
  return an empty list). Existing public Thread URLs and service mappings stay intact.
  The old non-null column remains a compatibility write for overlapping old replicas;
  deploy these readers before removing it in the schema-retirement PR.
- Drop `raw_ingestion_fenced_at_cursor` with the old-table write-rejection triggers.
  Runtime projection no longer reads it; new identity setup still writes zero to
  preserve old-table write rejection until that explicit schema change.
- Drop retained app `event` and `feed_state` tables and their ORM classes only after
  deployment verification shows no runtime dependencies and handoff tooling is retired.
  Make retained-data deletion explicit in that PR rather than incidental to a rename.
- Migration `0022_session_projection_lease` replaces ephemeral Sandbox-wide leases
  with `session_projection_lease`, keyed by public Session/Thread UUID. Each Session
  independently acquires, renews and fences projection commits; Sandbox discovery does
  not own projection authority. Deleted-Sandbox histories remain projectable.
  Stop old app replicas before migration and deploy the matching app code. The migration
  waits on the old table's writes, removes old lease authority and starts new leases empty;
  it does not alter archive rows, app projections, checkpoints or runner storage. Stop new
  replicas before downgrading and restarting old binaries. This is not rollout evidence.
- Audit app `sandbox`, `harness`, `model` and `cwd` fields: distinguish necessary UI
  projections from redundant launch/routing metadata. They are not all proven dead.
  Session identity, runner bindings and frozen launch configuration remain service-owned;
  any app copies must be derived/read-side data, not competing sources of truth.
- Audit `ThreadHistorySummary` attachment/end/resume fields and duplicate model/activity
  metadata against actual consumers. Remove only demonstrated redundancy; projection
  progress and operational UI state need not equal the service archive watermark.
- Update misleading model/helper docstrings (`runner_session`, raw-reader wording,
  "app ingestion") as their contracts change. Leave durable rules in AGENTS.md;
  keep this temporary cleanup checklist here and in the DAG.

### Session-lease cutover evidence

The per-Session ownership cutover (#9692) completed on 2026-10-10. The temporary
app-only Recreate prerequisite (#9698) stopped old owners before migration; Sandbox
Service, runners and their storage were unchanged.

Bounded live checks at 04:53–04:55 America/Los_Angeles verified:

- Testing was 1/1 Ready and staging 2/2 Ready on the f5273a6 app image; all three
  migration init containers exited zero.
- Both app databases reported `0022_session_projection_lease`, with
  `session_projection_lease` present and `sandbox_ingestion` absent.
- Testing had 142 active leases; staging had 58. Multiple Sessions in one Sandbox
  held independent leases.
- Two active staging projection checkpoints advanced between samples, by 135 and
  3,584 cursor positions. Bounded log samples from both staging replicas and testing
  had no matching error, exception or stalled lines. No archive scan was performed.

The deployment-only follow-up restores `self.env.replicas.strategy` and removes the
migration-specific Recreate regression test: staging returns to RollingUpdate;
testing retains its normal Recreate strategy. This is not a schema rollback.
A binary rollback across the ownership-scope change still requires stopping the app
and downgrading the lease schema first.

### Locator-column deployment prerequisite

Before merging #9712, deploy the app-only Recreate strategy and verify it in both
environments. #9707 is deployed on image `devel-20261010132451-80294e6`: testing
has one updated/Ready replica and staging two; all migration init containers exited
zero. Bounded startup logs from all three app Pods had no error/exception matches.
Those binaries still select/write the locator column, so rolling overlap with the
column-drop migration is unsafe despite their public-ID routing.

The prerequisite changes only app deployment strategy, not its Pod template. The
subsequent migration image rollout stops old app Pods normally before replacement
init containers run. Expect brief app unavailability; Sandbox Service, runners and
archive storage stay unchanged. Do not force-delete Pods.

After #9712 passes CI and this strategy is verified live, merge the schema change.
Check migration exit status, revision/column removal, readiness and bounded
projection progress. Then restore the environment strategy and remove the temporary
rollout regression test. No full-history scan or backfill is required.
