# Agent Image Authoring Guide

## What is a Critic?

A critic is **any OCI container that writes critique data to the database**. That's the only hard contract.

A critic container receives database credentials and a snapshot to review. It writes issues and occurrences to the `reported_issues` and `reported_issue_occurrences` tables, then exits. How it does that is entirely up to you.

**You are not limited to a simple agent loop.** A critic can be:

- A single LLM agent with custom tools and prompts
- A multi-stage pipeline (static analysis → LLM review → validation)
- An orchestrator that spawns and coordinates multiple LLM sub-agents for different subtasks
- A hybrid system combining linters, AST analysis, pattern matching, and LLM judgment
- Any combination — whatever produces the best results on the target metric

**Use all affordances available:** bundle additional tools and linters, run your own experiments, orchestrate complex pipelines, add pre/post-processing stages. The goal is a critic that scores well, not a critic that follows a particular architecture.

The built-in critic images use a basic single-agent loop as a starting point. You can modify them, replace their logic entirely, or build from scratch.

## Runtime Environment

The orchestrator injects these environment variables into every agent container:

| Variable | Purpose |
|----------|---------|
| `PGHOST`, `PGPORT`, `PGDATABASE` | PostgreSQL connection |
| `PGUSER` | Per-run PostgreSQL role (RLS scoping) |
| `PGPASSWORD` | Deterministic password for the role |
| `OPENAI_BASE_URL` | LLM proxy (OpenAI-compatible) |
| `OPENAI_API_KEY` | Auth token — base64-encoded `PGUSER:PGPASSWORD` |
| `PROPS_BACKEND_URL` | Backend HTTP API |

**Database credentials** are deterministic — the same `agent_run_id` always produces the same username and password, allowing reconnection.

The backend's LLM proxy at `OPENAI_BASE_URL` authenticates with `OPENAI_API_KEY` and logs all LLM requests to the `llm_requests` table.

**Access by agent type:**

| Capability | Critic | Grader | Critic developer (you) |
|------------|--------|--------|------------------------|
| PostgreSQL (RLS-scoped) | Yes | Yes | Yes |
| LLM proxy (`/v1/responses`) | Yes | Yes | Yes |
| Runs API (`/api/runs/critic`) | No | No | Yes |
| Registry proxy (`/v2/*`) | No | No | Yes |

## Available Base Images

The system provides these built-in images in the registry:

- `critic:latest` — default critic
- `critic:high_recall` — high recall variant
- `critic:contract_truthfulness` — contract checker
- `critic:dead_code` — dead code detector
- `critic:flag_propagation` — flag propagation analyzer
- `critic:verbose_docs` — verbose documentation detector
- `grader:latest` — grader agent

Pull these as starting points, inspect their internals, and repackage with modifications.

## Creating Custom Images

`crane` is pre-installed in your container with registry credentials already configured.

### Inspect a base image

```bash
REGISTRY=$(echo $PROPS_BACKEND_URL | sed 's|https\?://||')

# View image config (entrypoint, ENV)
crane config $REGISTRY/critic:latest --insecure | python3 -m json.tool

# List all files
crane export $REGISTRY/critic:latest - --insecure | tar t

# Extract a specific file
crane export $REGISTRY/critic:latest - --insecure | tar xf - -O \
  props/agents/critic/critic_bin.runfiles/_main/props/agents/critic/main.py
```

**Understanding the default critic:** To see what the built-in critic does and what its system prompt says, extract the key files:

```bash
RUNFILES=props/agents/critic/critic_bin.runfiles/_main

# Read the critic's entry point (agent loop, tool registration)
crane export $REGISTRY/critic:latest - --insecure | tar xf - -O $RUNFILES/props/agents/critic/main.py

# Read the critic's system prompt template
crane export $REGISTRY/critic:latest - --insecure | tar xf - -O $RUNFILES/props/agents/critic/prompt.md.mako
```

This shows exactly what instructions the critic receives, what tools it has, and how it processes snapshots. Use this to understand what to change.

### Modify and push

The simplest approach: overlay `main.py` at the runfiles path. The existing entrypoint launcher will run your code instead of the original. Agents push by digest, not by tag.

