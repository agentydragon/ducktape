---
name: test_props
description: Manual live props deployment testing — sets up Podman infrastructure (postgres, registry, backend) and runs real agent containers. NOT for standard Bazel tests (use `bazel test //props/...` for those).
allowed-tools: Bash, Read, Grep, Glob, Edit, Write, WebFetch, Task
---

# Test Props Live Deployment

Knowledge base for manually running the props critic and grader evaluation stack.
Covers infrastructure setup, database lifecycle, agent image push, running critics,
grading, and exporting results.

**Not for standard tests** — use `bazel test //props/...` for unit, integration, and
e2e Bazel tests. This skill is for manual live deployment and evaluation runs.

For evaluation workflow background:

- @props/docs/openai_evaluation/evaluation.md
- @props/docs/local_llm_evaluation/evaluation.md

## Environment Detection

Detect gVisor (Claude Code on the Web) by kernel version:

```bash
if [[ "$(uname -r)" == "4.4.0" ]]; then
  IS_GVISOR=true
else
  IS_GVISOR=false
fi
```

gVisor requires:

- `PROPS_DOCKER_NETWORK=host` (agent containers must use host networking)
- `--annotation run.oci.keep_original_groups=1` on all podman containers
- Verify `$DOCKER_HOST` is set (the session hook sets this)

## Infrastructure Setup

Start Postgres (port 5433) and the OCI registry (port 5050):

```bash
bash .claude/skills/test_props/start_infra.sh
```

Set PG environment variables for subsequent commands:

```bash
export PGHOST=127.0.0.1
export PGPORT=5433
export PGUSER=postgres
export PGPASSWORD=$(cat props/.devenv/state/pg_password)
export PGDATABASE=eval_results
```

## Database Lifecycle

### Fresh schema

`db recreate` drops all schema objects (tables, views, functions, policies), then
runs Alembic migrations to recreate the schema from scratch and syncs model
metadata. **Does not sync specimens.**

```bash
PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres \
PGPASSWORD=$(cat props/.devenv/state/pg_password) \
PGDATABASE=eval_results \
bazel run //props/cli:cli -- db recreate --yes
```

### Migrations and specimen sync via backend lifespan

When `auto_migrate = true` is set in the config file, the backend runs
`alembic upgrade head` on startup (idempotent — only applies pending migrations).

When `auto_sync_specimens = true` is set, the backend scans `/specimens` and syncs
all specimens on startup. Before starting the backend, symlink the repo's specimens
directory:

```bash
ln -sf "$(git rev-parse --show-toplevel)/props/specimens" /specimens
```

The `config.ollama.toml` in this skill directory enables both flags.

### Importing an existing dump

To continue from a saved dump (e.g., from a previous session):

1. Recreate schema (via `db recreate`).
2. Sync specimens — start backend with `auto_sync_specimens = true` (and the `/specimens`
   symlink in place), or run `bazel run //props/cli:cli -- db sync-specimen` per specimen.
3. Import the dump:

```bash
# OpenAI evaluation results:
zstd -dc props/docs/openai_evaluation/results.sql.zst \
  | psql --set ON_ERROR_STOP=on -d eval_results

# Local LLM (ollama) results:
zstd -dc props/docs/local_llm_evaluation/results.sql.zst \
  | psql --set ON_ERROR_STOP=on -d eval_results
```

The dump excludes ground-truth tables (snapshots, file_sets, true_positives, etc.)
that come from specimens. Specimens must be in the DB before importing.

## Backend Startup

### With remote Ollama (gpt-oss:20b)

Retrieve the Ollama API key from k8s:

```bash
export OLLAMA_API_KEY=$(kubectl get secret ollama-api-key -n claude-sandbox \
  -o jsonpath='{.data.api-key}' | base64 -d)
```

Symlink specimens and build:

```bash
ln -sf "$(git rev-parse --show-toplevel)/props/specimens" /specimens
bazel build //props/backend:backend_bin
```

Start in background:

