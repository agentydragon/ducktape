# Backend API Access

The props backend provides HTTP REST APIs for agent orchestration. Critic-dev agents can call these endpoints directly.

## Connection

Use the `PROPS_BACKEND_URL` environment variable (defaults to `http://props-backend:8000`) and your postgres credentials for authentication:

```python
import os
import httpx

backend_url = os.environ.get("PROPS_BACKEND_URL", "http://props-backend:8000")
auth = (os.environ["PGUSER"], os.environ["PGPASSWORD"])
```

## Using CriticRunClient (Recommended)

For running critic evaluations, use the `CriticRunClient` class for the REST API calls and the `wait_until_graded()` function for polling grading status directly from the database:

```python
from props.agents.critic_dev.eval_client import CriticRunClient
from props.agents.critic_dev.grading import wait_until_graded
from props.core.models.examples import WholeSnapshotExample

async with CriticRunClient.from_env() as client:
    # Run critic (calls REST API)
    result = await client.run_critic(
        definition_id="critic",
        example=WholeSnapshotExample(snapshot_slug="ducktape/2025-01-01"),
        timeout_seconds=3600,
        budget_usd=5.0,
        critic_model="gpt-5.1-codex-mini",
    )

# Wait for grading completion (polls database directly, not via API)
status = await wait_until_graded(result.critic_run_id)
print(f"Recall: {status.total_credit}/{status.max_credit}")
```

**Note:** `wait_until_graded()` validates that:

- The critic run is finished (COMPLETED, FAILED, or TIMED_OUT)
- The critic run was started by the current agent

## OpenAPI Schema

The full API schema is available at `/openapi.json`. Use this for detailed request/response formats:

```python
# Fetch schema
schema = httpx.get(f"{backend_url}/openapi.json").json()
```

## Available Endpoints

| Endpoint           | Method | Description               |
| ------------------ | ------ | ------------------------- |
| `/api/runs/critic` | POST   | Run critic on an example  |
| `/v1/responses`    | POST   | LLM proxy (OpenAI format) |
| `/v2/*`            | \*     | OCI registry proxy        |

## Access Control

| Agent Type                         | Runs API | Registry | LLM Proxy |
| ---------------------------------- | -------- | -------- | --------- |
| Admin (localhost or postgres user) | ✓        | ✓        | ✓         |
| Critic-dev (optimizer)             | ✓        | ✓        | ✓         |
| Critic-dev (improver)              | ✓        | ✓        | ✓         |
| Critic                             | ✗        | ✗        | ✓         |
| Grader                             | ✗        | ✗        | ✓         |

Grading status is polled directly from the database by `wait_until_graded()` inside containers.
