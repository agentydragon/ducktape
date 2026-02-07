# Budget Enforcement Plan

## Overview

Move budget tracking from `CriticDevOptimizeTypeConfig.budget_limit` to a proper column on `agent_runs`, with enforcement at the LLM proxy level.

## Current State

- `budget_usd` column on `agent_runs` is NOT NULL and populated for all agent types
- `RunCriticRequest` requires `budget_usd` with `gt=0` validation
- Grader daemons get $10k default budget
- LLM proxy logs requests to `llm_requests` table
- `model_metadata` table has cost info per model

## Design Decisions

- **Budget querying**: Agents query their budget/consumed via `agent_budget_status` view with RLS - no special API needed
- **Grader budget**: High limit (e.g., $10000) instead of truly unlimited
- **Unknown models**: Reject requests - enforce via FK that `agent_runs.model` references `model_metadata`
- **Critic-dev budget**: Critic-dev agents specify budget limit explicitly in their spawn tool call
- **Concurrent overspend**: Acceptable if in-flight requests cause slight overage (leave TODO in impl)
- **Pre-flight estimate**: Check consumed budget before each call, not estimate future cost
- **Model metadata RLS**: Grant agents SELECT on `model_metadata` so they can see cost info

## Design

### Schema Changes

**`agent_runs` table** (column already exists, just needs population):

```sql
budget_usd: FLOAT NOT NULL  -- Max USD cost for this agent tree. All agents have explicit budget.
```

**Add FK constraint** to ensure model is known:

```sql
ALTER TABLE agent_runs
ADD CONSTRAINT agent_runs_model_fk
FOREIGN KEY (model) REFERENCES model_metadata(model_id);
```

**Remove** `budget_limit` from `CriticDevOptimizeTypeConfig` after migration.

### New Database View: `agent_budget_status`

```sql
CREATE VIEW agent_budget_status AS
WITH RECURSIVE agent_tree AS (
    -- Base case: each agent
    SELECT agent_run_id, agent_run_id AS root_id
    FROM agent_runs

    UNION ALL

    -- Recursive: children belong to parent's tree
    SELECT ar.agent_run_id, at.root_id
    FROM agent_runs ar
    JOIN agent_tree at ON ar.parent_agent_run_id = at.agent_run_id
),
costs AS (
    -- Cost per agent (own LLM requests only)
    -- INNER JOIN is safe since agent_runs.model has FK to model_metadata
    SELECT
        lr.agent_run_id,
        COALESCE(SUM(
            COALESCE(lr.input_tokens, 0) * mm.input_cost_per_token +
            COALESCE(lr.output_tokens, 0) * mm.output_cost_per_token
        ), 0) AS own_cost_usd
    FROM llm_requests lr
    JOIN model_metadata mm ON lr.model = mm.model_id
    GROUP BY lr.agent_run_id
)
SELECT
    ar.agent_run_id,
    ar.budget_usd,
    COALESCE(c.own_cost_usd, 0) AS own_consumed_usd,
    COALESCE((
        SELECT SUM(c2.own_cost_usd)
        FROM agent_tree at2
        JOIN costs c2 ON c2.agent_run_id = at2.agent_run_id
        WHERE at2.root_id = ar.agent_run_id
    ), 0) AS tree_consumed_usd,
    ar.budget_usd - COALESCE((
        SELECT SUM(c2.own_cost_usd)
        FROM agent_tree at2
        JOIN costs c2 ON c2.agent_run_id = at2.agent_run_id
        WHERE at2.root_id = ar.agent_run_id
    ), 0) AS remaining_usd
FROM agent_runs ar
LEFT JOIN costs c ON c.agent_run_id = ar.agent_run_id;
```

Columns:

- `agent_run_id` - The agent run
- `budget_usd` - Budget limit (all agents have explicit budget)
- `own_consumed_usd` - Cost of this agent's own LLM requests
- `tree_consumed_usd` - Total cost of this agent + all descendants
- `remaining_usd` - Budget remaining

### ORM Model for View

```python
class AgentBudgetStatus(Base):
    """Read-only ORM model for agent_budget_status view."""

    __tablename__ = "agent_budget_status"
    __table_args__ = {"info": {"is_view": True}}

    agent_run_id: Mapped[UUID] = mapped_column(primary_key=True)
    budget_usd: Mapped[float]
    own_consumed_usd: Mapped[float]
    tree_consumed_usd: Mapped[float]
    remaining_usd: Mapped[float]
```

### Proxy Enforcement

The LLM proxy (`/api/llm/v1/responses`) checks budget before forwarding:

```python
async def check_budget(agent_run_id: UUID, db: Database) -> None:
    """Raise 402 Payment Required if agent is out of budget."""
    with db.session() as session:
        status = session.get(AgentBudgetStatus, agent_run_id)

        if status is None:
            raise HTTPException(404, "Agent run not found")

        if status.tree_consumed_usd >= status.budget_usd:
            raise HTTPException(
                402,
                f"Budget exhausted: {status.tree_consumed_usd:.4f} USD consumed "
                f"of {status.budget_usd:.4f} USD limit"
            )
```

Error response: HTTP 402 Payment Required with message explaining the situation.

### Spawn Constraints

When spawning a child agent, validate:

