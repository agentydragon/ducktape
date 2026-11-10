# Bootstrap event storage comparison (2026-11-04)

**Decision:** Keep the current COSI event log while the actual output distribution and user-facing
need for retained output remain unknown. If that evidence justifies a storage change, a separate
SQLite event log with an indexed sequence and one durable transaction per event is a credible
candidate. The matched synthetic probe below shows lower restart memory and disk use, but similar
append time on this host. This report neither changes storage nor sets an output cap.

## Current contract and observed demand

[`bootstrap.Log`](../bootstrap/log.go) creates one `BootstrapEvents` resource for each stdout or
stderr read and one terminal result or interruption. [`scripts.Runner`](../../scripts/scripts.go)
reads both pipes concurrently into buffers of at most 64 KiB; actual reads can be smaller.
`cosistate.Open` loads every persistent resource into its in-memory COSI state at Runtime startup.
At the **pre-cleanup revision measured here** (`8b45c092`), `OpenLog` then lists, clones, sorts,
and validates every event, while `Since` repeats that full scan before filtering a suffix. The
preceding terminal-result cleanup removes the production `Since` call and method. It avoids the
second scan during terminal recovery but leaves COSI's eager payload load and the initial full
`OpenLog` replay. The [earlier 100 MiB COSI probe](2026_11_02_bootstrap_cosi_storage_probe.md)
measured a 1.99 GB process high-water RSS for full replay; that is a synthetic stress case, not a
production observation.

At the measured revision, the only production call to `Log.Since` was
`bootstrapErrorForLog`, which retrieved the terminal event after `OpenLog`; tests were its other
callers. The preceding cleanup removes this path. The host passes output bytes to `AppendOutput`,
and no production RPC or delivery path reads those bytes back. The
published contract retains the full prefix, but live output streaming is prospective work. The
terminal event and durable selection are the authorities for the one-shot attempt: after a restart,
a selected attempt without a terminal event is recorded interrupted, never rerun. A failed event
write poisons the writer because COSI may have committed to disk before returning an error.

A read-only `kubectl get sandboxes -A` on 2026-11-04 returned no active Sandbox resources, and no
running Runtime store was available for a content-free count of event sizes. This says nothing
about historical output on retained volumes. We have no measured distribution of total output,
chunk sizes, event counts, or production write latency. An inventory should record only those
numbers per Sandbox, with no script text or output bytes, before claiming a capacity or latency
benefit. It should include completed and interrupted attempts and account for retained volumes.

## Matched synthetic probe

An uncommitted Go probe under the task scratch directory used the production `cosistate.Open`,
`bootstrap.OpenLog`, `AppendOutput`, `AppendResult`, and pre-cleanup `Since` APIs. Its SQLite branch used the
same `InitializationEvent` protobuf bytes, `sequence INTEGER PRIMARY KEY`, one `BEGIN IMMEDIATE`
and `COMMIT` per event, `journal_mode=WAL`, and `synchronous=FULL`. It synchronized the database
directory after creation. Both branches alternated stdout and stderr with a repeated, printable
64 KiB payload and committed the same successful terminal event. It checked every sequence,
stream, payload, and terminal result on full replay. The SQLite full replay decoded rows one at a
time; COSI's `Since` returns all of them. A fresh process performed every restart read. The Go
binary was built with `bb run`; the scratch source, synthetic databases, and raw profiles are not
part of this PR.

The host was Linux 6.18.55, Go 1.26.8, and `/tmp` on ext4. The write timer covered serialized
event appends and the terminal commit, excluding database creation and close. The restart timer
included opening the store and reading/validating events, excluding process startup and close.
The OS page cache was not cleared, and host activity was not controlled. The Go probe sampled
`HeapAlloc` every 5 ms and read process `VmHWM`; those are benchmark telemetry, not profiler
attribution. Separate Go heap profiles were captured with `runtime/pprof` while stores remained
open. Three paired 1 MiB writes and one paired 10 MiB write were run; larger and small-chunk
cases were not rerun head to head. All figures below are rounded; MiB use binary units, while
heap/RSS use decimal MB.

| Workload and measure                                  | COSI resources | SQLite WAL/FULL |
| ----------------------------------------------------- | -------------: | --------------: |
| 1 MiB, 16 chunks: append, three samples               |    2.27–3.27 s |     2.07–2.64 s |
| 1 MiB: allocated database after close                 |       1.44 MiB |        1.02 MiB |
| 10 MiB, 160 chunks: append, one sample                |        23.98 s |         22.32 s |
| 10 MiB: allocated database after close                |      10.63 MiB |       10.09 MiB |
| 10 MiB: open and full decode/validation, one sample   |         282 ms |           81 ms |
| 10 MiB: peak sampled heap during full replay          |        61.2 MB |          4.1 MB |
| 10 MiB: process high-water RSS during full replay     |         235 MB |           60 MB |
| 10 MiB: open and terminal lookup, one sample          |         217 ms |         9.1 ms* |
| 10 MiB: peak sampled heap during terminal lookup      |        64.9 MB |         1.6 MB* |
| 10 MiB: process high-water RSS during terminal lookup |         245 MB |          47 MB* |

