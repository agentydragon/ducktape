# Successful sampler audit deduplication

Status: implementation under validation; staging has not been rewritten. Sampler is at migration
13 and History at 16. The new data migrations are sampler 14 and History 17. After application and
live verification, replace both chains with current-schema baselines and remove the converter.

## Retained data and verification

Successful sampling and compaction results drop their top-level `events` arrays. Terminal JSON,
usage, HTTP metadata, Anthropic replacement contexts, individual provider events, raw transport,
failure diagnostics and semantic observations remain. The migration requires a complete ordered
provider stream with the same Session/correlation ID and verifies every removed event using
standard JSON decoding with exact numbers. Hashes are transient equality checks, never stored IDs.

Every result keeps its original EventEntry cursor/origin, protobuf unknown fields and boundary
record ID, fragment index/count, parent/correlation IDs and media type. Smaller JSON is distributed
across the original fragment slots. No entries or source references are deleted, no checkpoints are
reset, and no projection rebuild is required. Sampler intent JSON and its boundary fragments change
in one transaction. History uses ordinary kind-31 rows; packed delta/native blocks are untouched.

This is an explicit maintenance rewrite of otherwise immutable evidence. The matching source and
History copies must change together before exporters resume; ordinary ingress conflict checks
remain strict. Interrupted progress is safe to rerun: committed records already lack `events` and
incomplete record transactions roll back. Unknown shapes or missing/mismatched evidence fail
migration instead of deleting the only retained copy. Do not discard that error or mark Goose
completion manually. Output counts and integrity results, not raw provider payloads.

## Cutover

1. Publish and validate both service images from the implementation PR. Before allowing automatic
   rollout of those images, pause the `central-sampler` and `history-service` Deployments. Record
   original replica counts, images, pause states, database sizes and CNPG/Flux settings.
2. For the maintenance window, suspend the owning `agentplane-staging` Flux Kustomization. Confirm
   sampler has no unresolved effects/live operations and every current Session's History export
   checkpoint covers its source cursor. Gracefully scale sampler to zero, then History to zero.
   Native harnesses retain their journals and queue ingress during the History outage.
3. Create one-off Jobs from each service's existing Pod template, using the validated new image,
   existing service account/settings/Secret mounts and `args: ["--migrate-only"]`. These are the
   serving binaries and normal embedded Goose lifecycle, not separate migrator images. Run sampler
   migration 14 then History 17; keep both services stopped on any error. Verify completion in
   the existing Goose tables and matching compact records for every overlapping source/History ID.
4. Before vacuum, verify healthy physical/logical replication and at least 4 GiB free on each
   PostgreSQL volume. Temporarily set CNPG `max_slot_wal_keep_size` to the previously tested 8 GB.
   Monitor free space, retained WAL, replica lag and slot status; cancel maintenance if free space
   falls below 4 GiB, retained WAL exceeds 6 GiB, or a slot is lost/the retention setting reverts.
5. Run these statements individually on the discovered primary, outside a transaction:

   ```sql
   -- central_sampler
   VACUUM (FULL, ANALYZE) sampler_intent;
   VACUUM (FULL, ANALYZE) sampler_event;
   -- sandbox_service
   VACUUM (FULL, ANALYZE) session_event;
   ```

6. Wait for replication to catch up; restore the recorded CNPG settings. Restore replicas with
   the compact-audit writer images, resume Deployment rollouts and restore the original Flux state.
   Verify direct sampler follows, History pagination, export reconnection and exact duplicate
   replays, Electric, and fresh sampling/tool use/compaction for both providers. Record logical
   payload removal and physical table/database sizes separately.

## Contraction and rollback

Only after verification: publish the baseline/converter-removal PR. Preserve deployed Goose history
and advisory-lock identities. Fresh databases build the final schema; adoption accepts only the
completed rewritten predecessor, not older inline/reset schemas. Delete superseded migrations,
converter code, obsolete fixtures and otherwise-unused Go-migration plumbing.

The data rewrite has no down migration: deleting its history marker cannot restore removed arrays.
After success, use an image containing compact success writers and both data migrations if a serving
rollback is needed. Never roll back to writers that recreate full successful audit arrays. On a
failed rewrite, keep the cutover held, repair the reported evidence inconsistency and rerun; do not
resume one side against a differently rewritten copy.
