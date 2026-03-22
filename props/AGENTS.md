@README.md

# Agent Guide

## Component Docs

- **Backend:** @backend/AGENTS.md
- **Frontend:** @frontend/AGENTS.md
- **Tests:** @testing/AGENTS.md

@docs/AGENTS.md

**Key agent-facing templates (transcluded):**

@agents/docs/system_access.md
@agents/docs/db/ground_truth.md.mako
@agents/docs/db/examples.md.mako
@agents/docs/db/evaluation_flow.md.mako
@agents/critic_dev/authoring_agents.md.mako

## Database Migrations (Alembic)

**All schema changes must go through Alembic migrations.**

- Migrations: `db/migrations/versions/`, config: `db/migrations/env.py`
- Revision IDs: YYYYMMDD000000 format (e.g., `20251213000000`)
- RLS policies: managed in `db/setup.py` via `enable_rls()` (not in migrations)
- RLS helper functions: created in migrations (they're schema)

**CASCADE WARNING:** `DROP VIEW ... CASCADE` drops all dependents. Before writing such a migration: query `pg_depend` to list ALL dependent views, recreate them in dependency order, and re-grant permissions.

## Agent Database Roles

Agents get persistent PostgreSQL roles with RLS-scoped access. Passwords are deterministic (HMAC-SHA256 of salt + agent_run_id).

- Username pattern: `agent_{agent_run_id}`
- `current_agent_run_id()` extracts ID from username for RLS policies
- See `orchestration/agent_credentials.py`

## MCP I/O vs DB Persistence Models

Do not use MCP I/O types (`CriticSubmitPayload`, `ReportedIssue`, etc.) in database schemas. Two parallel hierarchies:

- **MCP I/O Models** (`agents/critic/models.py`, `agents/grader/models.py`): API contract with rich types
- **DB Persistence Models** (`db/snapshots.py`): stable storage format with primitives
- **Conversion**: `agents/grader/persistence.py` bridges between them

## Service Management

**Never start services manually** (no raw `uvicorn`, no manual postgres). Use `docker compose` as described in README.md.

## Database Safety

**NEVER run `props db recreate` without the user's explicit agreement.** It drops ALL data including expensively-collected agent rollouts.
