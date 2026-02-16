---
name: test_props
description: Manual live props deployment testing — sets up Podman infrastructure (postgres, registry, backend) and runs real agent containers with a real OpenAI API key. NOT for standard Bazel tests (use `bazel test //props/...` for those).
argument-hint: "[workflow: setup|critic|grader|improver|all]"
allowed-tools: Bash, Read, Grep, Glob, Edit, Write, WebFetch, Task
---

# Test Props Live Deployment

Manual live deployment testing. Sets up Podman infrastructure, initializes the database,
runs the backend, pushes agent images, and tests agent workflows with real OpenAI calls.

**Not for standard tests** — use `bazel test //props/...` for unit, integration, and e2e
Bazel tests. This skill is for manual live deployment verification only.

**Argument:** `$ARGUMENTS` (default: `all`)

- `setup` - Only set up infrastructure, database, backend, and push images
- `critic` - Run a critic on a snapshot and verify output
- `grader` - Verify graders are running and grading
- `improver` - Test improver agent
- `all` - Full setup + test all workflows

## Background

For evaluation workflow docs (model selection, file-set examples, export/import):

- @props/docs/openai_evaluation/evaluation.md
- @props/docs/local_llm_evaluation/evaluation.md

## Prerequisites

- `OPENAI_API_KEY` must be in environment
- Podman must be running (claude_hooks handles this)

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
- `DOCKER_HOST` must be set (check `$DOCKER_HOST` env var)

## Phase 1: Infrastructure Setup

1. Check if podman containers `props-postgres` and `props-registry` are running:

   ```bash
   podman ps --format "{{.Names}}"
   ```

   If not running, start them:

   ```bash
   bash .claude/skills/test_props/start_infra_podman.sh
   ```

2. Set environment variables:

   ```bash
   export PGHOST=127.0.0.1
   export PGPORT=5433
   export PGUSER=postgres
   export PGPASSWORD=$(cat props/.devenv/state/pg_password)
   export PGDATABASE=eval_results
   export ADGN_PROPS_SPECIMENS_ROOT=$(git rev-parse --show-toplevel)/props/specimens
   ```

3. Initialize database. Use `db recreate` which drops and recreates the schema
   from scratch, then syncs all data:

   ```bash
   PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres \
   PGPASSWORD=$(cat props/.devenv/state/pg_password) \
   PGDATABASE=eval_results \
   ADGN_PROPS_SPECIMENS_ROOT=$(git rev-parse --show-toplevel)/props/specimens \
   bazel run //props/cli:cli -- db recreate --yes
   ```

## Phase 2: Start Backend

1. Check if backend is already running:

   ```bash
   curl -s http://127.0.0.1:8000/health
   ```

   If not running, build and start it in the background:

   ```bash
   bazel build //props/backend:backend_bin
   ```

   Then start with all required env vars:

   ```bash
   PROPS_CONFIG_FILE=.claude/skills/test_props/config.podman.toml \
   PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres \
   PGPASSWORD=$(cat props/.devenv/state/pg_password) \
   PGDATABASE=eval_results \
   ADGN_PROPS_SPECIMENS_ROOT=$(git rev-parse --show-toplevel)/props/specimens \
   PROPS_REGISTRY_UPSTREAM_URL=http://127.0.0.1:5050 \
   PROPS_DOCKER_NETWORK=host \
   DOCKER_HOST=$DOCKER_HOST \
   bazel-bin/props/backend/backend_bin serve > /tmp/backend.log 2>&1 &
   ```

   **Note**: `PROPS_DOCKER_NETWORK=host` is required on gVisor (Claude Code on
   the Web) because the default Docker bridge network (`props-agents`) doesn't
   work under gVisor's netavark. On native environments, `host` also works fine.

## Phase 3: Push Agent Images

Push images to the **registry proxy** (port 8000), not the direct registry
(port 5050). The proxy records agent definitions and the grader supervisor
listens for grader tag changes.

First, set up Docker auth for the registry proxy:

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

Then push each agent type using Bazel `oci_push` targets:

```bash
bazel run //props/agents/critic:push
bazel run //props/agents/grader:push
```

