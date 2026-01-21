# Plan: Agent Loop Inside Container

## Overview

Move the LLM API querying and agent loop from the host scaffold into the Docker container. The container becomes a self-contained agent that talks to an LLM proxy, executes tools via subprocess, and writes results to Postgres.

**Benefit:** Prompt optimizer agents can author entire agentic systems - arbitrary LLM pipelines, workflows, subagents, classifiers, loops, tool calls, analysis, dispatch. Not limited to append-only single-agent patterns.

## Architecture

### Current

```
Host Scaffold                      Container
─────────────                      ─────────
AgentEnvironment
├─ Create temp DB user
├─ Start HTTP MCP server
└─ Create container ─────────────> Container starts
                                   /init → stdout = system prompt
Agent.run() (host-side)
├─ Sample LLM (OpenAI API)  ◄───── Control on host
├─ Route tool calls:
│   ├─ docker_exec ──────────────> props critic-agent ...
│   └─ critic_submit (HTTP MCP)
└─ DatabaseEventHandler
    └─> Write events to DB
```

### Proposed

```
Host Scaffold                      Container
─────────────                      ─────────
AgentEnvironment (simplified)
├─ Create temp DB user
├─ Start LLM proxy (rspcache)
├─ [Subagent spawn endpoint for PO/PI]
└─ Create container ─────────────> Container starts (CMD)
                                   ├─ props snapshot fetch (from Postgres)
                                   ├─ Construct prompt, start agent loop
                                   ├─ Calls LLM via proxy (OPENAI_BASE_URL)
                                   ├─ Tool calls = subprocess (exec)
                                   ├─ Writes to Postgres directly
                                   └─ Exits 0 on success
```

## Decisions

### Container Interface

| Aspect     | Decision                                           |
| ---------- | -------------------------------------------------- |
| Entrypoint | Standard Dockerfile `CMD` (not `/init` convention) |
| Completion | Exit code 0 = success, non-zero = failure          |
| Abort      | Host hard-kills container (`docker kill`)          |
| Logs       | Capture and store container logs (see below)       |

### LLM Proxy

| Aspect            | Decision                                                                     |
| ----------------- | ---------------------------------------------------------------------------- |
| Env vars          | `OPENAI_BASE_URL`, `OPENAI_API_KEY` (Responses API compatible)               |
| Token             | Same as existing Postgres password (`agent_{uuid}`)                          |
| Token validation  | Via Postgres (lookup agent_runs by username pattern)                         |
| Model restriction | One model per run, enforced by proxy                                         |
| Cost budget       | Per-agent token counts, tracked via parent-child in agent_runs               |
| Streaming         | Not supported (simplifies logging/budgeting)                                 |
| Implementation    | New FastAPI proxy, similar pattern to rspcache but simpler (Responses only)  |
| Container         | New `llm-proxy` service in compose.yaml on `props-internal` + `props-agents` |
| Port              | 5052 (internal), exposed on `props-agents` network                           |

**Reference:** Existing `rspcache/` proxy handles OpenAI Responses API with caching, streaming, and token auth. New proxy is simpler:

- No caching (every request goes upstream)
- No streaming (simplifies logging)
- Auth via Postgres temp user lookup (not separate token table)
- Logs full request/response to new `llm_requests` table

### Tool Execution

| Aspect          | Decision                                                                 |
| --------------- | ------------------------------------------------------------------------ |
| Mechanism       | Subprocess inside container (no docker_exec from host)                   |
| Tool schema     | Generic `exec` tool taking command array                                 |
| Timeouts/limits | Reuse `mcp_infra.exec.subprocess.run_proc()` (standalone, no MCP needed) |
| Critique tools  | Bundle existing `props critic-agent` CLI (insert-issue, submit, etc.)    |

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
| Code reuse    | Could use `agent_core.Agent` with exec tool, or simpler standalone loop       |

### Grader Daemon Mode

| Aspect        | Decision                                                          |
| ------------- | ----------------------------------------------------------------- |
| Lifecycle     | Container runs indefinitely (no exit between grading batches)     |
| Wake/sleep    | Internal loop uses pg_notify on `grading_pending` channel         |
| Drift handler | `GraderDriftHandler.on_before_sample()` returns `Abort()` → sleep |
| Timeout       | No timeout for daemon graders (eternal)                           |
| Scope         | One daemon per snapshot, grades all critiques for that snapshot   |