\* The SQLite terminal lookup scanned **sequence keys only** for density, then decoded the last
row. COSI `OpenLog` decoded and validated every protobuf, including observation type, stream,
and terminal placement. These timings do **not** establish an equivalent integrity check. A
SQLite startup that preserves the current full-validation guarantee must stream and validate all
rows; the 81 ms full replay is the closer measured comparison. After validation, ten indexed
one-row suffix queries averaged 0.42 ms in this sample, versus 22.1 ms for COSI `Since`. The
suffix measurements are API costs, not evidence of a production polling loop.

The live Go heap profile for 10 MiB COSI replay attributed 10.9 MiB of in-use heap to protobuf
byte unmarshalling through the COSI backing load; the SQLite streamed-read profile had no
payload-sized retained allocation. These are sampled in-use profiles and do not account for all
allocations or RSS. Write times varied, and the two implementations have different buffering and
validation work. The experiment supports a bounded-memory indexed design, not a production
throughput prediction or an unconditional migration.

## Design if the inventory warrants a change

Keep `BootstrapSelections` and unrelated retained COSI resources in `runtime-resources.db`. Use a
separate SQLite database under the same persistent `StateDir` for full protobuf events. A
`sequence INTEGER PRIMARY KEY` index permits bounded `WHERE sequence > ? ORDER BY sequence LIMIT ?`
reads and a direct terminal lookup. Store an explicit event kind with each row to validate
terminal placement without loading payloads; validate every protobuf at append and during
startup migration, and stream full validation at restart if the present corruption-detection
contract must remain exact. A transaction must insert the event and update the head/terminal
metadata together. Recovery rejects gaps, events after a terminal, invalid observations, and
metadata disagreement. This is one attempt per Sandbox; sequence allocation must stay under one
writer lock, with no overwrite or `INSERT OR REPLACE` recovery shortcut.

`WAL` plus `synchronous=FULL` commits each event before acknowledging it. Its write-ahead log is
part of the durable database; backup and restore must use SQLite's online backup mechanism or
another consistent snapshot of the database and WAL, never copy only the main file while it is
live. On a failed or cancelled commit, the writer must stop accepting further events and stop the
script; restart resolves whether the event landed before appending any interruption. A missing
terminal after a previously selected script remains outcome-unknown and becomes interrupted,
without replay. Disk-full, corrupt, locked, and unreadable stores fail closed rather than report
success or discard output. No cap or truncation policy is implied.

The existing pipe readers serialize callbacks at the log writer, so event sequence reflects
successful append order, not a total ordering of the two operating-system pipes. A future live
stream would publish only committed cursors, page from the durable log after reconnect, and bound
reader memory and send capacity. This is a separate API/consumer decision; no such stream is
currently wired.

### Data-preserving cutover and rollback

Use a one-way, exclusive startup or offline migration before any Runtime controller or script
starts. Back up the complete persistent state,
including the COSI database and every Session journal, with a restore drill. While no writer is
active, copy the COSI event prefix to SQLite, validate every ordered protobuf and its terminal,
and commit the rows plus a complete-copy marker in one SQLite transaction. Reopen and compare the
copy with its source before allowing the new version to write. A crash before the marker leaves
the COSI source authoritative for rerunning the migrator; after verified completion the new
version uses only SQLite. Keep the COSI source data. This avoids a live dual-writer or
cross-database transaction claim; no historical event is deleted merely because a copy exists.

An unfinished selected attempt must resolve to a durable interruption without rerunning its
script before the source is copied. If this cannot be done safely at the exclusive cutover, the
migration must stop. A rollback to a COSI-only binary **after any SQLite write** would require a
separately designed and tested reverse restoration while Runtime is stopped; simply reverting
the binary would ignore new output or outcome events. Roll forward or explicitly restore data
before that rollback. Do not drop or recreate an authored volume, journal, or unrelated resource.

Before a cutover, test commit-error ambiguity, process death between every copy/marker step,
reopen after WAL recovery, corrupt/gapped history, concurrent tail readers, disk-full behavior,
and backup/restore of both formats. Measure actual per-Sandbox output and event-count tails and
repeat the append, restart, and read experiment with representative chunk distributions. The
current COSI design remains simpler while those measurements and a real output consumer are
absent; the preceding terminal-result cleanup removes the second scan but not COSI's eager
payload load or initial full replay at Runtime startup.