These push to `localhost:8000/<type>:latest` using credentials from
`~/.docker/config.json`.

## Phase 4: Test Workflows

### Critic

First, find the critic image digest:

```sql
SELECT digest FROM agent_definitions WHERE agent_type = 'critic';
```

Run a critic on a snapshot via the API:

```bash
PG_PASSWORD=$(cat props/.devenv/state/pg_password)
AUTH_TOKEN=$(echo -n "postgres:$PG_PASSWORD" | base64)
CRITIC_DIGEST="<digest from above>"

curl -s -X POST http://127.0.0.1:8000/api/runs/critic \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"definition_id\": \"$CRITIC_DIGEST\",
    \"example\": {\"kind\": \"whole_snapshot\", \"snapshot_slug\": \"<slug>\"},
    \"critic_model\": \"gpt-5-mini\",
    \"timeout_seconds\": 300,
    \"budget_usd\": 5.0
  }"
```

**Note**: This call blocks until the critic container exits.

**Verify critic completion:**

1. Poll until the critic run completes:

   ```sql
   SELECT agent_run_id, status FROM agent_runs
   WHERE agent_run_id = '<run_id>';
   ```

   Wait until `status = 'exited'`.

2. Check that `reported_issues` has findings:

   ```sql
   SELECT COUNT(*) FROM reported_issues
   WHERE agent_run_id = '<run_id>';
   ```

   There should be at least one reported issue.

3. Issues should have valid file paths and line ranges:

   ```sql
   SELECT ri.issue_id, ri.title, rio.locations
   FROM reported_issues ri
   JOIN reported_issue_occurrences rio
     ON rio.agent_run_id = ri.agent_run_id AND rio.reported_issue_id = ri.issue_id
   WHERE ri.agent_run_id = '<run_id>';
   ```

### Grader

Graders start automatically when the grader image is pushed. Verify:

```sql
SELECT agent_run_id, status, type_config->>'snapshot_slug' as snapshot
FROM agent_runs WHERE type_config->>'agent_type' = 'grader';
```

There should be one grader per snapshot, all `in_progress`.

**Verify grading after critic:**

Once the critic run completes and graders are running, verify that grading
happens — the grader should create `grading_edges` for the critic's issues.

1. Check `grading_pending` for drift (missing grading edges):

   ```sql
   SELECT COUNT(*) FROM grading_pending
   WHERE critique_run_id = '<critic_run_id>';
   ```

2. Poll until the count reaches 0. This means all grading edges have been
   created — every reported issue has been compared against every relevant
   ground truth occurrence.

3. Verify `grading_edges` exist:

   ```sql
   SELECT ge.critique_run_id, ge.critique_issue_id,
          ge.tp_id, ge.fp_id, ge.grade
   FROM grading_edges ge
   WHERE ge.critique_run_id = '<critic_run_id>';
   ```

   There should be at least one grading edge per reported issue.

### Improver

Start an improver run and verify it proposes critic modifications:

```bash
curl -X POST http://127.0.0.1:8000/api/runs/start \
  -H "Authorization: Basic $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_type": "improver",
    "model": "gpt-5-mini",
    "example": {"type": "whole_snapshot", "snapshot_slug": "<slug>"}
  }'
```

## Troubleshooting

### Graders not starting

The grader supervisor defers spawning until the HTTP backend is ready. If
graders don't appear:

1. Ensure the grader image was pushed to the **proxy** (port 8000)
2. Check backend logs for `Grader definition changed` / `Starting graders`
3. Restart the backend if needed

### Image resolution errors

Add insecure registry entries to
`~/.cache/claude-hooks/podman/registries.conf`:

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

## Key Architecture Points

- **Registry proxy**: Integrated into the backend. Push images to port 8000
  (backend), which proxies to port 5050 (upstream registry) and records
  agent definitions.
- **Grader supervisor**: Listens for `grader_definition_changed` pg_notify.
  When a grader tag is pushed, all grader containers are (re)started.
- **Agent containers**: Run with host networking, per-agent PostgreSQL roles,
  and RLS-scoped database access.
- **Model selection**: Use at least gpt-5 level models for meaningful results.
  Config file: `.claude/skills/test_props/config.podman.toml`.
