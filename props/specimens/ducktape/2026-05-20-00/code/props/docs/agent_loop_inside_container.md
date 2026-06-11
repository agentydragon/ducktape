# Agent Loop Inside Container

## Overview

All agent loops run inside Docker containers. Each container is a self-contained agent that talks to the LLM proxy (integrated into the unified backend), executes tools via subprocess, and writes results to Postgres.

**Benefit:** Critic developer agents can author entire agentic systems - arbitrary LLM pipelines, workflows, subagents, classifiers, loops, tool calls, analysis, dispatch. Not limited to append-only single-agent patterns.

## Architecture

```
Host Scaffold                      Container
─────────────                      ─────────
run_loop_agent()
├─ Create temp DB user
├─ [Subagent spawn endpoint for critic-dev]
└─ Create container ─────────────> Container starts (CMD)
                                   ├─ props snapshot fetch (from Postgres)
                                   ├─ Construct prompt, start agent loop
                                   ├─ Calls LLM via proxy (OPENAI_BASE_URL)
                                   ├─ Tool calls = subprocess (exec)
                                   ├─ Writes to Postgres directly
                                   └─ Exits 0 on success
```

### Key implementations

- **Critic:** `props/agents/critic/main.py` — `DirectToolProvider` with exec, insert_issue, submit, report_failure tools. Entry point: `CMD ["/app/critic"]`.
- **Grader:** `props/agents/grader/loop.py` — `DirectToolProvider` with exec, list_pending, show_issue, show_tp/fp, insert_edges, fill_remaining, delete_edges, submit, report_failure tools. Snapshot grader mode via `props/agents/grader/main.py` with pg_notify.
- **Critic-dev (optimizer/improver):** `props/agents/critic_dev/optimize/main.py`, `props/agents/critic_dev/improve/main.py` — `DirectToolProvider` with exec, run_critic, wait_until_graded_tool, submit, report_failure tools.
- **Host scaffold:** `props/orchestration/agent_registry.py` — creates agent DB role, starts container, waits for exit, captures logs, determines status from exit code.

## Decisions

### Container Interface

| Aspect     | Decision                                                                     |
| ---------- | ---------------------------------------------------------------------------- |
| Entrypoint | Standard Dockerfile `CMD` (not `/init` convention)                           |
| Completion | Exit code 0 = success, non-zero = failure                                    |
| Status     | Host determines status (agents cannot update their own status due to RLS)    |
| Abort      | Host hard-kills container (`docker kill`) on timeout                         |
| Logs       | Capture and store container logs (see below)                                 |
| Lifecycle  | Host records `started_at`, `ended_at`, `container_exit_code` in `agent_runs` |

**Status determination (outside container):**

- Agents cannot update their own `agent_runs.status` column due to Row Level Security (no UPDATE policy)
- Host determines final status after container exits based on:
  - Exit code 0 + issues reported → `COMPLETED` (for critics)
  - Exit code 0 + all grading edges complete → `COMPLETED` (for graders)
  - Timeout (container killed) → `TIMED_OUT`
  - Exit code != 0 or validation failed → `REPORTED_FAILURE`
- Submit/report_failure tools perform validation but don't change status; they signal intent via exit code

### LLM Proxy

The LLM proxy is integrated into the unified backend (`props/backend/routes/llm.py`), not a separate service.

| Aspect            | Decision                                                       |
| ----------------- | -------------------------------------------------------------- |
| Env vars          | `OPENAI_BASE_URL`, `OPENAI_API_KEY` (Responses API compatible) |
| Token             | Same as existing Postgres password (`agent_{uuid}`)            |
| Token validation  | Via Postgres (lookup agent_runs by username pattern)           |
| Model restriction | One model per run, enforced by proxy                           |
| Cost budget       | Per-agent token counts, tracked via parent-child in agent_runs |
| Streaming         | Not supported (simplifies logging/budgeting)                   |
| Implementation    | FastAPI route in unified backend at `POST /v1/responses`       |
| Port              | 8000 (same as backend)                                         |

### Resource Limits

| Aspect          | Decision                                                                      |
| --------------- | ----------------------------------------------------------------------------- |
| USD budget      | `agent_runs.budget_usd` - max USD cost for agent + all child agents           |
| Timeout         | `agent_runs.timeout_seconds` - max wall-clock time before container is killed |
| Budget enforce  | LLM proxy checks budget before each request; rejects if exceeded              |
| Timeout enforce | `agent_registry` uses `asyncio.wait_for()` to kill container on timeout       |
| Lifecycle       | `agent_runs.started_at` and `ended_at` record container execution window      |

**Budget enforcement by proxy:**

