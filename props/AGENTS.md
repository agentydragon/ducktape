@README.md

@testing/AGENTS.md

Frontend conventions: <frontend/AGENTS.md>. Authoring agent-facing prompt templates (under
<agents/>): <agents/AGENTS.md>. Do not `@`-transclude the runtime prompt templates
(<agents/docs/>, <agents/critic_dev/authoring_agents.md.mako>); the critic and grader templates
include what they need.

## Database

- **Never run `props db recreate` without the user's explicit agreement**: it drops all data,
  including expensively collected agent rollouts.
- Every schema change, RLS policies and helper functions included, is an Alembic migration in
  `db/migrations/versions/`; revision IDs are `YYYYMMDD000000`.
- `DROP VIEW ... CASCADE` drops all dependents: list them from `pg_depend` first, recreate them in
  dependency order, and re-grant permissions.
- Agents get persistent roles `agent_{agent_run_id}` with deterministic HMAC passwords;
  `current_agent_run_id()` derives the ID from the username for RLS
  (<orchestration/agent_credentials.py>).

Never start services by hand (raw `uvicorn`, manual postgres); use `docker compose` per README.
