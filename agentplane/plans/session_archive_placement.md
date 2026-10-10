# Session Event archive placement and first cutover

Status: **placement implemented; post-cutover cleanup in progress**. Sandbox Service
owns the archive and live ingestion; the app reads service history and maintains UI
projections. The app runtime/test port landed in #9670. Accepted bounded handoff and
rollout evidence lives in the [read-cutover plan](session_history_read_cutover.md#runtime-cleanup-acceptance).
This is not a claim that every later cleanup revision has finished deployment.

The one-shot importer and its image are retired. Do not start another importer or
backfill; future repair requires separate review. The [historical handoff checks](../sandbox_service/session_history/CUTOVER.md)
retain the bounded-verification contract while legacy app tables remain. Table drops,
identity cleanup and migration squashing remain explicit follow-up work in the
[task DAG](task_dag.md#current-state-and-scheduling). No agent read grants are implied.

## Choice

Put the durable raw Session Event archive **inside Sandbox Service**, backed by
service-owned PostgreSQL state. It is not a property of a Kubernetes Sandbox CR,
runner PVC, or app database. A Session archive survives its Sandbox/Pod/PVC's deletion.
Keep the archive component's storage schema, replay and read interfaces separate
from the Kubernetes inventory/provisioning component so it could later be extracted
to a standalone history service without changing public Session identity.

This is the smallest acyclic route to agent-facing reads: the app already calls
Sandbox Service; Sandbox Service must never query the app at runtime. Agents should
read retained histories via Sandbox Service after authorization. A separate service
would add another ownership/authentication/delivery hop today, without a proven
independent scaling or availability need. Revisit placement if archival ingestion,
retention or read load demonstrably needs a separate failure domain, or when central
command admission calls for a different authority.

## Authority and data

The runner currently owns admitting commands and publishing ordered Events from
its SQLite journal. The archive copies a **contiguous, independently replayable raw
prefix**; it must not invent a native command outcome or reconstruct native resume
state from Thread folds. A durable Session ID names one archive, with source identity,
exact Event payload, source sequence, archived cursor, and copied high-water mark.
Keep source/runner provenance so replay can reject a conflicting duplicate rather
than overwrite it. Multiple Sandbox Service replicas require a per-log claim/fence
or verified idempotency; no process-local ingestion owner is sufficient.

For legacy histories, retain the app's public Event-log/Thread UUID and import its
association with the original Sandbox identity and runner session ID. The runner
session ID remains the physical locator of its journal/native files. New histories
can use a single public Session/Thread UUID. Classification and eventual SA read
grants belong with the durable history authority, not the live Sandbox or a preset;
no agent-facing grants ship as part of the raw-store PR.

Store only the archive's durable state in the new service database. Keep operator
Thread presentation and UI-friendly folds in the app for the first cutover. The app
consumes the archive replay/follow API with its **own** projection checkpoint and
fold epoch; its fold transaction need not be the archive ingestion transaction.
An unrecognized Event or broken fold must not stop copying the raw prefix. Expose
fold lag/error to authorized operators; never advertise a fold cursor newer than
its archived raw prefix.

## Cutover, one-way

1. Add service-owned archive storage and internal/operator raw read/replay, without
   opening an unscoped SA read surface or changing today's app writes.
2. Back up/inventory app history and one-off import IDs, Event payloads, source
   sequences and checkpoints. Import deleted-Sandbox histories too. Compare exact
   prefixes, not merely row counts; the importer is a migration tool, **not** a
   runtime app API dependency.
3. Start live runner-to-archive replay using validated duplicates or per-log
   fencing. While the app remains the public raw authority, compare both observed
   prefixes and fill any gap between the snapshot and runner updates. A runner
   unavailable before its unarchived prefix is recovered is a cutover blocker, not
   a reason to pretend the archive is complete.
4. Quiesce/fence the old app ingester for the affected logs, reconcile its final
   cursor against the new archive and elect the new archive as the sole raw owner.
   Switch app UI folds to consuming archive Events; retire app raw writes and
   direct SA transcript reads. Do not leave two competing public authorities.
5. Verify restart/replay, multiple replicas, exact-duplicate/conflict cases,
   fold failure and catch-up, old Thread URLs, and read-after-Sandbox-deletion.
   The app remains a composition client and an owner of its UI projections only.

A cutover must have a backup and a recorded high-water mark. Rolling old app code
back after it stops ingesting requires reconciling _new_ authoritative archive
Events, not simply flipping traffic back to stale app tables. Remove one-off import
code once completed, retaining the legacy **data association** needed for old
runners. Never delete the existing Sandbox state or rename native directories to
make a schema migration look simpler. Squash the app's historical Alembic revisions
only after the identity/archive cutovers and all deployed stamps are verified.

## Operational separation

Archive failures must not make inventory and existing Sandbox lifecycle RPCs
universally unready. Report failures and archive lag explicitly; separately gate
archive read/ingestion where its database is unavailable. A runner may continue
using its current journal while the service is down and replay after recovery.
Moving command durability or switching the runner to an outbound connection is a
separate later decision. A deleted Sandbox cannot be used as an archive lookup
prerequisite, and app-backed broker reads are not a temporary implementation.