**How daemon graders work:**

- "Drift" = ungraded (critique issue, GT occurrence) pairs in `grading_pending` view
- Daemon goal: make `grading_pending` empty for its snapshot
- Loop: check drift → grade until empty → sleep waiting for `NOTIFY grading_pending`
- GT changes (new TPs/FPs, edits) trigger notifications that wake the daemon
- Uses `asyncio.Event` for coordinated wake/sleep, background `pg_listen` task
- On context length exceeded: daemon manager auto-restarts with fresh context

**pg_notify permissions:** Daemon uses admin credentials for LISTEN (not temp user credentials). Agent temp users don't need LISTEN permissions - they only write to DB, the notification triggers fire from postgres-owned SECURITY DEFINER functions.

### Subagent Spawning

| Aspect          | Decision                                                            |
| --------------- | ------------------------------------------------------------------- |
| Spawn           | External HTTP endpoints by agent type: `/run_critic`, `/run_grader` |
| Status query    | Direct Postgres query (no external call needed)                     |
| Results/logs    | Direct Postgres query                                               |
| Cost accounting | Counts against parent's budget                                      |
| Limits          | No explicit concurrency/spawn limits; cost + timeout sufficient     |
| Wait helpers    | In-container tools like "wait_until_graded" can poll DB internally  |

**Spawn API sketch:**

```
POST /run_critic
{
  "image": "critic@sha256:...",
  "example": {"snapshot_slug": "...", "example_kind": "...", ...},
  "model": "gpt-4o"
}
→ {"agent_run_id": "..."}  # Returns immediately, agent runs async
```

Only `/run_critic` needed - daemon grader automatically grades all critics.

**Authentication:**

- Part of existing FastAPI backend (`props/backend/`)
- Backend checks source:

  ```
  # Agent (from container network) - must provide credentials
  curl -u "agent_<uuid>:<password>" http://backend:8000/api/run_critic ...

  # Human (from localhost) - no credentials needed
  curl http://localhost:8000/api/run_critic ...
  ```

- FastAPI dependency checks request:
  - From localhost/127.0.0.1 → human mode, `parent_run_id` = NULL
  - From container network → require Basic auth, validate against Postgres, set `parent_run_id`
- Simple IP check is sufficient for personal infra (not exposed to internet)

Spawn is **non-blocking** - returns immediately with agent_run_id. PO can spawn multiple critics in parallel.

**Wait helpers (in-container, not external):**

- `wait_for_agent(agent_run_id)` - polls `agent_runs` until agent completes
- `wait_for_grading(critic_run_id)` - polls until daemon grader has graded this critic
- Implemented as CLI tools: `props agent wait <id>`, `props agent wait-graded <critic_id>`
- No external endpoint needed - just DB polling

**Typical PO workflow:**

1. `POST /run_critic` → critic_run_id (returns immediately)
2. `props agent wait <critic_run_id>` (blocks until critic done)
3. `props agent wait-graded <critic_run_id>` (blocks until daemon grader grades it)
4. Query metrics from DB

### Observability

| Aspect         | Decision                                                                   |
| -------------- | -------------------------------------------------------------------------- |
| LLM calls      | Logged by LLM proxy to new `llm_requests` table                            |
| Container logs | Capture stdout/stderr, store in new columns on `agent_runs`                |
| Access         | PO/PI agents and humans can query logs from DB                             |
| Events table   | Deprecated (big bang cutover) - LLM proxy logs + container logs replace it |

**New `llm_requests` table:**

- `id`, `agent_run_id` (FK), `created_at`
- `request_body` (JSONB) - full OpenAI Responses API request
- `response_body` (JSONB) - full response including `usage` field
- `input_tokens`, `output_tokens` (extracted from response for easy querying)
- `model` (denormalized for filtering)
- `latency_ms`
- Cost computation via view joining with model pricing metadata table

**New columns on `agent_runs`:**

- `container_stdout` (TEXT) - captured stdout
- `container_stderr` (TEXT) - captured stderr
- Or possibly a separate `agent_logs` table if logs get large

