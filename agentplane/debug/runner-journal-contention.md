# Runner journal contention

The dispatcher test in [invocation 53205169-05f8-5b2e-ac96-32ef3edf9d37](https://app.buildbuddy.io/invocation/53205169-05f8-5b2e-ac96-32ef3edf9d37)
completed its body, then timed out waiting for Sandbox `systest-wake-6978f45d` to stop.
The retained runner log reports a journal permanently failed with `SQLITE_BUSY`; the Sandbox remains
Quiescing. Cleanup's online journal backup also times out.

The successful data-proof archive has six committed Events and a matching checkpoint, plus an
8,720-byte rollback journal. The main database and rollback journal modification times are
15:29:34.977Z and 15:29:34.981Z on 2026-10-28. Its manifest reports a valid six-Event backup.
The test proves continued writes only to its separate WAL probe, not the runner's DELETE-mode journal.

An external backup reader can block a DELETE-mode writer's commit. SQLite documents that a busy
[COMMIT leaves its transaction active](https://www.sqlite.org/lang_transaction.html#implicit_versus_explicit_transactions).
Previously both journal commit paths fenced the writer without rollback or a busy wait, retaining
locks that could obstruct cleanup backup. Deterministic tests exercise this mechanism directly.

This supports a backup/commit contention diagnosis, but does not establish the unique first fault in
that invocation. The retained runner log starts at 15:34:34Z, after the journal was already failed;
it cannot identify the first failing operation or exclude another external reader.
