# Agent Loop Inside Container

## Overview

All agent loops run inside Docker containers. Each container is a self-contained agent that talks to the LLM proxy, executes tools via subprocess, and writes results to Postgres.

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

- **Critic:** `props/agents/critic/main.py` — critic entry point and tools; `CMD ["/app/critic"]`.
- **Grader:** [`props/agents/grader/main.py`](../agents/grader/main.py) runs the persistent snapshot grader; [`props/agents/grader/tools.py`](../agents/grader/tools.py) defines its tool data models.
- **Critic-dev:** [`props/agents/critic_dev/loop.py`](../agents/critic_dev/loop.py) builds optimizer/improver tools, including critic spawn and wait; [`grading.py`](../agents/critic_dev/grading.py) checks grading status.
- **Host scaffold:** `props/orchestration/agent_registry.py` — creates agent DB role, starts container, waits for exit, captures logs, determines status from exit code.

## Decisions

### Container Interface

| Aspect     | Decision                                                                     |
| ---------- | ---------------------------------------------------------------------------- |
| Entrypoint | Standard Dockerfile `CMD` (not `/init` convention)                           |
| Completion | Exit code 0 = success, non-zero = failure                                    |
| Status     | Host determines status (agents cannot update their own status due to RLS)    |
| Abort      | Host hard-kills container/Pod on timeout                                     |
| Logs       | Runtime logs are shipped to Loki and read through the backend                |
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

The LLM proxy is a separate service (`props/llm_proxy`) that reuses the backend LLM router.

| Aspect            | Decision                                                                        |
| ----------------- | ------------------------------------------------------------------------------- |
| Env vars          | `OPENAI_BASE_URL`, `OPENAI_API_KEY` (OpenAI-compatible API)                     |
| Token             | Same as existing Postgres password (`agent_{uuid}`)                             |
| Token validation  | Via Postgres (lookup agent_runs by username pattern)                            |
| Model restriction | One model per run, enforced by proxy                                            |
| Cost budget       | Per-agent token counts, tracked via parent-child in agent_runs                  |
| Streaming         | Not supported (simplifies logging/budgeting)                                    |
| Implementation    | LLM proxy routes at `/v1/responses`, `/v1/chat/completions`, and `/v1/messages` |
| Port              | 8000                                                                            |

The route an agent may call is determined by `model_metadata.api_shape`.
`responses` models use `/v1/responses`, `chat_completions` models use
`/v1/chat/completions`, and `anthropic` models use `/v1/messages`.

### Registry Proxy

The registry proxy is a separate service (`props/registry_proxy`) that reuses the shared registry route module.

| Aspect           | Decision                                                          |
| ---------------- | ----------------------------------------------------------------- |
| Env vars         | `PROPS_REGISTRY_URL`                                              |
| Token            | Same as existing Postgres password (`agent_{uuid}`)               |
| Token validation | Via Postgres auth middleware                                      |
| Access           | Critic-dev agents can read/push by digest; critics/graders cannot |
| Implementation   | Registry routes at `/v2/*`                                        |
| Port             | 8000                                                              |

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

| Aspect          | Decision                                                            |
| --------------- | ------------------------------------------------------------------- |
| Mechanism       | Subprocess inside container (no docker_exec from host)              |
| Tool schema     | Generic `exec` tool taking command array                            |
| Timeouts/limits | Reuse `util.exec.subprocess.run_proc()` (standalone, no MCP needed) |
| Critique tools  | Direct tools: `insert_issue`, `insert_occurrence`, `submit`, etc.   |

**Exec implementation:** Reuse `util/exec/subprocess.py:run_proc()` directly:

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
| API style     | OpenAI-compatible shape selected by `model_metadata.api_shape`                |
| Max turns     | Don't enforce (cost/timeout are sufficient)                                   |
| Context limit | Container's responsibility; compaction is future work                         |
| Completion    | "submit" tool validates → returns errors (agent retries) or succeeds → exit 0 |
| Code reuse    | Uses `agent_core.Agent` with `DirectToolProvider`                             |

### Snapshot Grader Mode

| Aspect     | Decision                                                                                    |
| ---------- | ------------------------------------------------------------------------------------------- |
| Lifecycle  | Persistent loop processes batches and sleeps; supervisor reconciles one grader per snapshot |
| Wake/sleep | `sleep` waits on snapshot-filtered `grading_pending` notifications                          |
| Context    | Context overflow restarts the loop from scratch; completed edges are skipped                |
| Timeout    | Snapshot grader launch has no registry run timeout; supervisor controls pod lifecycle       |

