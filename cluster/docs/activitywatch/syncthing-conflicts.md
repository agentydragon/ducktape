# ActivityWatch Syncthing index-loss conflict evidence

[ActivityWatch project overview](README.md).

Captured before the one-time conflict cleanup on 2026-07-21 UTC.

## Finding

The receiver stored its Syncthing index under `/var/syncthing/data` on an `emptyDir`,
while `/sync-inbox` survived on `activitywatch-sync-inbox`. The receiver restarted at
`00:33:16Z` with a fresh index. At `00:33:35Z`, both connected peers reported mismatching
index IDs. Syncthing replaced their canonical files at `00:33:37Z` and `00:33:41Z`; the
two conflict filenames carry those same timestamps.

This strongly identifies loss of the receiver index across restart as the conflict source,
not simultaneous desktop writers. The fix persists `/var/syncthing/data` on
`activitywatch-syncthing-state`. The two files below were deleted after the new index
converged.

Later conflicts are not automatically evidence that the index was lost again. The importer
canary deliberately restored older files on the receive-only receiver, and Syncthing
correctly preserved those local versions as conflicts when newer peer versions arrived.
See [the importer canary record](importer-canary.md). Every conflict still
stops the importer and requires attribution before deletion.

## Files preserved in the diagnostic snapshot

| Device                                          | Canonical events | Conflict events | Conflict SHA-256                                                   |
| ----------------------------------------------- | ---------------: | --------------: | ------------------------------------------------------------------ |
| rugged (`15c10090-44ad-4ed6-963f-9b66e35d2055`) |           55,639 |          54,167 | `896f372d4de8dfac63f3e19f86e62e5773ed39bd9cb1b89afccd7c11e58b0d12` |
| wyrm2 (`a1627407-d5d1-4481-be32-b9a9906d1b5f`)  |           37,640 |          37,447 | `be4abbbb5076e2830f4e66dbe14c63037e784ed2ee3b1908ef52b07f5e3ba6a6` |

All six canonical/conflict SQLite files returned `PRAGMA quick_check = ok`. Wyrm2's
conflict events are an exact subset of its canonical database. Rugged differs by one AFK
heartbeat whose canonical row has the same start and data but a later end time; all other
conflict events are present canonically. The copies are therefore older snapshots, not
independent histories.