1. Parent's remaining budget >= child's requested budget
2. Child budget must be explicitly specified (no inheritance)

```python
def validate_child_budget(
    parent_run_id: UUID,
    child_budget_usd: float,
    db: Database
) -> None:
    """Validate child agent can be spawned with requested budget.

    Critic-dev agents specify budget explicitly when spawning critics via tool call.
    """
    if child_budget_usd <= 0:
        raise ValueError(f"child_budget_usd must be positive, got {child_budget_usd}")

    with db.session() as session:
        parent_status = session.get(AgentBudgetStatus, parent_run_id)

        if parent_status is None:
            raise ValueError("Parent agent not found")

        if child_budget_usd > parent_status.remaining_usd:
            raise ValueError(
                f"Cannot spawn child with budget {child_budget_usd:.4f} USD - "
                f"parent only has {parent_status.remaining_usd:.4f} USD remaining"
            )
```

### API Changes

**Agent launch** (all paths must specify budget):

```python
# AgentConfig changes
class AgentConfig(BaseModel):
    image_ref: str
    model: str
    parent_agent_run_id: UUID | None
    type_config: TypeConfig
    budget_usd: float  # NEW: Required parameter, all agents have explicit budget
```

**HTTP endpoints that launch agents**:

- `POST /api/runs/critic` - Requires `budget_usd` parameter (gt=0)
- Grader daemons - High budget ($10000) for long-running daemon
- Any critic-dev spawn endpoints - Requires `budget_usd` parameter

### No Default Budget

Budget must always be explicitly specified. The validation layer rejects requests without it.

```python
def validate_budget_for_agent_type(
    agent_type: AgentType,
    budget_usd: float
) -> None:
    """Ensure budget is specified for all agent types."""
    if budget_usd <= 0:
        raise ValueError(f"budget_usd must be positive, got {budget_usd}")
    # Graders get high limit (e.g., $10000), not unlimited
```

### Out of Budget Behavior

When an agent exceeds its budget:

1. Next LLM request returns HTTP 402
2. Agent SDK should propagate this as an exception
3. Agent likely crashes/fails (no special handling required)
4. Run status becomes whatever the container exit indicates (REPORTED_FAILURE or TIMED_OUT)

No new `AgentRunStatus.BUDGET_EXCEEDED` - the natural failure mode is sufficient.

### Migration Steps

1. **Add view** `agent_budget_status` — not started (budget checks use inline recursive CTE instead)
2. ~~**Update AgentConfig** to require `budget_usd` parameter~~ — done (`budget_usd` is on `AgentRun` directly)
3. ~~**Update agent_registry** to populate `budget_usd` column on launch~~ — done (all agent types populate it)
4. ~~**Update LLM proxy** to check budget before forwarding~~ — done (recursive CTE sums self + descendant costs, rejects with 429)
5. ~~**Update eval endpoints** to require budget parameter~~ — done (`RunCriticRequest.budget_usd` is required)
6. ~~**Update spawn validation** to check parent's remaining budget~~ — done (`_validate_spawn_budget` in agent_registry)
7. **Remove** `budget_limit` from `CriticDevOptimizeTypeConfig` — not started
8. ~~**Backfill** existing critic-dev optimizer runs~~ — N/A (budget_usd is NOT NULL, always populated)

### Files to Modify

| File                                    | Changes                                                  |
| --------------------------------------- | -------------------------------------------------------- |
| `props/db/models.py`                    | Remove `budget_limit` from `CriticDevOptimizeTypeConfig` |
| `props/core/agent_types.py`             | Remove `budget_limit` field                              |
| `props/orchestration/agent_registry.py` | Populate `budget_usd` on launch, validate spawn budget   |
| `props/backend/routes/llm.py`           | Add budget check before proxy                            |
| `props/backend/routes/runs.py`          | `budget_usd` parameter on critic run endpoints           |
| `props/db/migrations/versions/...`      | Add view, backfill data                                  |

### Agent Documentation Updates

Agent-facing documentation (`.md.j2` templates) needs updates:

| File                                          | Changes                                         |
| --------------------------------------------- | ----------------------------------------------- |
| `props/docs/agents/critic.md.j2`              | Document budget behavior, 402 response handling |
| `props/docs/agents/critic_dev_optimize.md.j2` | Document budget management for spawned critics  |
| Any other agent prompts                       | Budget awareness guidance                       |

**Documentation should cover:**

- Budget is enforced at LLM proxy level
- HTTP 402 response when budget exhausted
- Agent should handle 402 gracefully (e.g., submit partial work before dying)
- Query remaining budget via `agent_budget_status` view (RLS-protected)
- Query model costs via `model_metadata` table
- Child agent budget constraints (cannot exceed parent's remaining)

### Grant Permissions

The `agent_budget_status` view needs SELECT granted to `agent_base` so agents can check their own budget status.

```sql
GRANT SELECT ON agent_budget_status TO agent_base;
GRANT SELECT ON model_metadata TO agent_base;
```

RLS on the view filters to agent's own tree (via `current_agent_run_id()` and `is_agent_ancestor()`).

## Implementation TODOs

Leave in implementation code:

```python
# TODO: Concurrent in-flight requests may cause slight budget overspend.
# This is acceptable - budget check happens per-request before forwarding,
# but parallel requests could all pass the check before any complete.
```
