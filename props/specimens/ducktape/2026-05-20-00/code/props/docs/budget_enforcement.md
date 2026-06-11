# Budget Enforcement

Budget tracking and enforcement for agent runs.

## Overview

Every agent run has an explicit `budget_usd` limit. The LLM proxy enforces this budget before forwarding requests, preventing agents from exceeding their allocation.

## Schema

**`agent_runs.budget_usd`** (FLOAT NOT NULL): Maximum USD cost for this agent's LLM requests. All agents have explicit budget - no defaults, no "unlimited".

**`model_metadata`** table: Cost per token for each model. Foreign key from `agent_runs.model` ensures all models have known costs.

**`llm_requests`** table: Logs all LLM requests with token counts for cost tracking.

## Budget Checking

The LLM proxy (`/api/llm/v1/responses`) checks budget before forwarding:

```python
# Compute consumed cost via recursive CTE (self + all descendants)
consumed = sum(input_tokens * input_cost + output_tokens * output_cost)
if consumed >= budget_usd:
    raise HTTPException(429, f"Budget exhausted: {consumed:.4f} of {budget_usd:.4f} USD")
```

The recursive CTE sums costs across the agent tree (parent + all spawned children).

## Spawn Constraints

When spawning a child agent:

1. Child budget must be explicitly specified (no inheritance)
2. Child budget cannot exceed parent's remaining budget

```python
def _validate_spawn_budget(parent_run_id: UUID, child_budget_usd: float) -> None:
    if child_budget_usd > parent_remaining:
        raise ValueError(f"Cannot spawn with {child_budget_usd} - parent has {parent_remaining} remaining")
```

## Agent Types

| Agent Type | Typical Budget                                      |
| ---------- | --------------------------------------------------- |
| Critic     | Specified per-run via `RunCriticRequest.budget_usd` |
| Grader     | $10,000 (high limit for long-running graders)       |
| Critic-dev | Specified when spawning                             |

## Out of Budget Behavior

When an agent exceeds its budget:

1. Next LLM request returns HTTP 429
2. Agent SDK propagates as exception
3. Agent fails naturally (no special status needed)
4. Run status reflects container exit (REPORTED_FAILURE or TIMED_OUT)

## Concurrent Overspend

Slight budget overspend is possible if parallel in-flight requests all pass the pre-flight check before any complete. This is acceptable - budget is a limit, not a hard cap.
