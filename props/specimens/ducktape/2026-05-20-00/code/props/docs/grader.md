# Snapshot Graders (Reconciliation Loop)

Persistent graders that continuously reconcile grading state with ground truth.

## Concept: Reconciliation Loop

Snapshot graders follow the **Kubernetes controller pattern** — a reconciliation loop that drives actual state toward desired state:

```
Desired State: grading_pending is empty (all critique/GT pairs have edges)
Actual State:  grading_pending shows missing edges
Action:        Grade missing pairs until no drift remains
```

This is fundamentally different from one-shot graders:

| One-Shot Grader                | Snapshot Grader                |
| ------------------------------ | ------------------------------ |
| Runs once per critique         | Runs continuously per snapshot |
| Context loaded fresh each time | Context reused (cache hits)    |
| No awareness of GT changes     | Reconciles on GT changes       |
| Manual re-run needed           | Self-healing                   |

## Ground Truth Versioning (Transparent)

The reconciliation loop **transparently handles ground truth changes**:

1. **GT addition**: New TP/FP occurrences appear in `grading_pending` → grader grades them
2. **GT removal**: CASCADE DELETE removes stale edges → no stale data
3. **GT modification**: Old edges deleted, new pairs appear in pending → grader re-grades

No explicit versioning needed. The `grading_edges` table IS the checkpoint — graders can restart at any time and resume from `grading_pending`.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   GraderSupervisor (host)                   │
├─────────────────────────────────────────────────────────────┤
│  Manages one container per snapshot:                        │
│  - spawn_existing(): start graders for all snapshots        │
│  - pg_notify listener: spawn on snapshot_created            │
│  - pg_notify listener: restart all on grader_definition_changed │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   Grader Container                          │
├─────────────────────────────────────────────────────────────┤
│  main.py:                                                   │
│    1. Fetch snapshot to /workspace                          │
│    2. Start pg_notify listener (grading_pending changes)    │
│    3. Wait for initial drift (avoid wasting LLM calls)      │
│    4. Run agent loop:                                       │
│       - Grade pending edges                                 │
│       - Cluster unmatched issues                            │
│       - Call sleep() when done (awaits notifications)       │
│    5. Only exits on fatal error (report_failure)            │
└─────────────────────────────────────────────────────────────┘
```

## Drift Detection

The `grading_pending` view is the source of truth for drift:

```sql
-- Shows (issue, occurrence) pairs without grading edges
SELECT critique_issue_id, tp_id, tp_occurrence_id, fp_id, fp_occurrence_id
FROM grading_pending
WHERE snapshot_slug = :snapshot
```

The grader's `sleep` tool checks this before sleeping:

- **Drift exists** → refuse to sleep, continue grading
- **No drift** → await `wake_event` until notification

## Notifications

PostgreSQL `pg_notify` triggers fire on GT changes:

- `INSERT/DELETE` on `true_positives`, `true_positive_occurrences`
- `INSERT/DELETE` on `false_positives`, `false_positive_occurrences`

No trigger needed for critique completion — `grading_pending` shows missing edges as soon as critic writes issues.

## Context Management

When a grader's context grows large:

1. Agent continues operating (sleep tool maintains context across wake cycles)
2. If context limit approached, transcript compaction can summarize old results
3. On fatal error, `GraderSupervisor` can restart with fresh context

## Configuration

One grader per snapshot. Config stored in `agent_runs.type_config`:

```python
@dataclass
class SnapshotGraderTypeConfig:
    agent_type: Literal["snapshot_grader"] = "snapshot_grader"
    snapshot_slug: str
```

## Token Economics

1 grader per snapshot saves ~75% tokens through GT caching:

- 14K stable prefix (ground truth) reused
- 93% cache hit rate after GT loaded

## Key Files

| File                                    | Purpose                                    |
| --------------------------------------- | ------------------------------------------ |
| `agents/grader/main.py`                 | Container entry point, agent loop, tools   |
| `agents/grader/drift_handler.py`        | Check `grading_pending` for work           |
| `agents/grader/notification_handler.py` | Inject pg_notify messages into agent       |
| `orchestration/grader_supervisor.py`    | `GraderSupervisor` — container lifecycle   |
| `backend/app.py`                        | Lifespan auto-start via `GraderSupervisor` |