### Security

| Aspect            | Decision                                              |
| ----------------- | ----------------------------------------------------- |
| Syscall filtering | None (containers are isolated enough)                 |
| Network           | Only LLM proxy, Postgres, subagent endpoint reachable |
| Registry          | PO/PI can push new images by digest                   |

### Docker Compose Topology

**Current services in `props/compose.yaml`:**

- `postgres` (5433:5432) - on `props-internal` + `props-agents`
- `registry` (5050:5000) - on `props-internal` + `default`
- `registry-proxy` (5051) - on `props-internal` + `props-agents`

**New service to add:**

```yaml
llm-proxy:
  image: props-llm-proxy:latest
  container_name: props-llm-proxy
  ports:
    - "5052:5052"
  networks:
    - props-internal
    - props-agents
  environment:
    PGHOST: props-postgres
    PGPORT: 5432
    PGUSER: ${PGUSER}
    PGPASSWORD: ${PGPASSWORD}
    PGDATABASE: ${PGDATABASE}
    OPENAI_API_KEY: ${OPENAI_API_KEY}
  depends_on:
    postgres:
      condition: service_healthy
```

**Network topology:**

- `props-internal` (internal: true) - postgres, registry, registry-proxy, llm-proxy
- `props-agents` - postgres, registry-proxy, llm-proxy (agent containers join this)
- Agent containers reach LLM proxy at `props-llm-proxy:5052`

## Implementation Sketch

### Critic Agent (Container Side)

```python
#!/usr/bin/env python3
"""Critic agent - runs inside container."""
import os
import subprocess
import sys
from openai import OpenAI

def main():
    # 1. Fetch snapshot (uses PG* env vars automatically)
    snapshot_slug = os.environ["SNAPSHOT_SLUG"]
    subprocess.run(["props", "snapshot", "fetch", snapshot_slug], check=True)

    # 2. Construct prompt (reuses existing rendering)
    system_prompt = render_critic_prompt(
        snapshot_slug=snapshot_slug,
        example_kind=os.environ["EXAMPLE_KIND"],
        files_hash=os.environ.get("FILES_HASH"),
    )

    # 3. Define tools
    tools = [{
        "type": "function",
        "function": {
            "name": "exec",
            "description": "Execute a command",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["command"]
            }
        }
    }]

    # 4. Agent loop
    client = OpenAI(
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
    )

    messages = [{"role": "system", "content": system_prompt}]

    while True:
        response = client.responses.create(
            model=os.environ["MODEL"],
            input=messages,
            tools=tools,
        )

        # Handle response, execute tools via subprocess
        # Submit tool: runs `props critic-agent submit ...`
        #   - Returns validation errors → agent sees output, can fix and retry
        #   - Succeeds → exit 0

    sys.exit(0)

if __name__ == "__main__":
    main()
```

### Host Scaffold (Simplified)

```python
async def run_critic(snapshot_slug: str, example: Example) -> AgentRunResult:
    async with TempDatabaseUser() as db_user:
        # Start container (no volume mount - agent fetches snapshot itself)
        container = await start_container(
            image=example.critic_image,
            env={
                "OPENAI_BASE_URL": rspcache_url,
                "OPENAI_API_KEY": db_user.password,  # Same token as PG
                "PGHOST": ..., "PGUSER": db_user.name, "PGPASSWORD": db_user.password, ...
                "SNAPSHOT_SLUG": snapshot_slug,
                "EXAMPLE_KIND": example.kind,
                "FILES_HASH": example.files_hash,
                "MODEL": "gpt-4o",
            },
        )

        # Wait for completion
        exit_code = await container.wait()
        logs = await container.logs()

        # Store logs in DB, determine status
        return AgentRunResult(
            status="completed" if exit_code == 0 else "failed",
            logs=logs,
        )
```

## Code Changes

### To Remove

