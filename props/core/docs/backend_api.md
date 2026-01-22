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

## Available Endpoints

### Run Critic

```
POST /api/eval/run_critic
```

Run a critic agent on an example and get the run ID.

**Request:**

```json
{
  "definition_id": "critic",
  "example": { "kind": "whole_snapshot", "snapshot_slug": "ducktape/2025-01-01" },
  "timeout_seconds": 3600,
  "budget_usd": null,
  "critic_model": "gpt-5.1-codex-mini",
  "target_metric": "whole_repo"
}
```

**Response:**

```json
{
  "critic_run_id": "uuid-here",
  "status": "COMPLETED",
  "message": "Critic completed successfully. Use wait_until_graded to get results."
}
```

### Wait Until Graded

```
POST /api/eval/wait_until_graded
```

Wait for a critic run to be fully graded and get the results.

**Request:**

```json
{
  "critic_run_id": "uuid-here",
  "timeout_seconds": 300,
  "poll_interval_seconds": 5
}
```

**Response:**

```json
{
  "grader_run_id": "uuid-here",
  "total_credit": 3.5,
  "max_credit": 5,
  "message": "To get recall metrics, query the recall_by_definition_split_kind view..."
}
```

## Example: Running a Critic Evaluation

```python
import os
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
print(f"Critic run started: {critic_run_id}")

# Wait for grading
response = httpx.post(
    f"{backend_url}/api/eval/wait_until_graded",
    auth=auth,
    json={"critic_run_id": str(critic_run_id)},
    timeout=300,
)
grading = response.json()
print(f"Recall: {grading['total_credit']}/{grading['max_credit']}")
```

## Access Control

| Agent Type                         | Access      |
| ---------------------------------- | ----------- |
| Admin (localhost or postgres user) | Full access |
| Prompt Optimizer (PO)              | Full access |
| Prompt Improver (PI)               | Full access |
| Critic                             | No access   |
| Grader                             | No access   |

## OpenAPI Schema

The full API schema is available at `/openapi.json`. LLMs can use this to discover all available endpoints.
