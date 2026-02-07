# Design: Agent Credential Passthrough for Backend Requests

## Summary

When an agent authenticates to the backend with their Postgres credentials (`agent_{uuid}`), use those credentials for the database connection instead of the admin connection pool. This leverages PostgreSQL RLS policies directly, removing the need to duplicate access control logic in Python.

## Completed

- **Phase 1**: Renamed `get_db` → `get_admin_db`, added `AdminDb` type alias, added `get_agent_db` dependency (in `auth.py` to avoid circular deps), added `AgentDb` type alias, added `Database.per_request()` classmethod (NullPool, no verification)
- **Phase 2**: Migrated read-heavy endpoints to `AgentDb` (`agent_definitions`, `stats`, all `runs` read endpoints). Write endpoints (`trigger_validation_runs`, `list_jobs`) keep `AdminDb` with explicit `require_admin_access`.
- **Tests**: Unit tests in `props/backend/test_auth.py`, integration tests with real Postgres + RLS in `props/backend/test_agent_db_integration.py`
- **Phase 5**: Admin token authentication — removed localhost admin exception, backend prints admin token URL on startup, frontend captures token from URL → `localStorage` → `Authorization: Bearer` header, paste-token fallback UI, WebSocket feed auth via `?token=` query param

### Key files

| File                                        | What changed                                                                    |
| ------------------------------------------- | ------------------------------------------------------------------------------- |
| `props/backend/auth.py`                     | `get_agent_db` generator, `AgentDb` type alias                                  |
| `props/backend/deps.py`                     | `get_admin_db`, `AdminDb` type alias                                            |
| `props/db/database.py`                      | `Database.per_request()` classmethod                                            |
| `props/backend/routes/agent_definitions.py` | `agent_db: AgentDb`                                                             |
| `props/backend/routes/stats.py`             | `agent_db: AgentDb` (all 3 endpoints)                                           |
| `props/backend/routes/runs.py`              | `agent_db: AgentDb` (4 read endpoints), `admin_db: AdminDb` (2 write endpoints) |

## Remaining Work

### Phase 3: Migrate write endpoints with RLS

- ⬚ Grading edges endpoints (when they exist)
- ⬚ Reported issues endpoints (when they exist)
- ⬚ Remove manual ACL checks that RLS now handles

### Phase 4: Clean up

- ⬚ Remove `ACL_CAN_*` sets for operations now handled by RLS
- ⬚ Simplify `CallerType` checks

## Endpoint Classification

**Already using `AgentDb`:**

| Endpoint                                | Notes                        |
| --------------------------------------- | ---------------------------- |
| `GET /api/definitions`                  | RLS filters by agent type    |
| `GET /api/runs`, `GET /api/runs/active` | RLS filters visible runs     |
| `GET /api/runs/{id}`                    | RLS enforces access          |
| `GET /api/runs/{id}/llm_requests`       | RLS filters visible requests |
| `GET /api/stats/*` (3 endpoints)        | RLS filters visible data     |

**Staying on `AdminDb`:**

| Endpoint                     | Reason                                       |
| ---------------------------- | -------------------------------------------- |
| `POST /api/llm/v1/responses` | Proxy needs admin INSERT into `llm_requests` |
| `POST /api/eval/*`           | Launches agent runs as admin                 |
| `POST /api/registry/*`       | Definition push needs admin INSERT           |
| `GET /api/ground_truth/*`    | Admin-only dashboard                         |
| `POST /api/runs/validation`  | Launches runs as admin                       |
| `GET /api/runs/jobs`         | Admin-only job tracking                      |

**Future (Phase 3):**

| Endpoint                     | Notes                         |
| ---------------------------- | ----------------------------- |
| `POST /api/runs/{id}/grades` | RLS enforces grader ownership |
| `POST /api/runs/{id}/issues` | RLS enforces critic ownership |

## Design Notes

### Connection Lifecycle

Per-request connections (Option A): Each agent request creates a `Database.per_request()` instance with NullPool. The `get_agent_db` generator disposes it in the `finally` block. Connection pooling per agent (Option B) can be added later if needed.

### RLS Policy Gaps

1. **`llm_requests`**: INSERT policy requires `current_agent_run_id() = agent_run_id` — conflicts with proxy pattern. Keep admin connection for LLM proxy.
2. **Stats views**: `recall_by_definition_example`, `recall_by_run` are granted SELECT to `agent_base`. RLS on underlying tables filters appropriately.

### Benefits

1. Single source of truth: RLS policies define access control
2. Defense in depth: RLS prevents unauthorized access even if Python ACL is wrong
3. Audit trail: Database logs show actual user performing operations

## Phase 5: Admin Token Authentication (Completed)

Replaced localhost exception with token-based admin auth (Jupyter-style).

### Key files

| File                                        | What changed                                       |
| ------------------------------------------- | -------------------------------------------------- |
| `props/backend/app.py`                      | Compute and print admin token URL on startup       |
| `props/backend/auth.py`                     | Removed localhost admin exception                  |
| `props/frontend/src/lib/api/client.ts`      | Bearer token middleware from `localStorage`        |
| `props/frontend/src/lib/stores/token.ts`    | Token capture, storage, and auth-failed management |
| `props/frontend/src/App.svelte`             | Token capture from URL, paste-token fallback UI    |
| `props/frontend/src/lib/stores/runsFeed.ts` | WebSocket auth via `?token=` query param           |
| `props/backend/routes/runs.py`              | WebSocket feed token validation                    |

## Non-Goals

- Connection pooling per agent (can add later if needed)
- Changing RLS policies (use existing policies)
- Removing admin connection pool (always needed for admin-only operations)
