# Running Props Evaluation with the OpenAI API

End-to-end procedure for running the props critic and grader against committed
specimen snapshots using the OpenAI Responses API (e.g., `gpt-5-mini`).

See <../local_llm_evaluation/evaluation.md> for the local LLM variant of this
workflow.

## Prerequisites

- Claude Code web session with `claude_hooks` (provides podman, `/dev/shm`,
  insecure-registry entries, `DOCKER_HOST` env var)
- `OPENAI_API_KEY` environment variable set with a valid OpenAI key
- ~2 GiB free RAM for PostgreSQL, registry, and backend containers

## Quick Reference

| Component  | Address          | Notes                              |
| ---------- | ---------------- | ---------------------------------- |
| PostgreSQL | `127.0.0.1:5432` | Database: `eval_results`           |
| Registry   | `127.0.0.1:5000` | Upstream OCI registry              |
| Backend    | `127.0.0.1:8000` | API + registry proxy + LLM proxy   |
| OpenAI API | `api.openai.com` | Default upstream for OpenAI models |

## Step 1: Configure Insecure Registries

Add insecure registry entries so podman can pull images from the backend
proxy over HTTP:

```bash
cat >> /etc/containers/registries.conf <<'EOF'

[[registry]]
location = "localhost:5000"
insecure = true

[[registry]]
location = "127.0.0.1:5000"
insecure = true

[[registry]]
location = "localhost:8000"
insecure = true

[[registry]]
location = "127.0.0.1:8000"
insecure = true
EOF
```

## Step 2: Start PostgreSQL

```bash
mkdir -p /dev/shm/pgdata

TMPDIR=/dev/shm podman run -d --name postgres --network=host \
  -e POSTGRES_PASSWORD=props-bench-dcfc0ef9506c6673 \
  -e POSTGRES_DB=eval_results \
  -v /dev/shm/pgdata:/var/lib/postgresql/data \
  docker.io/library/postgres:16
```

Wait for readiness:

```bash
for i in $(seq 1 10); do
  sleep 1
  pg_isready -h 127.0.0.1 -U postgres 2>/dev/null && break
done
```

## Step 3: Initialize Props Database

```bash
PGHOST=127.0.0.1 \
PGPORT=5432 \
PGUSER=postgres \
PGPASSWORD="props-bench-dcfc0ef9506c6673" \
PGDATABASE=eval_results \
ADGN_PROPS_SPECIMENS_ROOT="$PWD/props/specimens" \
  bazel run //props/cli -- db recreate --yes
```

## Step 4: Start OCI Registry

```bash
TMPDIR=/dev/shm podman run -d --name registry --network=host \
  docker.io/library/registry:2
```

Verify: `curl http://localhost:5000/v2/_catalog`

## Step 5: Create Props Config File

```bash
cat > /tmp/props-openai-config.toml <<'EOF'
backend_url = "http://127.0.0.1:8000"
grader_model = "gpt-5-mini"

[agent_env]
PGHOST = "127.0.0.1"
PGPORT = "5432"
PGDATABASE = "eval_results"
EOF
```

No `[upstreams.*]` or `[[models]]` sections are needed — OpenAI models like
`gpt-5-mini` are already in `model_metadata.yaml` with `upstream_name=NULL`,
which routes to the default OpenAI upstream automatically.

Setting `grader_model` enables the `GraderSupervisor`, which automatically
runs a grader after each critic finishes.

## Step 6: Start the Props Backend

```bash
PROPS_CONFIG_FILE=/tmp/props-openai-config.toml \
OPENAI_API_KEY="$OPENAI_API_KEY" \
OPENAI_BASE_URL="https://api.openai.com" \
PGHOST=127.0.0.1 \
PGPORT=5432 \
PGUSER=postgres \
PGPASSWORD="props-bench-dcfc0ef9506c6673" \
PGDATABASE=eval_results \
ADGN_PROPS_SPECIMENS_ROOT="$PWD/props/specimens" \
DOCKER_HOST="unix:///tmp/claude-podman-*.sock" \
PROPS_DOCKER_NETWORK=host \
PROPS_REGISTRY_UPSTREAM_URL=http://127.0.0.1:5000 \
TMPDIR=/dev/shm \
  bazel run //props/backend:backend_cli -- serve --host 127.0.0.1 --port 8000 &
```

**Critical**: `OPENAI_BASE_URL` must be `https://api.openai.com` (without
`/v1`). The proxy appends `/v1/responses` itself. Using the OpenAI SDK default
(`https://api.openai.com/v1`) would produce a double `/v1/v1/responses` path.

Expand the `DOCKER_HOST` glob to the actual podman socket path (check with
`ls /tmp/claude-podman-*.sock`).

The backend logs an admin token on startup:

```
INFO props.backend.app: Admin token: cG9zdGdyZXM6cHJvcHMt...
```

This base64-encoded `username:password` string is used for all authenticated
API calls and registry pushes.

## Step 7: Pre-pull Agent Images with Podman

The backend uses `aiodocker` to ask podman to pull images. In gVisor
environments, podman's socket server may not honor `registries.conf` changes
made after startup. Pre-pull the images with the podman CLI instead:

