# Restic session backups: SQLite storage and growth

Investigation, 2026-11-08. The question is whether archiving SQLite changes
logically would use less storage than capturing database files and consistent
images every 15 minutes. No logical-delta measurement or restore comparison has
been performed yet.

## Latest snapshot contents

Restic `ls --json` inventories logical file sizes, not compressed or unique
repository bytes. These snapshots still used the old client configuration.

| Snapshot | All files | Raw SQLite and sidecars | Staged SQLite images | SQLite total | Share of logical files |
| --- | ---: | ---: | ---: | ---: | ---: |
| Wyrm2 `d0aea118`, 11:16 PST | 55.29 GiB | 9.40 GiB | 9.34 GiB | 18.74 GiB | 33.9% |
| Rugged `017c8a95`, 11:00 PST | 16.64 GiB | 0.84 GiB | 0.80 GiB | 1.65 GiB | 9.9% |

The staging totals include a tiny manifest. SQLite identification here is by
`.sqlite`/`.db` filename and sidecars; each inspected snapshot has seven raw
SQLite databases and seven staged images. The Wyrm2 raw and staged
`thread_history_1.sqlite` files are each 9.11 GiB, almost all of that host's
SQLite total. These two logical copies share most Restic chunks: an earlier
snapshot shared 17,477 of about 17,600 chunks in each copy. Chunk counts are
not byte counts, and neither the logical total nor this fraction is a physical
storage or reclaim estimate.

Wyrm2's session journals grew from 18.12 GiB in its first retained snapshot on
October 25 to 35.20 GiB on November 8. They are the raw conversation archive;
`thread_history_1.sqlite` is a projection, but the completeness and recovery
semantics of replacing the database with a rebuild have not been proven. See
[the backup contract](../README.md#why-codex-session-files-remain-included).

## One 15-minute packed-data sample

The Wyrm2 snapshot `d0aea118` began at 11:16:03 PST, following `dd397bad` at
11:01:01. Its summary reports 82,002,590 bytes (78.2 MiB) of added packed data,
including tree data. The corresponding index was
`c78b96039e923ea231209e15c5bf5502372c64c807fafac61a6aff2fd71e56a2`: its 891
new data blobs match the snapshot's `data_blobs` count. Comparing their IDs with
the snapshot's raw and staged SQLite file trees gives:

| Newly packed data | MiB | Share of snapshot's added packed bytes |
| --- | ---: | ---: |
| Referenced by SQLite databases or sidecars | 61.6 | 78.7% |
| Of which: `thread_history_1.sqlite` | 38.8 | 49.6% |
| Of which: `logs_2.sqlite` and sidecars | 18.5 | 23.6% |
| Other data and tree/pack accounting difference | 16.6 | 21.3% |
| Total added packed data | 78.2 | 100% |

The SQLite-associated blobs total 432.6 MiB uncompressed and 61.6 MiB packed,
approximately 7:1 compression. Blob IDs shared between raw and staged files
were counted once. A chunk shared with another category would still be counted
as SQLite, so this is attribution by reference, not an exclusive-reclaim figure.
The sample covers root-level Codex `.sqlite` files and sidecars plus their
staged images; it does not attribute any other SQLite paths. It is one backup,
not a representative rate or a measurement across all historical snapshots.

For context, 90 Wyrm2 backups between November 7 10:45 and November 8 10:16
PST added 40.35 GiB of uncompressed new repository data and 6.47 GiB packed,
across all file types. Intermediate versions contribute to that total; an
endpoint-only diff does not measure all churn during the interval.

## Scope and next measurement

- On November 8, both workstations still ran the old client after
  [PR #14961](https://github.com/agentydragon/ducktape/pull/14961) merged. That
  PR excludes disposable caches, packages, and the diagnostic log database from
  future captures once deployed; it does not rewrite historical snapshots.
- In five sampled historical snapshots per host, including the first and latest,
  no files appeared under the already excluded Codex `worktrees/` or Claude
  `session-env/` paths. This is not an inventory of every snapshot.
- Measure several active and idle intervals on Wyrm2 after the exclusion is
  deployed. For each interval, select the two consistent staged SQLite images,
  verify their manifest digests and integrity, and compare the relevant tables
  using schema-aware primary keys. Count inserted, updated and deleted rows and
  the bytes of changed values, without publishing row contents. Define whether
  repeated edits, deletions, metadata and transaction boundaries must be
  recoverable before calling any representation redundant.
- Compare those logical changes with new Restic data blobs, both uncompressed
  and compressed, referenced by the same databases across **every** intervening
  snapshot. Include tree/index/pack overhead and chunks shared with other files;
  avoid summing overlapping per-file totals. Measure CPU, read I/O, and restore
  time as well as bytes. Net database file growth and an endpoint-only chunk
  diff are not substitutes for a logical change history.
- Compare a journal-first rebuild, less frequent consistent database images,
  and a logical change archive against the existing recovery contract. Preserve
  original journals, user memory, historical observations and consistent
  restore points; test interrupted capture and restoration before changing
  retention or pruning anything.

For reproducibility, `restic snapshots --json`, `restic ls --json <snapshot>`,
`restic diff --json <before> <after>`, and `restic cat tree <snapshot>:<dir>`
can inspect snapshot and file-tree metadata. `restic cat index <index-id>`
exposes blob IDs and packed lengths for attribution. The latter tree/index
format is an internal representation; record the Restic version and avoid
assuming one index per snapshot. Restic's snapshot summaries are a useful
cross-check, but `stats --path` filters snapshots by backup source path rather
than selecting individual files inside a snapshot. Use the documented restore
procedure and avoid copying credentials or publishing conversation contents.
