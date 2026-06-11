# Persistent Grader + Ground Truth CRUD Plan

## Overview

Replace ephemeral one-shot graders with persistent "grader daemons" (one per snapshot) that wake when drift is detected and reconcile grading decisions. Move ground truth source of truth from git YAML to PostgreSQL with REST API.

## Token Economics (Summary)

1 grader per snapshot saves ~75% tokens through GT caching (14K stable prefix reused at 93% cache hit rate after GT loaded).

---

## Completed

### Phase A: Infrastructure ✅

- Backup infrastructure (devenv `pg_backup` + `bazelisk run //props/cli -- db backup/restore/list-backups`)
- Clustering removal (migration 20251227000003)

### Phase B: Schema + YAML Extension ✅

- `graders_match_only_if_reported_on` column on TP/FP occurrences
- YAML schema, sync code, domain models all updated
- Migration 20251228000000 (squashed schema)

### Phase C.1-9: GT Read + Export ✅

- GT Read API: `GET /api/gt/snapshots`, `GET /api/gt/snapshots/{slug}`
- GT Browser Frontend: `SnapshotsList.svelte`, `SnapshotDetail.svelte`
- DB → YAML export: `bazelisk run //props/cli -- gt export`

### Phase D: Unified Grading Model ✅

Migration 20251228000001 completed:

- `grading_edges` table (bipartite graph model)
- Dropped `grading_decisions` table
- All recall views migrated to use `grading_edges`
- `grading_pending` view for drift detection
- `matchable_occurrences()` function for sparse matching

Grader CLI and docs completed:

- `bazelisk run //props/cli -- grader-agent list pending` — Query missing edges
- `bazelisk run //props/cli -- grader-agent show issue/gt` — Inspect issues/occurrences
- `bazelisk run //props/cli -- grader-agent match` — Create edges with credit
- `bazelisk run //props/cli -- grader-agent fill` — Bulk-fill remaining edges
- `bazelisk run //props/cli -- grader-agent delete` — Delete edges for redo
- `bazelisk run //props/cli -- grader-agent submit` — Finalize grading
- `docs/agents/grader.md.j2` — Updated with edges model
- `docs/db/grading.md.j2` — Updated with edges documentation

### Phase F: Snapshot Grader RLS ✅

- `current_grader_snapshot_slug()`, `is_critique_on_grader_snapshot()` helpers
- RLS policies for grader, snapshot_grader, prompt_optimizer, improvement
- pg_notify triggers for GT changes (INSERT/DELETE on TP/FP tables)

### Phase E: Documentation Updates ✅

- Bipartite graph model documented in grader.md.j2, grading.md.j2, evaluation_flow.md.j2
- Sparse matching and matchability rules clarified
- All grading_decisions references updated to grading_edges

### Phase G: Grader Daemon Implementation ✅

- `SnapshotGraderTypeConfig` in `agent_types.py`
- `GraderDriftHandler` in `grader/drift_handler.py`
- `GraderDaemonScaffold` in `grader/daemon.py`
- `SnapshotGraderAgentEnvironment` in `grader/snapshot_grader_env.py`
- `run_snapshot_grader()` in `agent_registry.py`
- `snapshot_grader_config()` method on `AgentRun`
- Daemon-mode prompt template `docs/agents/grader_daemon_mode.md.j2`
- Conditional template rendering in `grader.md.j2` (is_daemon flag)
- Updated `grader-agent init` to detect mode and pass data to template
- Unified CLI: both modes use `bazelisk run //props/cli -- grader-agent` (deleted separate `grader-daemon` CLI)
- G.10: `--run` option for snapshot mode (instead of qualified IDs)
  - `list pending` shows rich table with Run column in snapshot mode
  - All commands accept `--run` to filter by critic run
  - Short UUID prefix resolution from pending edges
- G.11: Backend auto-start via `DaemonManager` in `grader/daemon_manager.py`
  - Lifespan starts daemons for all snapshots if `PROPS_GRADER_MODEL` is set
  - Daemons sleep immediately if no drift
- G.12: Context exhaustion restart in `DaemonManager._run_daemon_with_restart()`
  - Catches `CONTEXT_LENGTH_EXCEEDED` status and spawns fresh daemon
  - Max 10 restarts per snapshot as safety limit
  - Agent docs updated to expect partial state
- `GradingPending` ORM model added to `db/models.py` for the view

**Future TODOs (left in code):**

- Resume existing IN_PROGRESS runs with transcript from DB on startup
- Handle new snapshots added after startup (pg_notify on snapshot insert)

---

## Pending

### Phase C.10: GT Write API

POST/PUT/DELETE endpoints for TPs/FPs (enables web-based GT editing)

### Phase G Details

#### G.1: Core Concept

The grader daemon is a **k8s controller-style reconciliation loop**:

- Goal: make `grading_pending` empty for its snapshot
- Drift = missing edges (critique_issue, matchable_occurrence pairs without grading_edges)
- When drift exists → grade; when empty → sleep until woken

#### G.2: Scope Decision

**1 grader daemon per snapshot.** Rationale:

- Avoids info mixing across repos/snapshots
- Batches work efficiently (all critiques for a snapshot share GT context)
- RLS already supports this (`snapshot_grader` agent type in migration 20251228000001)

#### G.3: Agent Loop Pattern

