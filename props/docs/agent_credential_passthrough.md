# Agent Credential Passthrough

Database connections using agent credentials for RLS enforcement.

## Overview

When an agent authenticates to the backend with their Postgres credentials (`agent_{uuid}`), the backend uses those credentials for the database connection. This leverages PostgreSQL RLS policies directly, removing the need to duplicate access control logic in Python.

## Connection Types

### CallerDb (Per-Request)

```python
from props.backend.auth import CallerDb

@router.get("/runs")
async def list_runs(caller_db: CallerDb) -> list[Run]:
    # Connection uses agent's credentials, RLS filters results
    with caller_db.session() as session:
        return session.query(AgentRun).all()
```

- Uses `Database.per_request()` with NullPool
- Connection disposed after request
- RLS policies filter data automatically

### AdminDb (Shared)

```python
from props.backend.deps import AdminDb, get_admin_db

@router.post("/runs/validation")
async def trigger_validation(admin_db: AdminDb) -> Job:
    # Admin connection for launching agents
    ...
```

- Uses the backend's single admin `Database` (`app.state.admin_db`), also on NullPool
- Full database access
- Required for: launching agents

## Endpoint Classification

**Using CallerDb (RLS-protected):**

| Endpoint                                | Notes                        |
| --------------------------------------- | ---------------------------- |
| `GET /api/definitions`                  | RLS filters by agent type    |
| `GET /api/runs`, `GET /api/runs/active` | RLS filters visible runs     |
| `GET /api/runs/{id}`                    | RLS enforces access          |
| `GET /api/runs/{id}/llm_requests`       | RLS filters visible requests |
| `GET /api/stats/*`                      | RLS filters visible data     |
| `/api/gt/*`                             | RLS filters visible data     |

**Using AdminDb:**

| Endpoint                    | Reason        |
| --------------------------- | ------------- |
| `POST /api/runs/validation` | Launches runs |

## Admin Token Authentication

Backend uses Jupyter-style token authentication (no localhost exception):

1. With `dev_mode = true`, the backend logs the admin token URL on startup
2. Frontend captures token from URL → `localStorage`
3. Requests include `Authorization: Bearer {token}` header

## Benefits

1. **Single source of truth**: RLS policies define access control
2. **Defense in depth**: RLS prevents unauthorized access even if Python ACL is wrong
3. **Audit trail**: Database logs show actual user performing operations