```bash
# Write your custom main.py at the runfiles path
MAIN_PY=props/agents/critic/critic_bin.runfiles/_main/props/agents/critic/main.py
mkdir -p /tmp/layer/$(dirname $MAIN_PY)
cp /workspace/my_custom_main.py /tmp/layer/$MAIN_PY

# Create a tar layer that shadows the original main.py
tar -cf /tmp/layer.tar -C /tmp/layer .

# Append layer to the base image (no entrypoint change needed)
# Set org.opencontainers.image.title to a short, human-readable name for your variant.
# This becomes the display name shown in the UI — use something descriptive like
# "chain-of-thought-critic" or "ast-analysis-v2" (keep it under 40 chars).
crane mutate $REGISTRY/critic:latest \
  --append /tmp/layer.tar \
  --label org.opencontainers.image.title="my-variant-name" \
  -o /tmp/image.tar \
  --insecure

# Compute digest and push
DIGEST=$(crane digest --tarball /tmp/image.tar)
crane push /tmp/image.tar $REGISTRY/critic@$DIGEST --insecure
echo "Created image: critic@$DIGEST"
```

Use this digest as `definition_id` when calling `run_critic`.

${include_doc("props/agents/critic_dev/built_in_critic.md.mako")}

## Running and Evaluating Custom Critics

After pushing a custom image, run and evaluate it:

```
1. Build image:  crane mutate ... --append ... --label org.opencontainers.image.title="my-name" -o /tmp/image.tar --insecure
2. Get digest:   DIGEST=$(crane digest --tarball /tmp/image.tar)
3. Push:         crane push /tmp/image.tar $REGISTRY/critic@$DIGEST --insecure
4. Run:          run_critic(definition_id=$DIGEST, example=..., timeout_seconds=120, budget_usd=0.5)
5. Grade:        wait_until_graded_tool(critic_run_id=..., timeout_seconds=300)
6. Check recall: SELECT * FROM recall_by_definition_split_kind WHERE critic_image_digest = '$DIGEST';
```

`run_critic` **blocks until the critic container exits** (typically minutes per call). `wait_until_graded_tool` polls the database until grading is complete.

Read `props.agents.critic_dev.loop` to see exact tool argument types and return types. Read `props.agents.critic_dev.eval_client` for the underlying REST API if you want to call it directly from Python (e.g., to run multiple critics in parallel).

### Constructing examples

Query the `examples` table to find available examples:

```sql
SELECT snapshot_slug, example_kind, files_hash, n_recall_denominator
FROM examples WHERE split = 'train' ORDER BY n_recall_denominator;
```

Read `props.core.models.examples` for the `ExampleSpec` discriminated union (whole_snapshot vs file_set).

## Backend API

The unified backend at `PROPS_BACKEND_URL` serves all functionality:

| Path | Description | Your access |
|------|-------------|-------------|
| `POST /api/runs/critic` | Run a critic agent on an example | Yes |
| `/v2/*` | Registry proxy (OCI API) | Yes |
| `/v1/responses` | LLM proxy (OpenAI API) | Yes |
| `/api/stats/*` | Dashboard stats | No (admin) |
| `/api/runs/*` | Agent run management | No (admin) |
| `/api/gt/*` | Ground truth management | No (admin) |

The backend serves as your OCI registry — use `PROPS_BACKEND_URL` as the registry host for all `crane` and `/v2/` operations.

### OpenAPI Introspection

The backend is a FastAPI app. Discover all endpoints and request/response schemas:

```bash
curl -s $PROPS_BACKEND_URL/openapi.json | python3 -m json.tool
```

## Best Practices

1. **Fail fast** — exit non-zero if prerequisites aren't met (missing files, DB connection fails, etc.)
2. **Use OCI layering** — reuse base images, add small layers for changes (more efficient than copying entire images)
3. **Push by digest** — immutable content addressing prevents conflicts (agents can only push by digest, not by tag)
4. **Start with file-set examples** — they're smaller, faster, and easier to debug than whole-snapshot
5. **Experiment freely** — try different architectures (multi-agent, pipeline, hybrid) and measure which scores best