```
┌─────────────────────────────────────────────────────────────┐
│                   GraderDaemonScaffold                      │
├─────────────────────────────────────────────────────────────┤
│  _listen_loop():           # Background task                │
│    async for notif in pg_listen('grading_pending'):         │
│      if notif.snapshot_slug == my_snapshot:                 │
│        queue.append(notif)                                  │
│        wake_event.set()                                     │
│                                                             │
│  run():                                                     │
│    agent = Agent.create(handlers=[DriftHandler, ...])       │
│    agent.process_message(SystemMessage(grader_prompt))      │
│                                                             │
│    while True:                                              │
│      await agent.run()     # Until Abort() (no drift)       │
│      wake_event.clear()                                     │
│      await wake_event.wait()  # Sleep until notification    │
│      notifs = drain_queue()                                 │
│      agent.process_message(UserMessage(format(notifs)))     │
│      # Loop continues, run() called again                   │
└─────────────────────────────────────────────────────────────┘
```

#### G.4: Drift Handler

```python
class GraderDriftHandler(BaseHandler):
    def __init__(self, snapshot_slug: str, queue: list, wake_event: asyncio.Event):
        self._snapshot_slug = snapshot_slug
        self._queue = queue
        self._wake_event = wake_event

    def on_before_sample(self) -> LoopDecision:
        # Drain notifications that arrived while working
        notifs = list(self._queue)
        self._queue.clear()
        self._wake_event.clear()

        has_drift = check_grading_pending(self._snapshot_slug)

        if not has_drift:
            return Abort()  # No drift → exit run(), sleep in scaffold

        if notifs:
            # Inject context about what changed
            msg = format_notifications(notifs)
            return InjectItems(items=[UserMessage.text(msg)])

        return NoAction()  # Continue grading
```

Key behaviors:

- Checks `grading_pending` before each sample (source of truth)
- Drains notification queue to prevent buildup
- Injects notification content as context when new events arrive during work
- Returns `Abort()` when drift is empty → scaffold awaits next notification

#### G.5: Context Exhaustion

When agent hits context limit:

1. `ContextLengthExceededError` raised
2. Scaffold catches, marks run as `CONTEXT_LENGTH_EXCEEDED`
3. Spawns new agent run for same snapshot
4. New agent queries `grading_pending` for remaining work
5. GT reloaded into context (cache hit if recent)

The `grading_edges` table IS the checkpoint. No special state to save.

#### G.6: Notification Channels

Existing trigger (from Phase F):

```sql
CREATE FUNCTION notify_gt_changed() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('grading_pending', json_build_object(
        'event', TG_OP || '_' || TG_TABLE_NAME,
        'snapshot_slug', COALESCE(NEW.snapshot_slug, OLD.snapshot_slug)
    )::text);
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
```

Fires on:

- `INSERT/DELETE` on `true_positives`
- `INSERT/DELETE` on `true_positive_occurrences`
- `INSERT/DELETE` on `false_positives`
- `INSERT/DELETE` on `false_positive_occurrences`

**No trigger needed for critique completion** - `grading_pending` view shows missing edges as soon as critic writes issues (even before run completes). Grader can grade incrementally.

#### G.7: Configuration

```python
@dataclass
class SnapshotGraderConfig:
    agent_type: Literal["snapshot_grader"] = "snapshot_grader"
    snapshot_slug: str
```

Stored in `agent_runs.type_config`. RLS uses `current_grader_snapshot_slug()` to extract.

#### G.8: Files Created/Modified

| File                            | Purpose                                 |
| ------------------------------- | --------------------------------------- |
| `grader/daemon.py`              | `GraderDaemonScaffold` class            |
| `grader/daemon_manager.py`      | `DaemonManager` for lifecycle + restart |
| `grader/drift_handler.py`       | `GraderDriftHandler`                    |
| `grader/snapshot_grader_env.py` | `SnapshotGraderAgentEnvironment`        |
| `agent_registry.py`             | `run_snapshot_grader()` method          |
| `cli/cmd_grader_agent.py`       | Unified CLI with `--run` option         |
| `backend/app.py`                | Lifespan auto-start via `DaemonManager` |
| `db/models.py`                  | `GradingPending` ORM model for view     |

#### G.9: Integration with AgentRegistry

```python
class AgentRegistry:
    async def run_snapshot_grader(
        self,
        snapshot_slug: str,
        client: OpenAIModelProto,
    ) -> UUID:
        """Run a snapshot grader daemon. Blocks until daemon exits or context exhausted."""
        # Create agent run with SnapshotGraderConfig
        # Set up GraderDaemonScaffold
        # Run until context exhausted or manual stop
```

The daemon lives in the registry's `_active` dict while running.

---

## Critical Files

| File                                         | Purpose                                          | Status      |
| -------------------------------------------- | ------------------------------------------------ | ----------- |
| `cli/cmd_grader_agent.py`                    | Grader CLI commands                              | ✅ Complete |
| `grader/edge_helpers.py`                     | Grader writes to grading_edges (ORM)             | ✅ Complete |
| `db/models.py`                               | Added `GradingPending` ORM model for view        | ✅ Complete |
| `docs/agents/grader.md.j2`                   | Grader agent docs                                | ✅ Complete |
| `docs/agents/grader_daemon_mode.md.j2`       | Daemon-mode prompt template                      | ✅ Complete |
| `docs/db/grading.md.j2`                      | Grading schema docs                              | ✅ Complete |
| `db/migrations/versions/20251228000001_*.py` | Recall views + edges                             | ✅ Complete |
| `grader/daemon.py`                           | Daemon scaffold                                  | ✅ Complete |
| `grader/daemon_manager.py`                   | Daemon lifecycle + restart on context exhaustion | ✅ Complete |
| `grader/drift_handler.py`                    | Drift detection handler                          | ✅ Complete |
| `grader/snapshot_grader_env.py`              | Daemon agent environment                         | ✅ Complete |
| `agent_registry.py`                          | `run_snapshot_grader()` method                   | ✅ Complete |
| `backend/app.py`                             | Lifespan auto-start daemons                      | ✅ Complete |
