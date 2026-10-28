# Dispatcher quiesce and live journal readers

## Retained failure

Notifications decoder PR #12683 failed `//agentplane/system_tests:test_dispatcher` at merge
commit `b07092023743efc9aa255048532e62c23dae6b03` in
[BuildBuddy 810f4205](https://app.buildbuddy.io/invocation/810f4205-c993-5ce2-8e9b-24cc231afe78).
The wake/admission and archive assertions passed. Cleanup then timed out after 300 seconds waiting
for `Ready.reason=Stopped`. The Sandbox remained `Quiescing`, with a running Pod and an unsealed
Runner lease reporting `PendingCommand`. The lease's `draining` value covers aggregate stages before
`Flushing`; it does not identify the exact blocked controller phase.

The successful archive around 15:12:21–22 UTC contains one admitted command at cursor 1,
`dispatch_planned=0`, no terminal cursor, and no turn, native request or `HarnessStarted` event.
Its checkpoint is cursor 1, `harness_running=0`, with no active turn. Codex had initialized local
databases, but there is no retained native thread or reply evidence. This differs from the older
[missing-Codex-binary cleanup failure](../../system_tests/k3s/debug/dispatcher_cleanup_2026-10-27.md).

Only the last 200 Runner log lines survived, from 15:17:21–23 UTC, repeatedly reporting
`journal failed; reopen it for recovery: database is locked (5) (SQLITE_BUSY)`. The cleanup-time online backup timed out
after 120 seconds with an empty partial destination. The first SQLite error and exact COSI state
were not retained, so the original lock collision cannot be attributed conclusively.

## Reproduced mechanism

The live journal uses SQLite `DELETE` mode. A backup reader holds a shared lock; committing a write
requires an exclusive lock. Previously the writer had no busy wait. An external reader therefore
made `COMMIT` fail immediately. Both direct writes and `CommitBatch` marked the journal failed
without rolling back the transaction, retaining locks that prevented subsequent readers even after
the original reader finished. This accounts for a possible permanent journal failure and a later
backup hanging; the successful archive before quiesce is a possible source of the first collision.

`TestFailedCommitReleasesSQLiteLocks` holds a real independent SQLite read transaction across each
commit path. Both cases failed on the original implementation with retained `SQLITE_BUSY` locks:
[baseline e0618b70](https://app.buildbuddy.io/invocation/e0618b70-2362-48ce-b1b7-f53b6631489f).

The fix adds a five-second SQLite busy wait for transient readers and rolls back failed commits
through the existing failure helper. The writer remains failed and its published prefix stays
unchanged after an error. The transient-reader test observes SQLite's pending write lock, releases
the reader, then verifies the writer commits and can seal. Fault tests force immediate contention
and verify lock release, rollback, and continued refusal of writes. All passed ten race-enabled runs:
[86755914](https://app.buildbuddy.io/invocation/86755914-9631-4aa1-9303-3b57ae613da7).

Dispatcher diagnostics now retain ten minutes of timestamped Runner/controller logs rather than
only the last 200 lines, so a recurrence can preserve the first failure. A passing deployed system
test validates the changed path but cannot retroactively prove the first cause in the failed run.
