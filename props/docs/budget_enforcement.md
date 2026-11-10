# Budget Enforcement

Every agent run has an explicit, non-null `budget_usd` limit; there is no unlimited
value. Critics and critic-dev agents receive their budgets at launch; snapshot
graders receive $10,000 ([registry](../orchestration/agent_registry.py)).

## Budget Checking

The [LLM proxy](../llm_proxy/routes.py) checks recorded spend before forwarding a
request. The [`agent_run_budget_status` view](../db/models.py) includes the run
and all descendants, using logged request usage and `model_metadata` prices.
At or above the budget, the proxy returns HTTP 429. The exception is zero recorded
tree spend with a zero-dollar budget, which passes the preflight check.

## Spawn Constraints

Child budgets are explicit, not inherited. The registry rejects a requested child
budget above the immediate parent's remaining budget, based on recorded subtree
spend. It does not reserve budgets for still-running children or check every
ancestor's budget; these remain known gaps in
[`_validate_spawn_budget`](../orchestration/agent_registry.py).

## Out of Budget Behavior

The agent SDK surfaces the 429 as an exception. An unhandled error can end the
container run; there is no special out-of-budget status. The registry records
`EXITED` and the container exit code, or `TIMED_OUT` when the run times out.

## Concurrent Overspend

Parallel in-flight requests can all pass the preflight check before their costs
are recorded. Overspend is therefore possible: the budget is a soft limit, not an
atomic spending cap.
