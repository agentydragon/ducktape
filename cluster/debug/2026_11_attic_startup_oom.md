# Attic restart and garbage-collection investigation (November 2026)

The `nix-cache/attic-next` container repeatedly hit its 4 GiB memory limit on
November 9. The OOM cause is not established. At the 01:47:07.897 UTC cAdvisor
scrape, it used about 4,080 MiB (1,769 MiB RSS and 2,019 MiB cache); it was
OOM-killed at 01:47:37. Its replacement reached about 2,481 MiB at
01:48:04.512 and was killed at 01:48:23. A later OOM kill occurred at
02:10:09; the previous-container log had no final diagnostic message.

PostgreSQL's `pg_stat_database.tup_fetched` rose by approximately 1.4 million,
2.26 million, and 2.26 million rows in the minute buckets ending at
01:48:15.513, 01:49:15.513, and 01:50:15.513 UTC. Those are database-wide
counters, not query or caller attribution. `pg_stat_statements` was unavailable,
and gateway telemetry did not identify Attic request methods or routes.

Monolithic Attic starts GC immediately after migrations; the configured
`30 days` interval separates runs rather than delaying the first one. The
November 9 deployment enabled `RUST_LOG=error,attic_server::gc=info` without
suppressing other targets' errors. On the replacement pod, GC started at
22:48:07.465 UTC. Its orphan-chunk phase emitted **10,328** `Deletion failed:
Storage error: No such file or directory` warnings between 22:49:03.586 and
22:49:03.747, then reported `Deleted 0 orphan chunks`. Earlier counts of
1,999 were truncated by `kubectl logs --tail=10000`.

Read-only inspection of the Attic database found exactly 10,328 chunks in
Deleted (`D`) state, with zero holders and no `chunkref` links. Their recorded
file sizes summed to 121,679,899 bytes. Of these, 10,278 were created on
November 8 from 23:00 to 23:25 UTC. Every one of the 10,328 UUID `.chunk`
paths was checked on the mounted local storage and was absent; sampled Valid
chunk files were present. A guarded transaction loaded only those verified
missing-file IDs, locked and rechecked their `D` state, zero holders, and lack
of references, then deleted exactly 10,328 **metadata rows**. It removed no
storage files or cache objects. The primary subsequently reported zero `D`
rows, and Attic remained Ready with zero restarts. GC was not forced, so the
next real GC pass has not yet verified that the warnings stop.

The [upstream GC implementation](https://github.com/zhaofengli/attic/blob/7a19204df10d606c5070e6bb72615c3461900c05/server/src/gc.rs)
marks orphan chunks `D`, removes their files, then deletes database rows for
successful removals. The [local storage backend](https://github.com/zhaofengli/attic/blob/7a19204df10d606c5070e6bb72615c3461900c05/server/src/storage/local.rs)
propagates `ENOENT` from `remove_file`, leaving an already-missing file's `D`
row to fail again on later passes. The deployed image is digest-pinned but has
no source-revision label; its observed behavior matches this mechanism. A
process kill between file removal and the final database delete could produce
such rows, but the file-loss event and originating cache cannot be established
from the available logs or schema. The `chunk` table has no cache owner or
state-transition timestamp. A preventive upstream change would treat `ENOENT`
as a successful local deletion while preserving other I/O errors, with a test
that reaps a `D` row whose file is already missing.

For any new OOM, align timestamped GC logs with per-container RSS/cache and
memory-event series, PostgreSQL query activity, and CI push times. The retained
Loki pod-log stream can cover deleted pods; `kubectl -n nix-cache logs
deploy/attic-next --previous --timestamps` covers only the immediately
preceding container. HTTP tracing was not enabled because Attic's Tower layer
logs the full URI, including arbitrary query strings. A sanitized route-level
counter or upstream instrumentation is needed to distinguish GC database work
from lookups and uploads when they overlap.
