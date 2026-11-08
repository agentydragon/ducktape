# Restic session backups: SQLite storage and growth

Investigation, 2026-11-08. The question is whether archiving SQLite changes
logically would use less storage than capturing database files and consistent
images every 15 minutes. One interval has been compared at the row level; no
logical-change archive or restore comparison has been implemented yet.

## Latest snapshot contents

Restic `ls --json` inventories logical file sizes, not compressed or unique
repository bytes. These snapshots still used the old client configuration.

| Snapshot                     | All files | Raw SQLite and sidecars | Staged SQLite images | SQLite total | Share of logical files |
| ---------------------------- | --------: | ----------------------: | -------------------: | -----------: | ---------------------: |
| Wyrm2 `d0aea118`, 11:16 PST  | 55.29 GiB |                9.40 GiB |             9.34 GiB |    18.74 GiB |                  33.9% |
| Rugged `017c8a95`, 11:00 PST | 16.64 GiB |                0.84 GiB |             0.80 GiB |     1.65 GiB |                   9.9% |

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

| Newly packed data                              |  MiB | Share of snapshot's added packed bytes |
| ---------------------------------------------- | ---: | -------------------------------------: |
| Referenced by SQLite databases or sidecars     | 61.6 |                                  78.7% |
| Of which: `thread_history_1.sqlite`            | 38.8 |                                  49.6% |
| Of which: `logs_2.sqlite` and sidecars         | 18.5 |                                  23.6% |
| Other data and tree/pack accounting difference | 16.6 |                                  21.3% |
| Total added packed data                        | 78.2 |                                   100% |

The SQLite-associated blobs total 432.6 MiB uncompressed and 61.6 MiB packed,
approximately 7:1 compression. Blob IDs shared between raw and staged files
were counted once. A chunk shared with another category would still be counted
as SQLite, so this is attribution by reference, not an exclusive-reclaim figure.
The sample covers root-level Codex `.sqlite` files and sidecars plus their
staged images; it does not attribute any other SQLite paths. It is one backup,
not a representative rate or a measurement across all historical snapshots.

## Actual table changes versus Restic, one interval

On Wyrm2, compare the consistent `thread_history_1.sqlite` images in snapshots
`2fad384d` (12:16 PST) and `19699d75` (12:30 PST). Both manifest digests matched
the images, and neither manifest reported errors. The previous image was held
open read-only while the client atomically replaced its staged image; the
replacement was then attached read-only. Both images were closed afterward.
No row contents were emitted or saved.

| Database table                    | Inserted rows | Updated rows | Deleted rows | Inserted and changed value bytes |
| --------------------------------- | ------------: | -----------: | -----------: | -------------------------------: |
| `thread_items`                    |         2,413 |            0 |            0 |                        8,470,113 |
| `thread_turns`                    |           121 |            6 |            0 |                           29,158 |
| `thread_history_projection_state` |             2 |           24 |            0 |                              385 |
| Other tables                      |             0 |            0 |            0 |                                0 |
| Total                             |         2,536 |           30 |            0 |             8,499,656 (8.11 MiB) |

Rows were joined by each table's primary key, comparing all columns with
SQLite's null-safe `IS NOT`. Inserted bytes sum the values of inserted rows;
updated bytes sum the **new values of changed columns**. Text/blob values are
counted as SQLite's byte representation; numeric values are cast to text for
this approximation. This excludes change-log framing, previous values,
transaction boundaries and SQLite page/index overhead. The 2,413 inserted
`thread_items` rows contained 8,036,439 bytes of `item_json`.

The matching backup wrote 877 new data blobs, matching the count in index
`41b23ccaa9cfc9e3893431f0bb7fdf37b0cebdafb4dbc0876f37c3cfcd1e68d9`.
Attributing the indexed blobs by file-tree references gives:

| Data for the interval                                                          | Uncompressed |    Packed |
| ------------------------------------------------------------------------------ | -----------: | --------: |
| New chunks referenced by the consistent thread-history image                   |   184.46 MiB | 34.97 MiB |
| New chunks referenced by raw or consistent thread-history images, deduplicated |   220.46 MiB | 41.54 MiB |
| All SQLite files and sidecars found at the Codex root or staging root          |            — | 61.09 MiB |
| All data and metadata added by the backup                                      |            — | 83.07 MiB |

The consistent image's packed chunks were 4.31 times the uncompressed inserted
and changed values, while its uncompressed chunks were 22.76 times those values.
The database file itself grew 9.80 MiB. This shows amplification in this
interval, not that a complete logical archive would cost exactly 8.11 MiB: it
would need its own serialization, compression, consistency, and recovery
mechanism. SQLite page and index changes and Restic chunk boundaries are
plausible contributors, but their individual costs were not measured.

As a compression probe, the last 2,413 rowids in the new image had the same
row count, total value bytes and `item_json` bytes as the observed inserts.
Serializing those candidate rows as JSONL yielded 8,855,782 bytes and gzip
compressed to 1,880,719 bytes. Their exact primary keys were not retained from
the join, so the candidate set is not proven to be the inserted set. This is a
compression hint, not a measured complete or recoverable logical delta.

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
