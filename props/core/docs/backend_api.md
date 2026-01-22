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
  "status": "COMPLETED"
}
```

### Get Grading Status

```
GET /api/eval/grading_status/{critic_run_id}
```

Check grading status for a critic run (non-blocking). Poll this endpoint until `is_complete` is true.

**Response (pending):**

```json
{
  "is_complete": false,
  "pending_count": 5,
  "grader_run_id": null,
  "total_credit": null,
  "max_credit": null,
  "split": null,
  "example_kind": null
}
```

**Response (complete):**

```json
{
  "is_complete": true,
  "pending_count": 0,
  "grader_run_id": "uuid-here",
  "total_credit": 3.5,
  "max_credit": 5,
  "split": "valid",
  "example_kind": "whole_snapshot"
}
```

## Example: Running a Critic Evaluation

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
print(f"Critic run completed: {critic_run_id}, status: {result['status']}")

# Poll for grading completion
while True:
    response = httpx.get(
        f"{backend_url}/api/eval/grading_status/{critic_run_id}",
        auth=auth,
        timeout=30,
    )
    status = response.json()

    if status["is_complete"]:
        print(f"Grading complete! Recall: {status['total_credit']}/{status['max_credit']}")
        print(f"Split: {status['split']}, Kind: {status['example_kind']}")
        break

    print(f"Grading in progress... {status['pending_count']} edges pending")
    time.sleep(5)  # Poll every 5 seconds
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
