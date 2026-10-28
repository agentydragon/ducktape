# Action storage version 6 cutover evidence

## Scope and observed database

On 2026-10-28, read-only inspection of the sole instance, `agentplane-staging`, found
Goose versions 1–5 applied. The four pending storage PRs (#12360, #12362, #12371 and
#12416) had not run there. Versions 1–5 are retained unchanged; their unpublished
expand/write/contract successors are replaced by one transactional Go migration 6.

The observed data comprised 7,332 requests, 36,552 events and 7,299 decisions,
including 2,439 human-operator decisions. Non-null policy evidence occupied
10,511,006 logical JSON bytes over 4,860 rows (maximum 2,888 bytes per row).
The 33 non-null actor objects all used the caller account shape. Action identity
JSON occupied 312,824 logical bytes. These are point-in-time observations, not
limits on future database growth. No live data was changed during this inspection.

## Preservation and failure tests

The real PostgreSQL integration test applies the production version 1–5 scripts,
seeds legacy rows and invokes the production migration. It checks identity,
arguments, human descriptions and decisions, caller/operator actors, all five
policy kinds, evidence bindings/sets/repository and SQL/JSON null handling.
It also tests concurrent migration, malformed evidence causing complete rollback,
repair and retry, and deterministic cancellation during an UPDATE after schema
expansion has started. Fresh/concurrent/repeated CLI initialization is covered
separately by `migrations:test_migrations`.

The native test passed on RBE:
[invocation fd66db64-d208-4f40-add9-414871cd3f70](https://app.buildbuddy.io/invocation/fd66db64-d208-4f40-add9-414871cd3f70).

## Timed populated conversion

The standard Go benchmark `BenchmarkNativeStorageCutover` seeds a version 5 database
outside its timed region, then times only the production version 6 transaction.
One RBE iteration used 10,000 requests, 50,000 events and 10,000 decisions, with
24,240,000 logical bytes of legacy policy evidence. It completed in **6.46 seconds**
and generated **25,966,400 bytes of PostgreSQL WAL**. Go reported 688,214,152 allocated
bytes in 3,175,465 allocations; total allocations are not peak resident memory.
The worker had an AMD EPYC CPU and 7,276 MB memory and used the PostgreSQL 18 Docker
fixture. This fixture exceeds the observed row counts and total evidence volume;
it does not reproduce staging hardware, cache state, contention or a latency bound.

[Benchmark invocation 2ae73ffb-aa94-4d46-ae89-6e96791f0e19](https://app.buildbuddy.io/invocation/2ae73ffb-aa94-4d46-ae89-6e96791f0e19).

The benchmark target belonged to the version 6 cutover image and was retired with the
historical converter in version 7. The invocation above preserves its measured result;
[version 7 baseline verification](2026_10_28_final_schema_baseline.md) covers the new migrator.

## Rollout boundary

The existing Actions Deployment uses `Recreate`, but independently pins its server
and migration images. Its old service image lacks the migrator binary. First
publish and deploy #12655, which adds that binary without changing the Deployment.
The cutover then switches init and serving to one image pin and removes the separate
migrator image. Do not merge the cutover before the prerequisite service image is
deployed. Retain a pre-cutover database backup. Verify migration 6, row preservation
and application readiness after deployment; local and RBE tests do not prove live
rollout success. This coordinated conversion intentionally has an Actions outage
while the transaction holds exclusive locks. Old JSON storage images cannot be
used after commit without a coordinated database restore.

## Verified staging cutover

PR #12360 merged as `030ac3944c708e6ac763052f6cfe809c880ede81`. The image containing
that migration reached staging as `devel-20261028133355-0cfeb66` on 2026-10-28. Both
migration init containers exited successfully between 13:37:47 and 13:37:53 UTC
(4 and 5 seconds including connection/lock waiting); both serving replicas became
ready with zero restarts. Goose version 6 is applied, and Flux reports Ready and Healthy.

Read-only pre/post queries produced identical counts and MD5 fingerprints for all
non-converted fields, ordered by primary key:

| Table             |   Rows | Fingerprint                        |
| ----------------- | -----: | ---------------------------------- |
| `action_request`  |  7,332 | `c64b6bd2fd3eaff82e872f06662e2d1a` |
| `action_event`    | 36,552 | `82422daf39210c0b1af8f7e560a191ab` |
| `action_decision` |  7,299 | `c7ac8cb0940bab03a2410d5445e4d1ea` |

The fingerprints hash `to_jsonb(row)` after excluding the converted columns, then
aggregate each row hash in primary-key order. This includes human notes, descriptions,
arguments, state and timestamps. The separately normalized `[group,name]` identity
fingerprint also matched (`58494bd09b107dc007d49432b8e655e6`). All 2,439 human
decisions remain. Non-null converted actor/evidence counts match the old source
counts (33 and 4,860), with no empty binary snapshots. All three retired JSON columns
are absent. These live checks complement the migration's full-field conversion tests;
they do not replace an authenticated operator workflow test.