| File/Component                                | Reason                                              |
| --------------------------------------------- | --------------------------------------------------- |
| `critic/submit_server.py`                     | `CriticSubmitServer` replaced by CLI + exit 0       |
| `grader/submit_server.py`                     | `GraderSubmitServer` replaced by CLI + exit 0       |
| `prompt_optimize/prompt_optimizer.py`         | `PromptEvalServer` class replaced by spawn endpoint |
| `agent_handle.py`                             | Host-side agent loop no longer needed               |
| `db_event_handler.py`                         | `DatabaseEventHandler` - events table deprecated    |
| Events table                                  | Replaced by LLM proxy logs + container logs         |
| HTTP MCP server startup in `AgentEnvironment` | No longer needed                                    |
| `docker_exec` tool from host                  | Tools run via subprocess inside container           |

**Events table deprecation - files to update/remove:**

Writing events (remove):

- `props/db_event_handler.py` - `DatabaseEventHandler` class
- `props/db/models.py` - `Event` model class

Reading events (update to use `llm_requests` or remove):

- `props/backend/routes/runs.py` - WebSocket streams, API responses
- `props/core/cli/cmd_speak_with_dead.py` - replay agent execution
- `props/core/cli/cmd_analyze_exec.py` - docker_exec pattern analysis
- `props/core/cli/cmd_critic_dev_helpers.py` - development utilities
- `props/core/gepa/gepa_adapter.py` - reflection event filtering

Schema/views (drop):

- `event_costs` view - extract costs from event payloads
- `run_costs` view - aggregate event costs
- Events table + indexes + RLS policies

Tests (update):

- `props/core/critic/test_e2e.py` - event assertions
- `props/db/test_split_based_rls.py` - event RLS tests
- `props/db/test_agent_queries.py` - event creation tests
- `props/testing/fixtures/db.py` - event fixtures

Documentation:

- `props/core/docs/db/events.md.j2` - remove

### To Simplify

| File/Component                                 | Change                                                                       |
| ---------------------------------------------- | ---------------------------------------------------------------------------- |
| `agent_setup.py` / `AgentEnvironment`          | Remove MCP server, just: create DB user, start container, wait, capture logs |
| `agent_registry.py` / `AgentRegistry`          | Simplified - just calls spawn endpoint                                       |
| `docker_env.py` / `PropertiesDockerCompositor` | Simplify or remove - less orchestration needed                               |
| Container images                               | Change from `/init` producing prompt to `CMD` running full agent             |

### To Add

| Component                   | Purpose                                                                      |
| --------------------------- | ---------------------------------------------------------------------------- |
| **LLM proxy**               | Token validation, request/response logging, cost tracking, model enforcement |
| **Spawn endpoint**          | `/run_critic` HTTP endpoint for PO to spawn critics                          |
| **In-container agent loop** | OpenAI Responses API client, exec tool, submit handling                      |
| **Wait CLI tools**          | `props agent wait <id>`, `props agent wait-graded <critic_id>`               |
| **Log capture**             | Store container stdout/stderr in DB                                          |
| **`llm_requests` table**    | New table for LLM proxy logs (request/response/tokens)                       |
| **Model pricing table**     | Metadata for computing dollar costs from token counts                        |

**New CLI subcommand group:** `props agent`

- `props agent wait <agent_run_id>` - poll until agent completes
- `props agent wait-graded <critic_run_id>` - poll until daemon grader grades critic
- Location: `props/core/cli/cmd_agent.py` (new file)

### To Keep (unchanged or minor changes)

| Component                 | Notes                                           |
| ------------------------- | ----------------------------------------------- |
| `props critic-agent` CLI  | Already exists, used by agent via subprocess    |
| `props grader-agent` CLI  | Already exists, used by agent via subprocess    |
| `props snapshot fetch`    | Already exists, used by agent to fetch snapshot |
| `grader/daemon.py`        | Keep, but agent loop moves inside container     |
| `grader/drift_handler.py` | Keep, runs inside container now                 |
| `noop_classifier/`        | Keep as-is (specialized utility)                |
| Database models, RLS      | Keep as-is                                      |
| Registry proxy            | Keep as-is                                      |

## Migration Path

### Phase 1: LLM Proxy

1. Build custom LLM proxy (similar to registry_proxy):
   - Token validation via Postgres (agent_runs lookup)
   - Request/response logging to DB
   - Cost tracking per agent_run_id (sum up parent chain)
   - Model enforcement (only allow assigned model)
   - No streaming