**How snapshot graders work:**

- `get_drift` exposes pending grading edges and clustering issues for the snapshot. `sleep` requires both to be empty before waiting; GT insert/delete and new reported issues/occurrences notify the `grading_pending` channel.
- The `asyncio.Event` listener ignores notifications for other snapshots. The grader's `agent_{uuid}` PostgreSQL user can `LISTEN` without a channel grant.
- A context-length overflow starts a fresh agent context; already-created grading edges are excluded from `grading_pending`, so completed work is retained.

The grader's persistent loop, notification handling, and tool binding are in [`main.py`](../agents/grader/main.py); argument and result schemas are in [`tools.py`](../agents/grader/tools.py). The registry starts snapshot graders without a run timeout; the supervisor reconciles pods and backs off after failures ([`agent_registry.py`](../orchestration/agent_registry.py), [`grader_supervisor.py`](../orchestration/grader_supervisor.py)).

**Edge model:** Each critique issue and matchable TP-or-FP occurrence pair within the snapshot needs an edge. Credit is 0–1: zero means non-match; positive credit records a match's quality. `fill_remaining` writes zero-credit edges for the verified pending count.

### Subagent Spawning

| Aspect          | Decision                                                                                                                                                                       |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Spawn           | Backend REST `POST /api/runs/critic`                                                                                                                                           |
| Status/grading  | Direct Postgres polls; grading requires an exited child started by the caller                                                                                                  |
| Concurrency     | Configured per-model limits queue critic/critic-dev runs; unset models are uncapped ([`app.py`](../backend/app.py), [`agent_registry.py`](../orchestration/agent_registry.py)) |
| Logs            | Backend `GET /api/runs/{id}/logs`                                                                                                                                              |
| Cost accounting | Child runs count against the parent's budget                                                                                                                                   |

Critic-dev uses `start_critic` → `wait_until_critic_completed` → `wait_until_graded_tool`; spawning returns the run ID immediately. Both waits are bounded, with configurable timeout limits. These tools use REST and direct database access; no MCP is needed. Grading status is available only after the critic exits and when the caller started that run. See [`loop.py`](../agents/critic_dev/loop.py) and [`grading.py`](../agents/critic_dev/grading.py).

### Observability

| Aspect         | Decision                                                                |
| -------------- | ----------------------------------------------------------------------- |
| LLM calls      | Logged by LLM proxy to `llm_requests` table                             |
| Container logs | Shipped by Promtail to Loki, fetched by `GET /api/runs/{id}/logs`       |
| Access         | Critic-dev agents and humans read logs through the backend's RLS checks |
| Cost tracking  | `llm_request_costs` and `llm_run_costs` views (per-request/run)         |

[`props/db/models.py`](../db/models.py) maps the `llm_requests` payload, usage, latency, and error fields, along with the request and per-run cost views.

### Security

| Aspect            | Decision                                              |
| ----------------- | ----------------------------------------------------- |
| Syscall filtering | None (containers are isolated enough)                 |
| Network           | Only LLM proxy, Postgres, subagent endpoint reachable |
| Registry          | Critic-dev agents can push new images by digest       |

### Runtime Topologies

Kubernetes production uses split services: backend/frontend, `props-llm-proxy`,
`props-registry-proxy`, CNPG PostgreSQL, and Forgejo registry storage. The
backend injects `OPENAI_BASE_URL=<llm_proxy_url>/v1` and `PROPS_REGISTRY_URL`
into agent Pods.

The local Docker compose file is a lightweight development topology. Docker E2E
fixtures start the split proxy services directly when they need production-like
proxy behavior.

- `postgres` (5433:5432) - on `props-internal` + `props-agents`
- `registry` (5000:5000) - on `props-internal` + `default`
- `backend` (8000:8000) - serves dashboard/control APIs in local dev

**Network topology:**

- `props-internal` (internal: true) - postgres, registry, backend
- `props-agents` - postgres, backend (agent containers join this)

## Historical Notes

Previous iterations used HTTP MCP servers (`CriticSubmitServer`,
`GraderSubmitServer`, `PromptEvalServer`), an `events` table, host-side agent
loops, backend-hosted LLM/registry routes, and DB-persisted container stdout /
stderr. These were replaced by in-container `DirectToolProvider` tools, the
standalone LLM and registry proxies, `llm_requests`, and Loki-backed container
logs.
