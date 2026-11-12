# Native content storage cutover

Migration 13 is one resumable Go migration in the serving binary. It converts sampler-owned
conversation records, active contexts and historical provider intents, validates reconstructed JSON,
then retires the old inline context storage. It preserves Session/intent identities, commands,
pending effect outcomes, journal sequence numbers and History export checkpoints. It never sends
provider or executor requests. History's database is outside this migration.

## Before cutover

1. Require the PR's PostgreSQL integration and provider blackbox tests to pass. Rehearse migration
   and restore against a recent sampler database copy, including a second run after interruption.
2. Record table/TOAST/index sizes and available database/WAL headroom. Conversion initially retains
   old and new representations; deleting old JSONB values does not immediately release filesystem
   space. Record expected context/request counts and the latest History export checkpoints.
3. Take and verify a restorable sampler database backup and retain the current image digest.
4. Hold GitOps/image automation for `central-sampler`, stop all sampler writer replicas, and wait
   until conversation ownership leases expire. Admission and sampling are unavailable during this
   maintenance window. Keep the Dispatcher, executor storage and History data intact.

## Convert and verify

Run `/usr/local/bin/central-sampler --migrate-only` from the candidate serving image using the
existing `AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL` Secret. This mode needs no provider credentials
or serving configuration and has a two-hour migration budget. Do not run old writers concurrently.
Normal startup refuses migration 13 on a populated database; this is an explicit writer cutover.

If interrupted, repeat the same command. Completed content/context rows and converted roots are
reused. A conversion error preserves the source representations and stops before retirement;
investigate the reported record before retrying. Goose version 13 is recorded only after success.

Verify version 13, retained Session/intent identities, context/request counts and unchanged export
checkpoints. Compare selected reconstructed OpenAI and Anthropic requests with the rehearsal copy,
including signed thinking, images, tool results, compacted contexts and unresolved intents.

Reclaim dead tuple/TOAST space while writers remain stopped. Inspect sizes first: `VACUUM FULL`
requires an exclusive lock, temporary space for the rewritten relation and WAL headroom. Rewrite
only tables whose measured dead space warrants it, one at a time (`sampler_intent`,
`sampler_conversation`, `sampler_event`); never launch a database-wide rewrite blindly.

Deploy the candidate image, restore the desired replica count and resume GitOps ownership. Verify
readiness, ordinary input/tool/compaction turns, reconstructed request reads, History checkpoint
advancement, and database growth over several appending turns. Restarts preserve pending effects;
they do not establish that uncertain external operations completed.

## Rollback

Before restoring writes, restore the verified sampler backup with the old image if conversion or
acceptance fails. There is no destructive down migration. After accepting new writes, reverting
only the image cannot read the new schema; stop writers and plan a data-preserving reverse conversion
or explicitly accept the exact post-backup loss before restoring. Retain backups until acceptance.

Legacy journal payloads remain logically exact for History replay and are compressed at rest.
They still contain their historical full contexts; this change prevents new quadratic request
copies and converts historical intents, but does not delete or rewrite retained History evidence.
