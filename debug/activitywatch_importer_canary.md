# ActivityWatch importer canary evidence

Captured during the first live pull-only importer canary on 2026-07-21 UTC.

## Status

The canary failed correctness gates. Keep both the Flux Kustomization
`activitywatch` and CronJob `activitywatch-importer` suspended.

The central ActivityWatch database was empty before the canary. It now contains partial,
non-canonical imports and must be rebuilt before another test. The canary Job completed
successfully from Kubernetes' perspective; that only means the process exited zero.

## Frozen source set

Syncthing was scaled to zero before the source snapshot. The inbox contained four
canonical databases and no conflict copies:

| Source directory                       | Device | Pre-canary SHA-256                                                 |
| -------------------------------------- | ------ | ------------------------------------------------------------------ |
| `15c10090-44ad-4ed6-963f-9b66e35d2055` | rugged | `3cc9658fa89587557c6284aad138b667429cecb64d95f6dde3526e4ed348e567` |
| `a1627407-d5d1-4481-be32-b9a9906d1b5f` | wyrm2  | `3ef2fda85a33467ba6a4a36f3e28d8afa09034d02c2175f04b9d42de2c6c313a` |
| `ca3ce270-b219-45a1-9d4f-9688d1fe3e40` | iguana | `d508b77417c931647f09afb175fd49e24ab91537864ba76d16a341be8f525044` |
| `f88539b2-fd4d-4c2e-bac9-407461228bab` | atlas  | `21f6701573ee64e6a0b92683a8cf4ffcf25a45c21f259ea274c65786956b366b` |

The canary ran:

```text
aw-sync --host activitywatch-write.activitywatch.svc.cluster.local \
  --port 5600 --sync-dir /sync-inbox sync --mode pull
```

## Finding 1: pull-only mutates the inbox

The logs showed schema migrations on all four remote databases. Every source hash changed.
The process also created `/sync-inbox/activitywatch-cluster/test.db`, even though the
selected mode was pull-only.

| Path                        | Post-canary SHA-256                                                |
| --------------------------- | ------------------------------------------------------------------ |
| rugged `test.db`            | `2d6eb3f1ab8309a18db604df2a03c90258cebc49702e676542abd291977d6648` |
| wyrm2 `test.db`             | `92e3ff6c135f84e6fe3821da7a20b50d70cd5c22e8e14e6d122924cf36608f3a` |
| iguana `test.db`            | `09b6509556d849350cda8bf8077f4609a2f9f3bbe18fa077afaa105b9014972a` |
| atlas `test.db`             | `ffb685bb0d6012cd0b1b04043f61b6727430b45434ae6a3b698503b49686fe02` |
| generated cluster `test.db` | `22067c0358353bc1bb816fa78b4198ff408c513b8d00977ae1e7ca406e53b72a` |

Cause in the pinned upstream implementation:

- `sync_run` unconditionally calls `setup_local_remote`, which creates
  `<sync-dir>/<local-device-id>/test.db`;
- remote files are opened through `Datastore::new`;
- the datastore worker opens SQLite read-write, enables WAL, and runs migrations.

A receive-only Syncthing folder does not make these writes harmless. Syncthing preserves
receiver-local modifications as conflicts when a peer supplies a different version.

## Finding 2: browser provenance collides

Both rugged and wyrm2 have bucket `aw-watcher-web-chrome_localhost` with hostname
`localhost`. Pull constructs the destination ID from bucket hostname, not from the
source database directory.

Rugged was processed first and created
`aw-watcher-web-chrome_localhost-synced-from-localhost` with 16,389 events. Wyrm2 then
targeted the same destination, resumed from rugged's newest timestamp, and reported
already up to date. None of wyrm2's 2,609 browser rows were imported into a distinct
device bucket.

## Finding 3: desktop staging databases are already amplified

The Syncthing files are aw-sync staging databases produced by repeated desktop
`--mode push` runs. They are not byte-for-byte snapshots of each desktop's authoritative
aw-server SQLite database.

Counts below were queried from the frozen pre-canary files. `distinct exact` is the
number of distinct `(starttime,endtime,data)` tuples.