### Phase 2: Simple Critic

1. Write minimal agent loop in props package
2. Update critic container image to use `CMD` running agent loop
3. Simplify `AgentEnvironment`:
   - Remove HTTP MCP server startup
   - Start container, wait for exit, capture logs
4. Test with existing critic prompts

### Phase 3: Grader

1. Update grader to run loop internally with pg_notify
2. Daemon mode: container stays running, internal sleep/wake

### Phase 4: Prompt Optimizer

1. Add subagent spawn endpoint
2. Update PO to use new architecture
3. Give PO access to container logs

### Phase 5: Cleanup

1. Remove deprecated code paths
2. Drop events table (or archive)
3. Update dashboard to use LLM proxy logs

## Resolved

### HTTP MCP Servers

| Server                 | Location                              | Tools                                        | Fate                                                |
| ---------------------- | ------------------------------------- | -------------------------------------------- | --------------------------------------------------- |
| **CriticSubmitServer** | `critic/submit_server.py`             | `submit`, `report_failure`                   | **Kill** - replaced by CLI + exit 0                 |
| **GraderSubmitServer** | `grader/submit_server.py`             | `grader_submit`, `report_failure`            | **Kill** - replaced by CLI + exit 0                 |
| **PromptEvalServer**   | `prompt_optimize/prompt_optimizer.py` | `run_critic`, `run_grader`, `report_failure` | **Transform** → external subagent spawn endpoint    |
| **ClassifierServer**   | `noop_classifier/classifier.py`       | `submit_classifications`                     | **Keep** - specialized utility, not core agent flow |

### Snapshot Fetching

**Decision:** Keep `props snapshot fetch` inside container.

- Simpler - no host-side change needed
- Already works
- Agent runs `props snapshot fetch <slug>` as part of init, uses PG\* env vars

### Cost Budget Propagation

**Decision:** LLM proxy queries `agent_runs.parent_agent_run_id` to compute budget tree.

- Parent spawns child → child's cost counts against parent's budget
- Proxy sums costs up the parent chain on each request
- No special token encoding needed - just query the table
- Track token counts (input_tokens, output_tokens) as reported by OpenAI Responses API
- Dollar cost computed via view joining `llm_requests` with model pricing table

### Exec Tool Implementation

**Decision:** Reuse `mcp_infra/exec/subprocess.py:run_proc()` directly.

- Standalone async function with no MCP dependency
- Already handles: timeouts, output capping (150KB), stderr, UTF-8 safety
- Import and call directly from in-container agent loop:
  ```python
  from mcp_infra.exec.subprocess import run_proc
  outcome = await run_proc(["props", "critic-agent", "submit", ...], timeout_s=60.0)
  ```

### Events Table Deprecation

**Decision:** Big bang cutover. Remove events table entirely, replace with:

1. `llm_requests` table - LLM proxy logs full request/response payloads
2. `agent_runs.container_stdout/stderr` - container logs

Files affected documented in "Code Changes" section above.

### Grader Daemon pg_notify Permissions

**Decision:** No changes needed. Daemon uses admin credentials for LISTEN, not temp user credentials.

- Temp users (`agent_{uuid}`) only need write access to their tables
- pg_notify triggers run as postgres (SECURITY DEFINER)
- Daemon's `_start_listener()` uses admin DSN for the listener connection

### Log Capture Guarantee

**Decision:** Collect logs on container exit via aiodocker. Accept that hard crashes may lose final lines.

- Host uses `aiodocker` to read container logs after container exits
- Store in `agent_runs.container_stdout` and `agent_runs.container_stderr`
- Hard crashes (OOM, SIGKILL) may lose buffered output - acceptable tradeoff
- **Important for agent-authoring agents (PO/PI):** Container logs are only available after the agent exits, not during execution. Design workflows accordingly (e.g., don't expect to read subagent logs until after `props agent wait` returns).

## Open Questions

### 1. Interactive Agents (Future)

Current plan: exit 0 = done.

**Question:** How will interactive agents work later?

Defer for now. Options when needed:

- WebSocket/streaming for bidirectional communication
- Agent polls for user input from DB
- Separate interactive agent mode with different lifecycle
