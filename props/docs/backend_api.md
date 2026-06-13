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

For the deployed Kubernetes backend, wait for `/readyz` before starting runs. The
HTTP server binds before slow startup finishes; until readiness is `200`, the
run API may be reachable but orchestration state such as `app.state.registry`
is not initialized yet.

## Running a Critic

Critic-dev agents trigger a critic run by POSTing to `/api/runs/critic`. The
agent loop exposes this as the `start_critic` function-tool (see
`props/agents/critic_dev/loop.py`), which posts a `RunCriticRequest` and returns
a `StartCriticResponse` with `critic_run_id`. The tool returns immediately; the
critic runs asynchronously in its own container.

The request and response models live in `props/core/eval_api_models.py`
(`RunCriticRequest`, `StartCriticResponse`). The runs API takes an image digest
as `definition_id`, not the display name "critic": fetch
`/api/definitions?agent_type=critic` and select the desired digest, usually the
newest row with `display_name == "critic"`.

To call the endpoint directly:

```python
import httpx

from props.core.eval_api_models import RunCriticRequest, StartCriticResponse
from props.core.models.examples import WholeSnapshotExample

request = RunCriticRequest(
    definition_id="sha256:...",
    example=WholeSnapshotExample(snapshot_slug="ducktape/2025-01-01"),
    timeout_seconds=3600,
    budget_usd=5.0,
)
async with httpx.AsyncClient(base_url=backend_url, auth=auth) as client:
    resp = await client.post("/api/runs/critic", json=request.model_dump(mode="json"))
    resp.raise_for_status()
    started = StartCriticResponse.model_validate(resp.json())

# Wait for grading completion (polls the database directly, not via API).
status = await wait_until_graded(started.critic_run_id, db)
print(f"Recall: {status.total_credit}/{status.max_credit}")
```

Grading status is **not** a REST endpoint. `wait_until_graded()`
(`props/agents/critic_dev/grading.py`) polls the `grading_pending` view in the
database until grading is complete. Inside the agent loop, the
`wait_until_critic_completed` and `wait_until_graded` tools wrap the same
database polling.

## OpenAPI Schema

The full API schema is available at `/openapi.json`. Use this for detailed request/response formats:

```python
# Fetch schema
schema = httpx.get(f"{backend_url}/openapi.json").json()
```

## Available Endpoints

| Endpoint                      | Method | Description                                                        |
| ----------------------------- | ------ | ------------------------------------------------------------------ |
| `/api/definitions`            | GET    | List registered agent image digests; filter with `agent_type`      |
| `/api/runs/critic`            | POST   | Run critic on an example                                           |
| `/api/runs/{id}/llm_requests` | GET    | A run's LLM transcript (request/response rows); RLS-scoped         |
| `/api/runs/{id}/logs`         | GET    | A run's container logs (from Loki); RLS-scoped to your descendants |

The LLM proxy (`/v1/responses`, `/v1/chat/completions`, and `/v1/messages`) is a
**separate service** (`props-llm-proxy`), reached via `OPENAI_BASE_URL`, not this
backend. The registry proxy (`/v2/*`) is also a **separate service**, reached via
`PROPS_REGISTRY_URL`, not this backend.
Read a launched agent's
**transcript** from `llm_requests` (DB, also exposed above) and its **container
logs** via `/api/runs/{id}/logs` — agents cannot query Loki directly.

`model_metadata.api_shape` is the compatibility gate for the proxy route:
`responses` models must call `/v1/responses`, `chat_completions` models must call
`/v1/chat/completions`, and `anthropic` models must call `/v1/messages`.
Request and response bodies are stored as raw JSON in `llm_requests` because each
shape has provider-specific extensions. The frontend keeps a raw JSON fallback
even when it has a richer renderer for a shape.

## Access Control

| Agent Type                         | Runs API | Registry | LLM Proxy |
| ---------------------------------- | -------- | -------- | --------- |
| Admin (localhost or postgres user) | ✓        | ✓        | ✓         |
| Critic-dev (optimizer)             | ✓        | ✓        | ✓         |
| Critic-dev (improver)              | ✓        | ✓        | ✓         |
| Critic                             | ✗        | ✗        | ✓         |
| Grader                             | ✗        | ✗        | ✓         |

Grading status is polled directly from the database by `wait_until_graded()` inside containers.