```bash
TMPDIR=/dev/shm podman pull --tls-verify=false \
  --creds="postgres:props-bench-dcfc0ef9506c6673" \
  127.0.0.1:8000/critic:latest

TMPDIR=/dev/shm podman pull --tls-verify=false \
  --creds="postgres:props-bench-dcfc0ef9506c6673" \
  127.0.0.1:8000/grader:latest
```

This step is only needed **after** the images have been pushed (Step 8). If
you push images before starting the backend, you can pre-pull before the
backend starts.

## Step 8: Build and Push Agent Images

Push through the backend's registry proxy (port 8000), which records
`agent_definitions` in the DB:

```bash
export TMPDIR=/dev/shm
ADMIN_TOKEN="<from backend startup logs>"

mkdir -p /tmp/crane-config
echo "{\"auths\":{\"localhost:8000\":{\"auth\":\"$ADMIN_TOKEN\"}}}" \
  > /tmp/crane-config/config.json

# Push critic image
DOCKER_CONFIG=/tmp/crane-config \
  bazel run //props/agents/critic:push -- \
    --repository localhost:8000/critic --tag latest --insecure

# Push grader image
DOCKER_CONFIG=/tmp/crane-config \
  bazel run //props/agents/grader:push -- \
    --repository localhost:8000/grader --tag latest --insecure
```

Then pre-pull as described in Step 7.

## Step 9: Run a Critic

```bash
ADMIN_TOKEN="<from backend logs>"

curl -s -X POST 'http://localhost:8000/api/runs/critic' \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "definition_id": "latest",
    "example": {"kind": "whole_snapshot", "snapshot_slug": "wt/2025-01-03-00"},
    "critic_model": "gpt-5-mini",
    "timeout_seconds": 1800,
    "budget_usd": 5.0
  }'
```

The response includes the `critic_run_id` and final `status`/`container_exit_code`.

## Step 10: Wait for Grading

The `GraderSupervisor` automatically starts per-snapshot graders on backend
startup. After a critic finishes, the grader for that snapshot wakes up (via
`pg_notify`) and grades the critic's output.

Monitor progress:

```bash
PGPASSWORD="props-bench-dcfc0ef9506c6673" psql -h 127.0.0.1 -U postgres -d eval_results \
  -c "SELECT agent_run_id, type_config->>'agent_type' AS type, model, status,
             container_exit_code FROM agent_runs ORDER BY created_at"
```

Wait until both critic and grader runs show `status = 'exited'` with
`container_exit_code = 0`.

**If the grader for your snapshot didn't start**: The grader supervisor starts
graders during backend startup. If the backend started before images were
pushed, the initial grader spawn fails and won't be retried. Restart the
backend after pushing images.

## Step 11: Export Results

Export run results (excluding ground truth tables) for another agent to import:

```bash
PGPASSWORD="props-bench-dcfc0ef9506c6673" pg_dump -h 127.0.0.1 -U postgres eval_results \
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
  > props/docs/openai_evaluation/results.sql
```

This exports: `agent_definitions`, `agent_runs`, `reported_issues`,
`reported_issue_occurrences`, `grading_edges`, `issue_clusters`,
`issue_cluster_members`, `llm_requests`.

## Importing Results in Another Session

To continue from an exported state:

```bash
# 1. Start PostgreSQL (Step 2)
# 2. Initialize database with ground truth (Step 3 — db recreate)
# 3. Import the exported results:
PGPASSWORD="props-bench-dcfc0ef9506c6673" psql -h 127.0.0.1 -U postgres -d eval_results \
  < props/docs/openai_evaluation/results.sql
```

The import loads `agent_definitions`, `agent_runs`, `reported_issues`, etc.
on top of the freshly synced ground truth. The importing agent can then:

- Query results directly via SQL
- Start the backend and run additional critics/graders
- Use the dashboard at `http://localhost:8000` to visualize metrics

## Troubleshooting

### HTTP 404 from OpenAI API

The LLM proxy at `/v1/responses` forwards to `{OPENAI_BASE_URL}/v1/responses`.
If `OPENAI_BASE_URL` includes `/v1` (e.g., `https://api.openai.com/v1`), the
resulting URL is `https://api.openai.com/v1/v1/responses` — a 404. Set
`OPENAI_BASE_URL=https://api.openai.com` (no `/v1`).

### `http: server gave HTTP response to HTTPS client`

Podman defaults to HTTPS for registry pulls. Either:

- Add insecure registry entries to `/etc/containers/registries.conf` (Step 1)
- Pre-pull images with `podman pull --tls-verify=false` (Step 7)

### Grader not starting for a snapshot

The grader supervisor starts all graders on backend startup. If images aren't
pushed yet, the initial spawn fails. Solution: push images first, then restart
the backend.

### Agent container can't reach services

Ensure `PROPS_DOCKER_NETWORK=host` is set so containers share the host network.

### `current_agent_run_id() returned NULL`

The agent container is connecting to the DB with the wrong user. The backend
creates per-run agent roles (e.g., `agent_{uuid}`) automatically. This error
indicates the container is using the `postgres` superuser instead — check that
the backend properly set the `PGUSER`/`PGPASSWORD` env vars.