| Device | Bucket  |   Rows | Distinct starts | Distinct exact |
| ------ | ------- | -----: | --------------: | -------------: |
| rugged | AFK     |    261 |             151 |            166 |
| rugged | browser | 16,389 |          14,412 |         15,551 |
| rugged | window  | 40,005 |          40,005 |         40,005 |
| wyrm2  | AFK     |    119 |             110 |            116 |
| wyrm2  | browser |  2,609 |           2,228 |          2,328 |
| wyrm2  | tmux    |  5,953 |           5,952 |          5,953 |
| wyrm2  | window  | 29,077 |          29,077 |         29,077 |
| iguana | AFK     |      9 |               9 |              9 |
| iguana | tmux    |     10 |              10 |             10 |
| iguana | window  |    107 |             107 |            107 |
| atlas  | AFK     | 84,814 |             229 |          4,738 |
| atlas  | tmux    |     78 |              78 |             78 |

Atlas AFK alone contains 80,076 rows beyond its 4,738 distinct exact tuples. This is the
concrete amplified input behind the earlier unexpectedly rapid database growth; importing
it also provides a direct explanation for the server's unexpected memory pressure.

The canary wrote 18,398 Atlas AFK events to the central server, not 84,814. That reduction
does not make the result canonical; it remains greater than the source's distinct exact
tuples. The exact reduction mechanism has not been traced, and aw-sync exposes no explicit
deduplication contract that would make 18,398 the expected result.

## Central result

The central server contained these counts after the canary:

| Device/bucket                          | Events |
| -------------------------------------- | -----: |
| rugged window                          | 40,005 |
| rugged AFK                             |    261 |
| rugged tmux                            |      0 |
| shared `localhost` browser destination | 16,389 |
| wyrm2 window                           | 29,077 |
| wyrm2 AFK                              |    119 |
| wyrm2 tmux                             |  5,953 |
| iguana window                          |    107 |
| iguana AFK                             |      9 |
| iguana tmux                            |     10 |
| atlas window                           |      0 |
| atlas AFK                              | 18,398 |
| atlas tmux                             |     78 |

## Restoration and subsequent conflicts

Before restarting Syncthing, the generated cluster database was deleted and all four
canonical paths were restored from the frozen pre-canary files. Their hashes matched the
pre-canary table exactly.

After Syncthing restarted, rugged and wyrm2 supplied newer canonical versions while the
receiver had locally restored older files. Syncthing preserved the restored versions as:

| Conflict                                                                             | SHA-256                                                            |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------------------ |
| `15c10090-44ad-4ed6-963f-9b66e35d2055/test.sync-conflict-20260721-035404-PATWINW.db` | `3cc9658fa89587557c6284aad138b667429cecb64d95f6dde3526e4ed348e567` |
| `a1627407-d5d1-4481-be32-b9a9906d1b5f/test.sync-conflict-20260721-035430-QKSEM74.db` | `3ef2fda85a33467ba6a4a36f3e28d8afa09034d02c2175f04b9d42de2c6c313a` |

Those hashes exactly match the frozen pre-canary sources. These two conflicts are therefore
an expected consequence of the manual receiver-side restoration, not evidence of another
Syncthing index reset. They still block the importer by design and should be deleted only
after this evidence is retained.

## Requirements for the next design

Do not enable scheduled ingestion until a replacement passes all of these gates:

1. Export from each desktop is idempotent across repeated runs and does not amplify
   heartbeat updates.
2. Each source has a stable repo-managed identity; `localhost` bucket metadata cannot
   merge histories from different devices.
3. Cluster ingestion opens source snapshots read-only and creates no files beside them.
4. Source hashes remain unchanged across a frozen import.
5. A second import of the same frozen inputs adds zero events.
6. The central counts and representative event ranges match a documented canonicalization
   rule.
7. Conflict files make the importer fail closed.

Two implementation directions remain plausible; neither is selected by this note:

- repair/fork aw-sync to provide the missing read-only, source-origin, and idempotency
  semantics;
- replace aw-sync transport with consistent snapshots of each real desktop SQLite DB plus
  a small repo-owned importer.
