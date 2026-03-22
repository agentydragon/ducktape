# Props Ecosystem

High-level architecture and shared infrastructure for the props evaluation system.

## Directory Structure

```
props/
├── .envrc                    # Devenv entry point for env vars
├── compose.yaml              # Docker Compose for postgres, registry, backend
├── core/                     # Core types and models
│   ├── agent_types.py        # Agent type definitions and TypeConfig union
│   ├── eval_api_models.py    # Request/response models for eval API
│   ├── models/               # Data models (examples, true_positive, etc.)
│   └── gepa/                 # GEPA prompt optimization
├── cli/                      # Command-line interface
│   └── cmd_*.py              # Subcommand modules (db, snapshot, etc.)
├── db/                       # Database layer
│   ├── models.py             # SQLAlchemy ORM models
│   ├── migrations/           # Alembic migration (single complete_schema)
│   └── sync/                 # Specimen sync from filesystem to DB
├── orchestration/            # Agent execution
│   ├── agent_registry.py     # Container lifecycle, image pull, role creation
│   └── agent_credentials.py  # Per-agent PostgreSQL role management
├── backend/                  # Unified FastAPI server
│   ├── app.py                # FastAPI app with lifespan
│   ├── auth.py               # Auth middleware (postgres creds → RLS)
│   └── routes/               # API endpoints
│       ├── stats.py          # Dashboard stats API
│       ├── runs.py           # Agent runs + validation API
│       ├── ground_truth.py   # Snapshot/issue browsing API
│       ├── llm.py            # LLM proxy (OpenAI-compatible)
│       └── registry.py       # OCI registry proxy
├── frontend/                 # Svelte dashboard UI
├── agents/                   # Agent implementations
│   ├── critic/               # Critic agent (finds issues in code)
│   ├── grader/               # Grader agent (matches issues to ground truth)
│   └── critic_dev/           # Meta-agents that develop critics
│       ├── improve/          # Creates improved critic definitions
│       └── optimize/         # Runs eval loops to select best critics
├── specimens/                # Frozen code snapshots with labeled issues
├── standards/                # Property definitions (issue taxonomies)
├── testing/                  # Shared test fixtures and utilities
├── docs/                     # Documentation
└── prompts/                  # Mako prompt templates
```

## Initial Setup

```bash
cd props

# 1. Allow direnv (generates PGPASSWORD, sets env vars)
direnv allow

# 2. Build and load backend image (includes frontend)
bazelisk run //props/backend:load

# 3. Start infrastructure (postgres, registry, backend+frontend)
docker compose up -d

# 4. Initialize database (runs migrations, syncs specimens)
# The backend will fail until this completes (missing tables).
bazelisk run //props/cli -- db recreate

# 5. Restart backend (picks up the now-initialized database)
docker compose restart backend

# 6. Log in to registry proxy (uses Postgres admin creds, stored in Docker config)
echo "$PGPASSWORD" | docker login localhost:8000 -u "$PGUSER" --password-stdin

# 7. Push agent images to registry
bazelisk run //props/agents/critic:push
bazelisk run //props/agents/grader:push
bazelisk run //props/agents/critic_dev/improve:push
bazelisk run //props/agents/critic_dev/optimize:push
```

## Development

```bash
docker compose up -d            # Start infrastructure
docker compose down             # Stop infrastructure
docker compose logs -f postgres # View logs
```

For frontend hot-reload during development, use `frontend:dev` instead of the
compose backend (starts its own backend + esbuild watch):

```bash
docker compose up -d postgres registry     # Infra only (no backend)
bazelisk run //props/frontend:dev          # Frontend :5173 + backend :8000
```

### Service URLs

- Dashboard: <http://localhost:8000> (frontend + API, same origin)
- Backend API: <http://localhost:8000/api/> — see <docs/backend_api.md> for endpoint reference
- PostgreSQL: localhost:5433
- Registry: localhost:5000 (direct, for debugging)

## Database Management

```bash
# psql access (uses PG* environment variables from devenv)
psql

# Recreate database from scratch (drops all data, runs migrations, syncs specimens)
bazelisk run //props/cli -- db recreate

# Backup and restore
bazelisk run //props/cli -- db backup
bazelisk run //props/cli -- db restore <backup_file>
```

## Specimens Dataset

Specimens live in `props/specimens/` (previously a [separate repository](https://github.com/agentydragon/specimens)).

Specimens are frozen code states with labeled issues (true positives and false positives) used for training and evaluating the LLM critic. The dataset includes:

- Per-snapshot directories with `BUILD.bazel` (slug, split via `specimen_targets()`) and issue files (`.yaml`)
- Each snapshot's `BUILD.bazel` defines the code source and train/valid/test split

The `ADGN_PROPS_SPECIMENS_ROOT` environment variable is set automatically by direnv (`.envrc`) to
`props/specimens/`. See <specimens/docs/authoring_guide.md> for the format spec.

## Evaluation Workflow

1. **Specimens** — frozen code snapshots with human-labeled canonical issues
2. **Critiques** — agent-generated issue reports per specimen
3. **Grading** — comparison against canonical issues (recall/precision)
4. **Selection** — agents ranked by fitness scores

Grading is automatic via snapshot graders. See <docs/training_strategy.md> for details.

## Cluster Deployment

Props runs in the Talos k8s cluster via a Helm chart (`props/helm/`), deployed by Flux from `cluster/k8s/props/helmrelease.yaml`.

### Components

| Component  | Image/Chart                             | Storage          |
| ---------- | --------------------------------------- | ---------------- |
| Backend    | `ghcr.io/agentydragon/props-backend`    | —                |
| PostgreSQL | Bitnami subchart                        | 10Gi proxmox-csi |
| Registry   | `registry:2` (OCI proxy behind backend) | 50Gi proxmox-csi |
| Backup     | CronJob using Bitnami PG image          | 5Gi proxmox-csi  |

All pods run on Proxmox nodes (`nodeSelector: topology.kubernetes.io/region: proxmox`).

### OCI Image

The backend OCI image (`//props/backend:image`) bundles:

- Python backend + frontend assets
- All specimen artifacts (code tars + data YAMLs) at `/specimens/{slug}/`

Built by Bazel, pushed to GHCR by CI.

### Startup Behavior

Controlled by `PropsConfig` toggles in `config.toml` (mounted via ConfigMap):

- `auto_migrate: true` — runs Alembic migrations on boot
- `auto_sync_specimens: true` — scans `/specimens/` and syncs all specimens to PostgreSQL

### PostgreSQL Backup

A daily CronJob (`backup.enabled: true`) runs `pg_dump | gzip` to a PVC, with configurable retention (`backup.retention.days`). Manual trigger:

```bash
kubectl create job --from=cronjob/props-backup props-backup-test -n props
```

### Registry Proxy

The backend authenticates OCI requests against PostgreSQL and proxies `/v2/*` to the internal `registry:2`. CI pushes agent images via `docker login props.allegedly.works`. The registry is configured with `REGISTRY_HTTP_RELATIVEURLS=true` so blob upload `Location` headers use relative URLs (required for external clients going through the Gateway API proxy).

## GitHub Copilot Agent Setup

See `.github/workflows/copilot-setup-steps.yml` for automated environment setup (env vars, network config). Uses `props-agents` Docker network (no HTTP proxy, unlike Claude hooks which use `host` network).
