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

Reproduce with Bazel on RBE:

```sh
bbr test //agentplane/action_service/migrations:native_storage_integration_test \
  --test_arg=-test.run=^$ \
  --test_arg=-test.bench=BenchmarkNativeStorageCutover \
  --test_arg=-test.benchtime=1x --test_arg=-test.benchmem --test_output=all
```

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
