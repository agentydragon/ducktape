# Wyrm2 importer OOM: buffered historical title projection

Investigated 2026-10-31 UTC. The importer image is
`devel-20261028181723-09b2e2d`. Status: candidate correction, with production-data
query validation; a corrected scheduled importer has not been deployed.

## Failure and causal evidence

`wyrm2` import containers repeatedly terminate with `OOMKilled`, exit 137, after
roughly 16–35 seconds. Their memory limit is 2 GiB. `rugged` uses the same image
and limit and completes. The node was not under MemoryPressure.

At the initial audit, PostgreSQL retained:

| Machine | Title observations | Snapshots with observations |        Raw title text |
| ------- | -----------------: | --------------------------: | --------------------: |
| wyrm2   |          2,194,559 |                         414 | 85 MiB, approximately |
| rugged  |            375,012 |                         183 | 46 MiB, approximately |

There are only **6,010 distinct `(native_session_id, source_path)` pairs** for
`wyrm2`. Repeated observations are provenance, not independent current titles.

`SnapshotIngestor.ingest()` imports the newest snapshot's Codex names, projects
titles, then begins historical transcript processing. The database showed 217
pending snapshots whose metadata had completed, while completed transcript
snapshots remained at 197, last captured October 28 15:15 UTC. This places the
stall after newest-metadata persistence and before transcript progress.

At approximately 04:18 UTC, a scheduled retry's database connection was observed
in `ClientWrite` on the unbounded `SELECT codex_title_observations...` query used
by `_project_codex_titles`. A later retry of that Job started at 04:18:32 and
OOM-killed at 04:18:48; complete transcript snapshots still numbered 197.
The failed containers emitted no Python traceback. Attempts to read their RSS
after locating them lost the short failure window; no exact production RSS
sample or allocator stack was recovered from those containers.

The source explains the allocation mechanism: `Session.execute()` on that ORM
query uses the default buffered result. Iterating it calls driver `fetchall()`
and constructs every historical observation object before the Python loop
selects a winner per current session. Merely spelling this as a `for` loop does
not stream rows. All 2.2 million observations are eligible for that buffer.

## Read-only profiling

Used the deployed web image's same model/SQLAlchemy runtime against the live
archive database, with `default_transaction_read_only=on` and a 60-second
statement cap. No transcript/title values or credentials were printed, no
projection updates were committed, and the full old result was not allocated in
the web container. Profilers: standard-library `cProfile` and `tracemalloc`;
process high-water RSS from `resource.getrusage`.

| Query                                           | Returned rows |            tracemalloc peak | Process max RSS |
| ----------------------------------------------- | ------------: | --------------------------: | --------------: |
| Existing query, diagnostic LIMIT 20,000         |        20,000 | 39,283,259 bytes / 37.5 MiB |     157,612 KiB |
| Candidate latest-per-pair query, entire history |         6,010 | 12,297,263 bytes / 11.7 MiB |      96,132 KiB |

The old sample's profile includes `sqlalchemy.orm.loading.chunks`, `_instance`,
`_raw_all_rows`, `_fetchall_impl`, and `psycopg.cursor.fetchall`. The candidate's
full-history query profile spends most of its time waiting for PostgreSQL and
constructs only the 6,010 selected objects. Its profiled run took 6.506 seconds;
this is one run, not a latency distribution or a benchmark of the whole importer.

Extrapolating the sample's Python allocation per row to the entire historical
result would exceed the 2 GiB container limit. That extrapolation is an estimate,
not a measured full-query peak. The source's buffering, observed active query,
profiler allocation path, successful SQL-side reduction, and failure-stage ledger
together identify the historical title projection as the OOM cause. Other
import stages have not been exhaustively memory-profiled.

Full profiler logs remain in the task scratch directory:
`/tmp/ai-usage-investigation/profile-title-buffered.log` and
`/tmp/ai-usage-investigation/profile-title-latest.log`.

## SQLite and index check

The local `wyrm2` Codex SQLite file is 162,803,712 bytes, approximately 155 MiB,
with 7,091 thread rows and 23 indices including the primary-key index. The
consistent staged backup image has the same file size and schema, with 7,087
rows at inspection. The schema includes time/recency, archived, cwd, provider,
source, section, and project indices.

The importer executes:

```sql
SELECT id, rollout_path, name, title FROM threads;
```

SQLite reports `SCAN threads`. Since this query deliberately reads every thread,
an additional index would not reduce its requested row count. The cursor is
iterated, and the image itself is extracted and hashed on disk. This small scan
does not explain the multi-million-row Python buffer in the later PostgreSQL
projection. No Codex-owned SQLite schema or index was modified.

PostgreSQL already has the title-observation primary key
`(machine_id, snapshot_id, native_session_id)` and an index on
`(machine_id, native_session_id)`. `EXPLAIN` for the candidate uses that index,
memoized snapshot lookups, an incremental sort and `Unique`. No index migration
is required for this correction. PostgreSQL still visits historical rows to
choose winners; this change removes their transfer and materialization in the
importer, not every cost of retaining history.

## Candidate correction and invariants

Select `DISTINCT ON (native_session_id, source_path)` ordered by the existing
winner rank: capture time, snapshot ID, title, all descending within each pair.
Older rows of a pair cannot win because matching eligibility depends only on
native ID/path and the current session state. Keep the ledger unchanged.

Preserve these boundaries:

- Different physical paths with the same native ID remain separate candidates.
- Empty-path/index-only observations remain separate from path-specific ones.
- Existing ID mismatch, conflict and ambiguity checks still run.
- Index-only projection remains gated on complete inventory.
- Current-name clearing retains its previous inventory-completeness gate.
- Observation history and transcript data are never deleted.

The small correctness fixture checks that a newer index-only observation cannot
rename two ambiguous physical transcripts and that unambiguous index-only names
wait for complete inventory. No performance regression test is included, per the
user's request.

`iguana` is outside this incident: its Restic repository does not exist, which
the user confirmed is expected because that machine is almost never used. No
repository provisioning or repair is planned for it.

## Acceptance after merge

After the new importer image is published and pinned through GitOps, verify a
scheduled `wyrm2` import survives title projection, advances transcript snapshot
watermarks past October 28, and drains the backlog without OOMs. A green test or
a read-only query pass does not establish that deployed recovery.
