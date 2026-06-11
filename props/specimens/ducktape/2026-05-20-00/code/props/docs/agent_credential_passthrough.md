# Agent Credential Passthrough

Database connections using agent credentials for RLS enforcement.

## Overview

When an agent authenticates to the backend with their Postgres credentials (`agent_{uuid}`), the backend uses those credentials for the database connection. This leverages PostgreSQL RLS policies directly, removing the need to duplicate access control logic in Python.

## Connection Types

### AgentDb (Per-Request)

```python
from props.backend.auth import AgentDb, get_agent_db

@router.get("/runs")
async def list_runs(agent_db: AgentDb) -> list[Run]:
    # Connection uses agent's credentials, RLS filters results
    return agent_db.session.query(AgentRun).all()
```

- Uses `Database.per_request()` with NullPool
- Connection disposed after request
- RLS policies filter data automatically

### AdminDb (Pooled)

```python
from props.backend.deps import AdminDb, get_admin_db

@router.post("/runs/validation")
async def trigger_validation(admin_db: AdminDb) -> Job:
    # Admin connection for launching agents
    ...
```

- Uses connection pool
- Full database access
- Required for: launching agents, LLM proxy INSERT, registry push

## Endpoint Classification

**Using AgentDb (RLS-protected):**

| Endpoint                                | Notes                        |
| --------------------------------------- | ---------------------------- |
| `GET /api/definitions`                  | RLS filters by agent type    |
| `GET /api/runs`, `GET /api/runs/active` | RLS filters visible runs     |
| `GET /api/runs/{id}`                    | RLS enforces access          |
| `GET /api/runs/{id}/llm_requests`       | RLS filters visible requests |
| `GET /api/stats/*`                      | RLS filters visible data     |

**Using AdminDb:**

| Endpoint                     | Reason                   |
| ---------------------------- | ------------------------ |
| `POST /api/llm/v1/responses` | Proxy needs admin INSERT |
| `POST /api/eval/*`           | Launches agent runs      |
| `POST /api/registry/*`       | Definition push          |
| `GET /api/ground_truth/*`    | Admin dashboard          |
| `POST /api/runs/validation`  | Launches runs            |

## Admin Token Authentication

Backend uses Jupyter-style token authentication (no localhost exception):

1. Backend prints admin token URL on startup
2. Frontend captures token from URL → `localStorage`
3. Requests include `Authorization: Bearer {token}` header
4. WebSocket feed uses `?token=` query param

## Benefits

1. **Single source of truth**: RLS policies define access control
2. **Defense in depth**: RLS prevents unauthorized access even if Python ACL is wrong
3. **Audit trail**: Database logs show actual user performing operations
