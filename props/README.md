# Props Ecosystem

Evaluation system for LLM code critics: critic agents report issues in frozen code
**specimens**, grader agents match the reports against human-labeled canonical issues
(recall/precision), and agents are ranked by fitness. Details: <docs/training_strategy.md>.

## Layout

- `core/`: agent types (`TypeConfig` union), eval API models, data models
- `cli/`: `props` CLI (`db` subcommands)
- `db/`: ORM models, Alembic migrations, specimen sync
- `orchestration/`: container lifecycle, image pull, per-agent Postgres roles
- `backend/`: FastAPI server — auth (Postgres creds → RLS), stats, runs, ground truth, LLM and
  OCI registry proxies
- `frontend/`: React + Mantine dashboard
- `agents/`: in-container code — `critic/`, `grader/`, `critic_dev/{improve,optimize}`
- `specimens/`: frozen code snapshots with labeled issues
- `standards/`: property definitions (issue taxonomies)
- `prompts/`: Mako prompt templates
- `testing/`: shared test fixtures

## Setup

```bash
cd props
direnv allow                                     # generates PGPASSWORD, sets env
bazelisk run //props/backend:load                # backend (+ frontend) image
bazelisk run //props/llm_proxy:load
docker compose up -d                             # postgres, registry, backend, llm-proxy
bazelisk run //props/cli -- db recreate          # migrations + specimen sync; backend fails until done
docker compose restart backend
echo "$PGPASSWORD" | docker login localhost:8000 -u "$PGUSER" --password-stdin   # registry proxy
for a in critic grader critic_dev/improve critic_dev/optimize; do bazelisk run //props/agents/$a:push; done
```

Frontend hot reload: `docker compose up -d postgres registry`, then
`bazelisk run //props/frontend:dev` (frontend `:5173`, backend `:8000`).

Dashboard and API: <http://localhost:8000> (API reference: <docs/backend_api.md>). Postgres:
`localhost:5433` (`psql` uses the devenv `PG*` vars). Registry, direct for debugging:
`localhost:5000`.

Database: `bazelisk run //props/cli -- db {recreate,backup,restore <file>}`; `recreate` drops all
data.

## Specimens

`props/specimens/` (formerly a [separate repository](https://github.com/agentydragon/specimens)):
per-snapshot directories whose `BUILD.bazel` (`specimen_targets()`) sets slug, code source and
train/valid/test split, plus issue YAMLs (true and false positives). Direnv sets
`ADGN_PROPS_SPECIMENS_ROOT`. Format: <specimens/docs/authoring_guide.md>.

## Cluster deployment

`props/deploy/` is the one Kustomize package (namespace, CNPG database, secrets, RBAC, app). It
is parked: Flux registers it via `props/deploy/flux-kustomization.yaml` (`suspend: true`); to
revive, restore that declaration to the root Flux bundle with `suspend: false`.
`props/deploy/forgejo/` holds retired Terraform kept for state/cleanup provenance and is not in
the package.

The backend image (`//props/backend:image`) bundles backend, frontend assets and every specimen
artifact at `/specimens/{slug}/`. `config.toml` (ConfigMap) enables `auto_migrate` and
`auto_sync_specimens` on boot.

The registry proxy manifests are revival scaffolding only: the Forgejo registry tenant's
content and upstream credential wiring were retired and must be restored deliberately. The
`props-registry-pull` Secret derives from the CNPG `props-db-app` credentials.