```bash
PROPS_CONFIG_FILE=.claude/skills/test_props/config.ollama.toml \
PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres \
PGPASSWORD=$(cat props/.devenv/state/pg_password) \
PGDATABASE=eval_results \
PROPS_REGISTRY_UPSTREAM_URL=http://127.0.0.1:5050 \
PROPS_DOCKER_NETWORK=host \
DOCKER_HOST=$DOCKER_HOST \
OLLAMA_API_KEY=$OLLAMA_API_KEY \
bazel-bin/props/backend/backend_bin serve > /tmp/backend.log 2>&1 &
```

The backend logs the admin token on startup:

```bash
grep "Admin token" /tmp/backend.log
```

Check health: `curl -s http://127.0.0.1:8000/health`

### With OpenAI

Use `config.podman.toml` and set `OPENAI_API_KEY` + `OPENAI_BASE_URL`:

```bash
PROPS_CONFIG_FILE=.claude/skills/test_props/config.podman.toml \
OPENAI_API_KEY=$OPENAI_API_KEY \
OPENAI_BASE_URL=https://api.openai.com/v1 \
PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres \
PGPASSWORD=$(cat props/.devenv/state/pg_password) \
PGDATABASE=eval_results \
PROPS_REGISTRY_UPSTREAM_URL=http://127.0.0.1:5050 \
PROPS_DOCKER_NETWORK=host \
DOCKER_HOST=$DOCKER_HOST \
bazel-bin/props/backend/backend_bin serve > /tmp/backend.log 2>&1 &
```

## Push Agent Images

Push images to the **registry proxy** (port 8000), not the upstream registry
(port 5050). The proxy records agent definitions; the grader supervisor
listens for grader tag changes.

Set up Docker auth for the registry proxy:

```bash
PG_PASSWORD=$(cat props/.devenv/state/pg_password)
AUTH_B64=$(echo -n "postgres:$PG_PASSWORD" | base64)
mkdir -p ~/.docker
cat > ~/.docker/config.json <<EOF
{
  "auths": {
    "localhost:8000": { "auth": "$AUTH_B64" }
  }
}
EOF
```

Push both agent types:

```bash
bazel run //props/agents/critic:push
bazel run //props/agents/grader:push
```

These push to `localhost:8000/<type>:latest`. The grader supervisor starts grader
containers automatically when the grader image is pushed.

## Running Critics

Get the admin token and run a critic via the API. The call blocks until the
critic container exits.

```bash
PG_PASSWORD=$(cat props/.devenv/state/pg_password)
AUTH_TOKEN=$(echo -n "postgres:$PG_PASSWORD" | base64)

# File-set example: rank-1 example (33 TPs, 39 occurrences, fastest to run)
curl -s -X POST http://127.0.0.1:8000/api/runs/critic \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "definition_id": "latest",
    "example": {
      "kind": "file_set",
      "snapshot_slug": "ducktape/2025-09-03-00",
      "files_hash": "8e2209f20bd1df0c5bc4073dfff739fe"
    },
    "critic_model": "gpt-oss-20b",
    "timeout_seconds": 1800,
    "budget_usd": 0.0
  }'
```

Top file-set examples (file-set runs are faster than whole-snapshot):

| Rank | Snapshot                       | `files_hash`                       | TPs | Occurrences |
| ---- | ------------------------------ | ---------------------------------- | --- | ----------- |
| 1    | `ducktape/2025-09-03-00`       | `8e2209f20bd1df0c5bc4073dfff739fe` | 33  | 39          |
| 2    | `ducktape/2025-11-20-00`       | `bb8aff17944a6348a8089790457e3094` | 15  | 31          |
| 3    | `ducktape/2025-11-26-00`       | `6e416fb1d095abc7fdc79131434c7dac` | 20  | 21          |
| 4    | `ducktape/2025-11-21-00`       | `15702f4d16234db852e973e31323fbdd` | 21  | 21          |
| 5    | `gmail-archiver/2025-12-17-00` | `9e218584782810e5a65195da8f63931a` | 14  | 21          |

`budget_usd = 0.0` for Ollama (local cluster inference is free).

## Monitoring Runs

