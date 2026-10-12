# ADR: settle streamed deltas out of stored history

- **Status:** Accepted (operator, 2026-10-10 PDT) and implemented; on in testing and staging, and stored history settled
- **Scope:** the History Service's raw log (`session_event`); the runner journal is unchanged
- **Mechanism:** [History Service § Delta settlement](../history_service/README.md#delta-settlement)

## Context

Streamed deltas were ~97% of the rows and 85–90% of the bytes of stored Session history (staging
sample, 2026-10-10), mostly Codex reasoning-summary deltas of a token or two, each kept as a native
frame plus its derived event. The completed item already holds the same content.

## Decision

When an item completes, the History Service drops its delta entries from `session_event` in the
same transaction, and records a `SettledDeltas` (template, completion cursor, cursor ranges, chunk
count, first and last chunk time) in their place. It settles only when all of these hold:

- **Clean span:** nothing anomalous between the item's start and completion (a malformed or
  duplicate-key frame, a failed command, harness stderr or exit, a reconciliation). Interrupted,
  failed and process-lost items never complete, so they never settle; recovery reads their deltas.
- **Exact templates:** every delta is a pair of a native frame matching a versioned template in
  `agentplane/native/delta_templates.py` exactly, as a parsed JSON value, and the derived event made
  from it. Templates are only added, never edited, so an old settlement is never reinterpreted.
- **Content is retained elsewhere:** the retained completion frame holds exactly the
  concatenation. A settlement points at runner-authored entries and never carries content of its
  own; an item whose completion does not reproduce its chunks (Codex command output without
  `aggregatedOutput`) keeps every chunk.

What is lost: chunk boundaries, per-chunk cursors, timestamps and ids, interleaving with other
events, and each frame's byte-level serialization. `session_event` stays runner-authored only;
its contract is "complete except the ranges a settlement names". The table keeps its name.

## Why in the History Service, not the runner

The bulk is in Postgres, and the runner is the exact recorder at the harness boundary, where a bug
needs every packet: while its Sandbox lives, the runner journal keeps every chunk, and the runner
protocol (cursors, replay, dense-prefix checks) needs no change. A replayed cursor that a
settlement covers is accepted as already stored.

## Alternatives not taken

- **Ephemeral storage for deltas.** The runner journal already is the full-fidelity copy for the
  Sandbox's life. An `UNLOGGED` side table for unsettled deltas would also cut WAL and backup
  volume; not built until that volume matters.
- **Carrying the concatenation in the settlement** so items without a reproducing completion could
  settle too: command output was ~3% of sampled bytes. Revisit only on a measurement.
- **Settlement as part of the Thread fold.** Settlement only proves that chunks add nothing to an
  existing record; presentation stays the fold's. The fold builds the same Thread with or without
  settlement.

## Consequences

- History stored before settlement was settled after the fact by a one-off Job, since deleted,
  with the operator's approval since it deletes stored history (2026-10-11, testing and
  staging; staging settled 45,785 items, 2.61 GB of payload). Deleting rows only frees space inside
  Postgres: returning it to the volume took a `VACUUM FULL` of `session_event`, `thread_evidence`
  and `thread_native_link` in a window (the CNPG image ships no `pg_repack`), which took staging's
  `session_event` from 4.6 GB to 835 MB.
- A `pg_dump` taken as the Job's rollback copy was OOM-killed on the staging primary, then limited
  to 512 MiB of memory. Backups before such a run come from the barman-cloud WAL archive and an
  on-demand CNPG `Backup` (`cluster/cdk8s/agentplane/db_backups.py`).
- A per-Session override and a matching runner-journal flag were considered and not built.
