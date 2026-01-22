# GitHub Copilot Instructions

This file provides instructions for GitHub Copilot and related AI coding assistants.

For detailed repository guidance, see: [AGENTS.md](../AGENTS.md)

## Repository Overview

"Ducktape" is a personal infrastructure repository containing various projects and utilities for managing configuration and deployment across multiple systems. Key areas:

- **LLM Tooling** (`llm/`, `adgn/`, `experimental/`) - Agent framework with MCP support
- **Infrastructure Automation** (`ansible/`) - System configuration and deployment
- **Development Tools** (`wt/`, `gatelet/`) - Worktree management and gateway services
- **Dotfiles** (`dotfiles/`) - Centrally managed via rcm (DO NOT modify files in `~/.` directly)

## Build System

The repository uses **Bazel** as the unified build system:

```bash
# Build all targets
bazel build //...

# Run tests
bazel test //...

# Lint (ruff + mypy via aspect_rules_lint)
bazel lint //...
```

**Python dependencies**: Managed via `requirements_bazel.txt` (single source of truth).
Target: Python 3.12+

### Rust

```bash
bazel build //finance/worthy:rust_main
bazel test //finance/worthy/...
bazel lint --config=rust-check //finance/...
```

**Rust dependencies**: Managed via root `Cargo.toml` + crate_universe.

## Code Style

Follow conventions in [STYLE.md](../STYLE.md):

- **No exception swallowing**: Catch specific exceptions, let real errors surface
- **Prefer exceptions over error lists**: Raise exceptions on validation failure
- **Use Pydantic as typed objects**: Access fields directly (`model.field`), not `dict.get(...)`
- **Explicit keyword arguments**: Use `Model(field=value)`, not `**kwargs` unpacking
- **Use enum values directly**: `EnumClass.VALUE`, not string literals
- **Let exceptions propagate**: Define error boundaries once, don't catch/reformat at each call site

## Testing

- Test files: `test_*.py` in same directory as code
- Framework: pytest with pytest-asyncio
- Use fixtures for shared test components (prefer conftest.py)
- Keep test bodies concise and focused on assertions

## Verification (Required)

**Before handing in any work, you MUST ensure all lint and tests pass.**

```bash
bazel lint //...   # Lint (ruff + mypy)
bazel test //...   # Run all tests
```

For Rust code, also run: `bazel lint --config=rust-check //finance/...`

All checks must pass before the work is considered complete.

### Ansible-Specific Changes

If you modify any files in `ansible/`, follow the dedicated checklist in [`ansible/AGENTS.md`](../ansible/AGENTS.md).

## Props Environment Setup

The props ecosystem is a code evaluation system that requires Docker infrastructure to run E2E tests. When working on props code, you'll need to set up the environment.

### Quick Setup

```bash
# 1. Generate environment variables
export PGPASSWORD=$(openssl rand -base64 24)
export OPENAI_API_KEY=test-key-not-used

# 2. Build Docker images
bazel run //props/registry_proxy:load
bazel run //props/llm_proxy:load

# 3. Pull infrastructure images
docker pull postgres:16
docker pull registry:2

# 4. Start infrastructure
cd props
docker compose up -d

# 5. Wait for services (PostgreSQL, registry, registry proxy)
until pg_isready -h 127.0.0.1 -p 5433 -U postgres 2>/dev/null; do sleep 1; done
until curl -sf http://127.0.0.1:5050/v2/ 2>/dev/null; do sleep 1; done

# 6. Initialize database
export PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres PGDATABASE=eval_results
export ADGN_PROPS_SPECIMENS_ROOT="$PWD/props/testing/fixtures/testdata/specimens"
bazel run //props/cli:cli -- db recreate -y

# 7. Push agent images to registry
bazel run //props/critic:push
bazel run //props/grader:push
bazel run //props/critic_dev/improve:push
bazel run //props/critic_dev/optimize:push
```

### Running Props E2E Tests

After setup, run E2E tests with:

```bash
export PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres PGDATABASE=eval_results
export AGENT_PGHOST=127.0.0.1
export PROPS_REGISTRY_PROXY_HOST=127.0.0.1
export PROPS_REGISTRY_PROXY_PORT=5051
export PROPS_DOCKER_NETWORK=props-agents
export PROPS_E2E_HOST_HOSTNAME=172.17.0.1

bazel test --keep_going \
  //props/critic:test_e2e \
  //props/critic_dev/improve:test_e2e \
  //props/critic_dev/optimize:test_e2e \
  //props/core:test_agent_pkg_e2e
```

### Cleanup

```bash
cd props
docker compose down    # Stop services
docker compose down -v # Stop and remove volumes (full cleanup)
```

### Environment Variables

The props environment uses these key variables (automatically configured in `.github/workflows/copilot-setup-steps.yml`):

- `ADGN_PROPS_SPECIMENS_ROOT`: Path to test fixtures (in-repo: `props/testing/fixtures/testdata/specimens`)
- `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`: PostgreSQL connection for host
- `AGENT_PGHOST`: PostgreSQL host for agent containers (127.0.0.1)
- `PROPS_REGISTRY_PROXY_HOST`, `PROPS_REGISTRY_PROXY_PORT`: Registry proxy config (127.0.0.1:5051)
- `PROPS_DOCKER_NETWORK`: Docker network for agents (props-agents)
- `PROPS_E2E_HOST_HOSTNAME`: Host network address for containers (172.17.0.1)

**Note**: These differ from Claude hooks which use `host` network. GitHub Actions uses `props-agents` Docker network (simpler, no HTTP proxy needed).

### Troubleshooting

**Services not starting?** Check logs:

```bash
docker logs props-postgres
docker logs props-registry-proxy
docker logs props-llm-proxy
```

**Database connection issues?** Verify PostgreSQL:

```bash
PGPASSWORD=$PGPASSWORD psql -h 127.0.0.1 -p 5433 -U postgres -d eval_results -c "SELECT 1"
```

**Registry issues?** Test connectivity:

```bash
curl http://127.0.0.1:5050/v2/
curl http://127.0.0.1:5051/v2/
```
