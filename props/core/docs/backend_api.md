# Backend API Access

The props backend provides HTTP REST APIs for agent orchestration. PO and PI agents can call these endpoints directly.

## Connection

Use the `PROPS_BACKEND_URL` environment variable (defaults to `http://props-backend:8000`) and your postgres credentials for authentication:

```python
import os
import httpx

backend_url = os.environ.get("PROPS_BACKEND_URL", "http://props-backend:8000")
auth = (os.environ["PGUSER"], os.environ["PGPASSWORD"])
```

## Using EvalClient (Recommended)

For running critic evaluations, use the `EvalClient` class for the REST API calls and the `wait_until_graded()` function for polling grading status directly from the database:

```python
from props.core.eval_client import EvalClient, wait_until_graded
from props.core.models.examples import WholeSnapshotExample

async with EvalClient.from_env() as client:
    # Run critic (calls REST API)
    result = await client.run_critic(
        definition_id="critic",
        example=WholeSnapshotExample(snapshot_slug="ducktape/2025-01-01"),
    )

# Wait for grading completion (polls database directly, not via API)
status = await wait_until_graded(result.critic_run_id)
print(f"Recall: {status.total_credit}/{status.max_credit}")
```

**Note:** `wait_until_graded()` validates that:

- The critic run is finished (COMPLETED, FAILED, or MAX_TURNS_EXCEEDED)
- The critic run was started by the current agent

## OpenAPI Schema

The full API schema is available at `/openapi.json`. Use this for detailed request/response formats:

```python
# Fetch schema
schema = httpx.get(f"{backend_url}/openapi.json").json()
```

## Available Endpoints

| Endpoint                            | Method | Description               |
| ----------------------------------- | ------ | ------------------------- |
| `/api/eval/run_critic`              | POST   | Run critic on an example  |
| `/api/eval/grading_status/{run_id}` | GET    | Check grading status      |
| `/v1/responses`                     | POST   | LLM proxy (OpenAI format) |
| `/v2/*`                             | \*     | OCI registry proxy        |

## Access Control

| Agent Type                         | Eval API | Registry | LLM Proxy |
| ---------------------------------- | -------- | -------- | --------- |
| Admin (localhost or postgres user) | ✓        | ✓        | ✓         |
| Prompt Optimizer (PO)              | ✓        | ✓        | ✓         |
| Prompt Improver (PI)               | ✓        | ✓        | ✓         |
| Critic                             | ✗        | ✗        | ✓         |
| Grader                             | ✗        | ✗        | ✓         |

## Raw HTTP Example

If you need to use raw HTTP requests instead of `EvalClient`:

```python
import os
import time
import httpx

backend_url = os.environ.get("PROPS_BACKEND_URL", "http://props-backend:8000")
auth = (os.environ["PGUSER"], os.environ["PGPASSWORD"])

# Run critic
response = httpx.post(
    f"{backend_url}/api/eval/run_critic",
    auth=auth,
    json={
        "definition_id": "critic",
        "example": {"kind": "whole_snapshot", "snapshot_slug": "ducktape/2025-01-01"},
        "critic_model": "gpt-5.1-codex-mini",
    },
    timeout=3600,
)
result = response.json()
critic_run_id = result["critic_run_id"]

# Poll for grading completion
while True:
    status = httpx.get(
        f"{backend_url}/api/eval/grading_status/{critic_run_id}",
        auth=auth,
    ).json()

    if status["is_complete"]:
        print(f"Recall: {status['total_credit']}/{status['max_credit']}")
        break

    time.sleep(5)
```
