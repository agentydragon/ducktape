# Budget Enforcement Plan

## Overview

Move budget tracking from `PromptOptimizerTypeConfig.budget_limit` to a proper column on `agent_runs`, with enforcement at the LLM proxy level.

## Current State

- `budget_limit` exists only in `PromptOptimizerTypeConfig`
- `budget_usd` column already exists on `agent_runs` (nullable, not populated)
- LLM proxy logs requests to `llm_requests` table
- `model_metadata` table has cost info per model

## Design

### Schema Changes

**`agent_runs` table** (column already exists, just needs population):

```sql
budget_usd: FLOAT NULL  -- Max USD cost for this agent tree. NULL = unlimited (grader only)
```

**Remove** `budget_limit` from `PromptOptimizerTypeConfig` after migration.

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
    SELECT
        lr.agent_run_id,
        COALESCE(SUM(
            COALESCE(lr.input_tokens, 0) * mm.input_cost_per_token +
            COALESCE(lr.output_tokens, 0) * mm.output_cost_per_token
        ), 0) AS own_cost_usd
    FROM llm_requests lr
    LEFT JOIN model_metadata mm ON lr.model = mm.model_id
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
- `budget_usd` - Budget limit (NULL = unlimited)
- `own_consumed_usd` - Cost of this agent's own LLM requests
- `tree_consumed_usd` - Total cost of this agent + all descendants
- `remaining_usd` - Budget remaining (NULL if unlimited)

### Proxy Enforcement

The LLM proxy (`/api/llm/v1/responses`) checks budget before forwarding:

```python
async def check_budget(agent_run_id: UUID, db: Database) -> None:
    """Raise 402 Payment Required if agent is out of budget."""
    with db.session() as session:
        result = session.execute(
            text("""
                SELECT budget_usd, tree_consumed_usd
                FROM agent_budget_status
                WHERE agent_run_id = :id
            """),
            {"id": agent_run_id}
        ).fetchone()

        if result is None:
            raise HTTPException(404, "Agent run not found")

        budget, consumed = result
        if budget is not None and consumed >= budget:
            raise HTTPException(
                402,
                f"Budget exhausted: {consumed:.4f} USD consumed of {budget:.4f} USD limit"
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
    child_budget_usd: float | None,
    db: Database
) -> None:
    """Validate child agent can be spawned with requested budget."""
    if child_budget_usd is None:
        return  # Explicitly unlimited (only valid for graders)

    with db.session() as session:
        parent = session.execute(
            text("SELECT remaining_usd FROM agent_budget_status WHERE agent_run_id = :id"),
            {"id": parent_run_id}
        ).fetchone()

        if parent is None:
            raise ValueError("Parent agent not found")

        remaining = parent.remaining_usd
        if remaining is not None and child_budget_usd > remaining:
            raise ValueError(
                f"Cannot spawn child with budget {child_budget_usd:.4f} USD - "
                f"parent only has {remaining:.4f} USD remaining"
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
    budget_usd: float | None  # NEW: Required parameter, None only for graders
```

**HTTP endpoints that launch agents**:

- `POST /api/eval/run-critic` - Add required `budget_usd` parameter
- `POST /api/eval/run-grader` - Budget is None (graders are unlimited)
- Any critic-dev spawn endpoints - Add required `budget_usd` parameter

### No Default Budget

Budget must always be explicitly specified. The validation layer rejects requests without it (except for graders which are explicitly unlimited).

```python
def validate_budget_for_agent_type(
    agent_type: AgentType,
    budget_usd: float | None
) -> None:
    """Ensure budget is appropriate for agent type."""
    if agent_type == AgentType.GRADER:
        if budget_usd is not None:
            raise ValueError("Graders must have unlimited budget (budget_usd=None)")
    else:
        if budget_usd is None:
            raise ValueError(f"{agent_type} agents require explicit budget_usd")
```

### Out of Budget Behavior

When an agent exceeds its budget:

1. Next LLM request returns HTTP 402
2. Agent SDK should propagate this as an exception
3. Agent likely crashes/fails (no special handling required)
4. Run status becomes whatever the container exit indicates (REPORTED_FAILURE or TIMED_OUT)

No new `AgentRunStatus.BUDGET_EXCEEDED` - the natural failure mode is sufficient.

### Migration Steps

1. **Add view** `agent_budget_status`
2. **Update AgentConfig** to require `budget_usd` parameter
3. **Update agent_registry** to populate `budget_usd` column on launch
4. **Update LLM proxy** to check budget before forwarding
5. **Update eval endpoints** to require budget parameter
6. **Update spawn validation** to check parent's remaining budget
7. **Remove** `budget_limit` from `PromptOptimizerTypeConfig`
8. **Backfill** existing prompt optimizer runs (copy from type_config)

### Files to Modify

| File                                    | Changes                                                |
| --------------------------------------- | ------------------------------------------------------ |
| `props/db/models.py`                    | Remove `budget_limit` from `PromptOptimizerTypeConfig` |
| `props/core/agent_types.py`             | Remove `budget_limit` field                            |
| `props/orchestration/agent_registry.py` | Populate `budget_usd` on launch, validate spawn budget |
| `props/backend/routes/llm.py`           | Add budget check before proxy                          |
| `props/backend/routes/eval.py`          | Add `budget_usd` parameter to endpoints                |
| `props/db/migrations/versions/...`      | Add view, backfill data                                |

### Agent Documentation Updates

Agent-facing documentation (`.md.j2` templates) needs updates:

| File                                       | Changes                                         |
| ------------------------------------------ | ----------------------------------------------- |
| `props/docs/agents/critic.md.j2`           | Document budget behavior, 402 response handling |
| `props/docs/agents/prompt_optimizer.md.j2` | Document budget management for spawned critics  |
| Any other agent prompts                    | Budget awareness guidance                       |

**Documentation should cover:**

- Budget is enforced at LLM proxy level
- HTTP 402 response when budget exhausted
- Agent should handle 402 gracefully (e.g., submit partial work before dying)
- How to check remaining budget (if API exposed)
- Child agent budget constraints (cannot exceed parent's remaining)

### Grant Permissions

The `agent_budget_status` view needs SELECT granted to `agent_base` so agents can check their own budget status.

```sql
GRANT SELECT ON agent_budget_status TO agent_base;
```

RLS on the view should filter to only show the agent's own tree (via `current_agent_run_id()`).

## Open Questions

1. **Should agents be able to query their remaining budget?** If yes, need an MCP tool or API endpoint. The view exists, just needs exposure.

2. **Pre-flight estimate**: Should proxy estimate cost before making the call and reject if estimate would exceed budget? Or just check after each call? (Current design: check consumed budget before each call, not estimate)

3. **Concurrent requests**: If agent makes parallel LLM calls, budget check happens per-request. Could slightly overspend if multiple requests are in flight when budget is nearly exhausted. Is this acceptable? (Likely yes - small overage is fine)

4. **Budget for prompt optimizer's child critics**: When PO spawns critics, how is their budget determined? Options:
   - PO specifies budget per critic explicitly
   - Fixed fraction of PO's remaining budget
   - Configurable in PO's type_config (new field: `per_critic_budget`)

5. **Model metadata completeness**: What if `model_metadata` is missing for a model? Current design returns cost=0 for unknown models. Should we:
   - Reject requests for unknown models?
   - Use a default high cost to be safe?
   - Log warning but allow?

6. **Grader budget justification**: Why are graders unlimited? They're daemons that may run indefinitely. Should they have per-snapshot or per-session budgets instead?
