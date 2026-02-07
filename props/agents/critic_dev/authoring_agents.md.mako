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

# View image config (CMD, ENV)
crane config $REGISTRY/critic:latest --insecure | python3 -m json.tool

# List all files
crane export $REGISTRY/critic:latest - --insecure | tar t

# Extract a specific file
crane export $REGISTRY/critic:latest - --insecure | tar xf - -O \
  app/critic.runfiles/_main/props/agents/critic/main.py
```

### Modify and push

Agents can only push by digest, not by tag. Use a local staging path, then push the final digest:

```bash
# Prepare your changes as a tar layer
mkdir -p /tmp/layer
cp /workspace/my_custom_main.py /tmp/layer/custom_main.py
tar -cf /tmp/layer.tar -C /tmp/layer .

# Append layer to a local staging path
PYTHON=/app/critic.runfiles/_main/props/agents/critic/_critic.venv/bin/python3
crane append -b $REGISTRY/critic:latest -f /tmp/layer.tar \
  -o /tmp/image.tar --insecure

# Mutate CMD and push; crane push outputs the digest
crane mutate --local /tmp/image.tar \
  --cmd "$PYTHON" --cmd "/custom_main.py" \
  -o /tmp/image-final.tar
DIGEST=$(crane push /tmp/image-final.tar $REGISTRY/critic --insecure)
echo "Created image: critic@$DIGEST"
```

Use this digest as `definition_id` when calling `run_critic`.

${include_doc("props/agents/critic_dev/built_in_critic.md.mako")}

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

### OpenAPI Introspection

The backend is a FastAPI app. Discover all endpoints and request/response schemas:

```bash
curl -s $PROPS_BACKEND_URL/openapi.json | python3 -m json.tool
```

### Runs API

Use the `run_critic` and `wait_until_graded` tools provided to you, or the bundled `CriticRunClient`:

```python
from props.agents.critic_dev.eval_client import CriticRunClient

async with CriticRunClient.from_env() as client:
    result = await client.run_critic(definition_id="sha256:...", ...)
    # Wait for grading (polls DB directly, not via REST API)
    from props.agents.critic_dev.grading import wait_until_graded
    grading = await wait_until_graded(result.critic_run_id, db)
```

The backend serves as your OCI registry — use `PROPS_BACKEND_URL` as the registry host for all `crane` and `/v2/` operations.

## Best Practices

1. **Fail fast** — exit non-zero if prerequisites aren't met (missing files, DB connection fails, etc.)
2. **Use OCI layering** — reuse base images, add small layers for changes (more efficient than copying entire images)
3. **Push by digest** — immutable content addressing prevents conflicts (agents can only push by digest, not by tag)
4. **Experiment freely** — try different architectures (multi-agent, pipeline, hybrid) and measure which scores best