```bash
# All runs with status
psql -c "SELECT agent_run_id, type_config->>'agent_type' AS type, model, status,
         container_exit_code FROM agent_runs ORDER BY created_at"

# Reported issues for a specific critic run
psql -c "SELECT COUNT(*) FROM reported_issues WHERE agent_run_id = '<run_id>'"

# Grading edges (populated by grader after critic finishes)
psql -c "SELECT ge.critique_run_id, ge.critique_issue_id,
         ge.tp_id, ge.fp_id, ge.grade
         FROM grading_edges ge
         WHERE ge.critique_run_id = '<critic_run_id>'"

# Grading pending (count should reach 0 when grading is complete)
psql -c "SELECT COUNT(*) FROM grading_pending WHERE critique_run_id = '<critic_run_id>'"
```

## Grader Supervisor

The `GraderSupervisor` is enabled by `grader_model` in the config file. It starts
automatically when the backend starts and listens for `grader_definition_changed`
pg_notify events. When a grader image is pushed, it (re)starts grader containers
for all active snapshots.

Graders run continuously, watching for new critic runs to grade. After a critic
completes, the grader picks up the new reported issues and creates `grading_edges`.

If graders don't appear after pushing the image, check:

1. Was the image pushed to the proxy (port 8000), not directly to the registry (port 5050)?
2. Backend logs: `grep -i grader /tmp/backend.log`

## Exporting Results

Export run results (excluding ground-truth and infrastructure tables) for import
in a future session. The export excludes specimen data (snapshots, file_sets,
true_positives, etc.) since those come from specimens on import.

```bash
# For ollama/local LLM evaluation:
pg_dump eval_results \
  --data-only --no-owner --no-privileges \
  --exclude-table=true_positives \
  --exclude-table=true_positive_occurrences \
  --exclude-table=false_positives \
  --exclude-table=false_positive_occurrences \
  --exclude-table=fp_occurrence_relevant_files \
  --exclude-table=occurrence_ranges \
  --exclude-table=critic_scopes_expected_to_recall \
  --exclude-table=file_sets \
  --exclude-table=file_set_members \
  --exclude-table=snapshots \
  --exclude-table=snapshot_files \
  --exclude-table=model_metadata \
  --exclude-table=agent_role_salt \
  --exclude-table=alembic_version \
  -f props/docs/local_llm_evaluation/results.sql \
  && zstd --rm --ultra -22 props/docs/local_llm_evaluation/results.sql
```

`llm_requests` stores full conversation transcripts (O(N²) growth across turns).
zstd compresses cross-row redundancy far better than per-row gzip.

## Troubleshooting

### Image resolution errors

Add insecure registry entries to `~/.cache/claude-hooks/podman/registries.conf`:

```toml
[[registry]]
prefix = "127.0.0.1:5050"
location = "127.0.0.1:5050"
insecure = true

[[registry]]
prefix = "127.0.0.1:8000"
location = "127.0.0.1:8000"
insecure = true
```

### Password issues

Use hex-only passwords in `props/.devenv/state/pg_password` (no `/`, `+`, `=`
characters that break asyncpg DSN parsing).

### Ollama API key

Retrieve fresh from k8s if the current key doesn't work:

```bash
kubectl get secret ollama-api-key -n claude-sandbox \
  -o jsonpath='{.data.api-key}' | base64 -d
```

## Key Architecture Points

- **Registry proxy**: Integrated into the backend. Push images to port 8000
  (backend), which proxies to port 5050 (upstream registry) and records agent
  definitions.
- **Grader supervisor**: Listens for `grader_definition_changed` pg_notify.
  When a grader tag is pushed, all grader containers are (re)started.
- **Agent containers**: Run with host networking, per-agent PostgreSQL roles,
  and RLS-scoped database access.
- **Model config**: `config.ollama.toml` (this skill directory) configures
  `gpt-oss:20b` via the remote LiteLLM cluster at `litellm.allegedly.works`.
  The cluster serves the model with 131072-token context (`OLLAMA_NUM_CTX=131072`).