1. On each LLM request, proxy queries `llm_run_costs` view to get current USD cost
2. Sum cost for agent + all child agents (via `parent_agent_run_id` tree)
3. Compare against `agent_runs.budget_usd` limit
4. Reject request with 429 if budget exceeded
5. Child agents inherit remaining budget from parent

Note: USD cost accounts for model pricing differences, cached input token discounts, etc.
The `llm_run_costs` view joins `llm_requests` with `model_metadata` pricing table.

**Timeout enforcement by agent_registry:**

1. Record `started_at` when creating AgentRun
2. Wrap container execution in `asyncio.wait_for(coro, timeout=timeout_seconds)`
3. If timeout fires, container is killed (run_loop_agent's finally block cleans up)
4. Record `ended_at` and set status to `TIMED_OUT`

### Tool Execution

| Aspect          | Decision                                                                 |
| --------------- | ------------------------------------------------------------------------ |
| Mechanism       | Subprocess inside container (no docker_exec from host)                   |
| Tool schema     | Generic `exec` tool taking command array                                 |
| Timeouts/limits | Reuse `mcp_infra.exec.subprocess.run_proc()` (standalone, no MCP needed) |
| Critique tools  | Direct tools: `insert_issue`, `insert_occurrence`, `submit`, etc.        |

**Exec implementation:** Reuse `mcp_infra/exec/subprocess.py:run_proc()` directly:

- Standalone async function, no MCP server dependency
- `MAX_BYTES_CAP = 150,000` bytes per stream (stdout/stderr)
- `MAX_EXEC_TIMEOUT_MS = 300,000` (5 minutes)
- Clean timeout handling with `asyncio.wait_for()` + process kill
- UTF-8 safe truncation via `errors="replace"`
- Returns `ExecOutcome` with discriminated exit status: `Exited | TimedOut | Killed`

### Agent Loop

| Aspect        | Decision                                                                      |
| ------------- | ----------------------------------------------------------------------------- |
| Location      | Inside container, part of props package                                       |
| API style     | OpenAI Responses API                                                          |
| Max turns     | Don't enforce (cost/timeout are sufficient)                                   |
| Context limit | Container's responsibility; compaction is future work                         |
| Completion    | "submit" tool validates → returns errors (agent retries) or succeeds → exit 0 |
| Code reuse    | Uses `agent_core.Agent` with `DirectToolProvider`                             |

### Snapshot Grader Mode

| Aspect        | Decision                                                          |
| ------------- | ----------------------------------------------------------------- |
| Lifecycle     | Container runs indefinitely (no exit between grading batches)     |
| Wake/sleep    | Internal loop uses pg_notify on `grading_pending` channel         |
| Drift handler | `GraderDriftHandler.on_before_sample()` returns `Abort()` → sleep |
| Timeout       | No timeout for snapshot graders (eternal)                         |
| Scope         | One grader per snapshot, grades all critiques for that snapshot   |

**How snapshot graders work:**

- "Drift" = ungraded (critique issue, GT occurrence) pairs in `grading_pending` view
- Grader goal: make `grading_pending` empty for its snapshot
- Loop: check drift → grade until empty → sleep waiting for `NOTIFY grading_pending`
- GT changes (new TPs/FPs, edits) trigger notifications that wake the grader
- Uses `asyncio.Event` for coordinated wake/sleep, background `pg_listen` task
- On context length exceeded: grader supervisor auto-restarts with fresh context

**pg_notify permissions:** Grader uses its temp user credentials (`agent_{uuid}`) for LISTEN. PostgreSQL allows any connected user to LISTEN on any channel without special grants. Notifications include `snapshot_slug` in the payload; the grader filters to only process notifications for its snapshot.

**Single implementation, two modes:**

- One-off (`GraderTypeConfig`): grades single critic run, has `submit` + `report_failure`
- Snapshot (`SnapshotGraderTypeConfig`): grades all critiques for snapshot, `report_failure` only (no `submit` - drift handler controls sleep)
- Mode flag controls tool availability; all other tools identical

**Grader Tools (DirectToolProvider):**

| Tool             | Args                                              | Returns             | Mode    | Purpose                                     |
| ---------------- | ------------------------------------------------- | ------------------- | ------- | ------------------------------------------- |
| `exec`           | `cmd`, `timeout_ms`, `cwd`                        | `ExecResult`        | both    | Shell commands for file reading, psql, etc. |
| `list_pending`   | `issue?`, `gt?`, `run?`                           | `list[PendingEdge]` | both    | Query `grading_pending` view                |
| `show_issue`     | `issue_id`, `run?`                                | `IssueDetails`      | both    | View reported issue + occurrence locations  |
| `show_gt`        | `gt_ref` (tp/id/occ or fp/id/occ)                 | `GTDetails`         | both    | View ground truth occurrence + rationale    |
| `insert_edges`   | `issue_id`, `rationale`, `edges[]`                | `str`               | both    | Create multiple edges: `{gt_ref, credit}`   |
| `fill_remaining` | `issue_id`, `expected_count`, `rationale`, `run?` | `str`               | both    | Bulk-fill remaining edges with credit=0     |
| `delete_edges`   | `issue_id`, `run?`                                | `str`               | both    | Delete all edges for issue (to redo)        |
| `submit`         | `summary`                                         | `None`              | one-off | Finalize grading (validates no pending)     |
| `report_failure` | `message`                                         | `None`              | both    | Report blocking error, exit                 |

**Edge model:** Every `(critique_issue, matchable_gt_occurrence)` pair needs an edge. Credit 0.0-1.0 for both TPs and FPs. Use credit=0 for non-matches, >0 for matches (quality of match).

**Grader loop:** `DriftHandler.on_before_sample()` checks `grading_pending` view. Returns `Abort()` when empty → agent loop exits → outer loop sleeps on pg_notify → wakes and creates fresh agent context.

**Grader supervisor:** `props/orchestration/grader_supervisor.py` manages grader lifecycle — listens on `snapshot_created` channel, spawns one grader per snapshot, handles restarts.

### Subagent Spawning

| Aspect          | Decision                                                                        |
| --------------- | ------------------------------------------------------------------------------- |
| Spawn           | REST API call to backend (`/api/runs/critic`)                                   |
| Status query    | Direct Postgres query (no external call needed)                                 |
| Results/logs    | Direct Postgres query                                                           |
| Cost accounting | Counts against parent's budget                                                  |
| Limits          | No explicit concurrency/spawn limits; cost + timeout sufficient                 |
| Wait helpers    | `wait_until_graded_tool` polls `grading_pending` view directly inside container |

Critic-dev agents have `DirectToolProvider` tools that call the backend REST API for spawning and poll the database directly for grading status. No MCP required.

```
Backend                                 Container (critic-dev)
───────                                 ──────────────────────
/api/runs/critic (REST)             DirectToolProvider
├─ Spawns critic container   ◄──────────  run_critic tool (HTTP POST)
└─ Returns critic_run_id

PostgreSQL                              DirectToolProvider
──────────                              ──────────────────
grading_pending view         ◄──────────  wait_until_graded_tool (polls DB)
└─ Returns when drift = 0
```

**Tools provided by DirectToolProvider:**

- `run_critic(definition_id, example, ...)` → critic_run_id (calls REST API)
- `wait_until_graded_tool(critic_run_id)` → grading results (polls database directly)

**Typical critic-dev workflow:**

1. `run_critic(...)` → critic_run_id (returns when critic completes)
2. `wait_until_graded_tool(critic_run_id)` (polls `grading_pending` until empty)
3. Query metrics from DB

### Observability

| Aspect         | Decision                                                        |
| -------------- | --------------------------------------------------------------- |
| LLM calls      | Logged by LLM proxy to `llm_requests` table                     |
| Container logs | Capture stdout/stderr, store in columns on `agent_runs`         |
| Access         | Critic-dev agents and humans can query logs from DB             |
| Cost tracking  | `llm_request_costs` and `llm_run_costs` views (per-request/run) |

**`llm_requests` table:**

- `id`, `agent_run_id` (FK), `created_at`
- `request_body` (JSONB) - full OpenAI Responses API request
- `response_body` (JSONB) - full response including `usage` field
- `model` (denormalized for filtering)
- `latency_ms`
- `error` (TEXT, nullable)
- Cost computation via `llm_request_costs` view joining with `model_metadata` pricing table
- Aggregated per-run costs via `llm_run_costs` view

### Security

| Aspect            | Decision                                              |
| ----------------- | ----------------------------------------------------- |
| Syscall filtering | None (containers are isolated enough)                 |
| Network           | Only LLM proxy, Postgres, subagent endpoint reachable |
| Registry          | Critic-dev agents can push new images by digest       |

### Docker Compose Topology

Services in `props/compose.yaml`:

- `postgres` (5433:5432) - on `props-internal` + `props-agents`
- `registry` (5000:5000) - on `props-internal` + `default`
- `backend` (8000:8000) - on `props-internal` + `props-agents` (serves LLM proxy at `/v1/responses`, registry proxy at `/v2/*`, critic runs API at `/api/runs/critic`, dashboard API)

**Network topology:**

- `props-internal` (internal: true) - postgres, registry, backend
- `props-agents` - postgres, backend (agent containers join this)
- Agent containers reach LLM proxy at `props-backend:8000`

## Historical Notes

Previous iterations used HTTP MCP servers (`CriticSubmitServer`, `GraderSubmitServer`, `PromptEvalServer`), an `events` table, and host-side agent loops. All have been removed in favor of in-container `DirectToolProvider` tools, `llm_requests` table, and container stdout/stderr capture.
