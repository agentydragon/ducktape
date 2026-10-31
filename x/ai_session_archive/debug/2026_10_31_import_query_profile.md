# Wyrm2 transcript import query profile

Investigated 2026-10-31 UTC against the deployed importer image
`devel-20261031043203-7564c2a`. A one-off Job ran the normal importer with
`cProfile` enabled. It used the existing Restic source and PostgreSQL ledger;
the profiler only added process-local statistics dumps. The Job was removed
and the scheduled importer was restored afterward.

## Measured work

The cumulative profiles were dumped with `SIGUSR1` at three points during the
import. Differences between dumps show distinct workloads, so these figures
locate costs in those intervals rather than establish a steady-state benchmark.
`cProfile` adds overhead, and its cumulative times should not be read as
unprofiled wall-clock predictions.

| Cumulative interval         | Work seen                                  | Relevant cumulative function time                                                                                                                                |
| --------------------------- | ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Start to first dump, 44.5 s | Catch-up metadata and two transcript files | Codex metadata read 14.6 s (10.6 s extracting and validating the SQLite image), title projection 10.0 s, `_process_file` 11.7 s                                  |
| First to second, 63.9 s     | One snapshot, ten transcript files         | `_process_file` 54.3 s; `restic.dump_to` 22.7 s; `_store_version` 29.0 s; `_get_payloads` 11.75 s in 112 batches; `_effective_payload_ids` 5.95 s in 108 batches |
| Second to third, 57.4 s     | Twenty-two transcript files                | `restic.dump_to` 40.9 s; `_previous_version` 6.89 s in 23 calls; `_get_payloads` 3.2 s in 29 batches                                                             |

In the second interval, repeated lineage lookup made 2,949
`Session.get(FileVersionRow, parent_id)` calls, approximately 5.08 s of the
profile. The first interval's two files made 857 ancestor `Session.get` calls
while looking for a dormant path's prior version, approximately 2.1 s. The
existing-payload lookup selected complete `RecordPayloadRow` objects, including
the parsed JSON body, although its caller used only ID, SHA-256 and raw bytes.
The second interval also spent 6.67 s in `_validate_jsonb` over approximately
3.04 million recursive calls; JSON storage is addressed separately. The title
lookup was approximately 0.1 s in that interval and is not part of this fix.

## Query changes

The importer now computes immutable parent-version ranks once per new file
version, instead of walking the same lineage for each 500-record batch. The
existing-payload query loads only SHA-256 and raw bytes (plus the implicit
primary key), retaining the raw-byte collision check. A recursive SQL query
finds the nearest prior path record in one database round trip, with the same
cycle guard, missing-parent behavior, deletion boundary, and independent
inventory boundary as the prior Python walk. No ledger schema or stored data is
changed.

Read-only `EXPLAIN ANALYZE` against one existing `wyrm2` path with 208 absent
ancestor records confirmed why the recursive lookup uses a lateral, one-row
parent probe. A plain join made PostgreSQL scan 445 machine snapshots on each
recursive step (9,108 shared buffer hits in the recursive branch). The lateral
form used 207 `restic_snapshots_pkey` probes (621 shared hits, 4 reads) while
the prior-path lookup used `snapshot_files_pkey`. This checks the query shape
on one historical path; it is not a measured end-to-end importer speedup.

The discovery ledger tests cover a dormant path, a deletion followed by an
unchanged snapshot, and an independent inventory without the path. The
modified, truncated/deleted, parent-link, and initial-import tests also pass.
No timing assertion is included.

## Reproducing the profile

Run the importer with the same image, arguments, Restic mounts and database
environment as the scheduled Job. Wrap its Python entry point with a
`cProfile.Profile` instance and a `SIGUSR1` handler that calls
`profiler.dump_stats('/tmp/import.prof')`; send that signal while the pod is
running and copy the dump before the pod exits. Inspect with
`python -m pstats /tmp/import.prof`, or load successive dumps with `pstats.Stats`
and compare cumulative call counts and times. Avoid parallel writers against
the same machine ledger.

The three raw dumps remain in task scratch, respectively
`/tmp/ai-usage-investigation/import-first.prof`, `import-second.prof`, and
`import-third.prof`, with SHA-256:

```
283b1fc6f06ed2c1cd0fb52a16a4f5361972a68d9c98dddba461ce35edcae107  import-first.prof
1c51bc3731384b2e5fa15ecf213f9731710de6b55b18cf88eb6a46e2dc2c98d7  import-second.prof
1be849b977329343f7748a6fb329288c02ed1958837d17dd52ada8b4299daaf0  import-third.prof
```
