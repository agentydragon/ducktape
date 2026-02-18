# GitHub Copilot Instructions

For detailed repository guidance, see: [AGENTS.md](../AGENTS.md) and [STYLE.md](../STYLE.md)

## Repository Overview

"Ducktape" is a personal infrastructure repository. Key areas:

- **Agent Framework** (`agent_cli/`, `agent_server/`, `agent_core/`, `agent_pkg/`) - Agent REPL, FastAPI backend, runtime
- **Props** (`props/`) - Code evaluation system with Docker-based E2E tests
- **MCP Infrastructure** (`mcp_infra/`) - MCP compositor and utilities
- **Infrastructure Automation** (`ansible/`) - System configuration and deployment
- **Development Tools** (`wt/`) - Worktree management
- **Dotfiles** (`dotfiles/`, `nix/home/`) - Shell configs (mostly Nix home-manager now)
- **Cluster** (`cluster/`) - k8s cluster configuration

## Build System

**Bazel** is the unified build system. Always use Bazel, never direct `pytest` or `python`:

```bash
bazel build //...                    # Build all (lint runs by default)
bazel test //...                     # Run all tests
bazel run //tools/format             # Format code
bazel run //tools:gazelle            # Update BUILD files
```

Python 3.12+. Dependencies in `requirements_bazel.txt`.

## Verification (Required)

Before handing in any work:

```bash
bazel build //...   # Build + lint (runs by default)
bazel test //...    # Run all tests
```

For Rust code: `bazel build //finance/...`

If you modified `ansible/`, follow the checklist in [ansible/AGENTS.md](../ansible/AGENTS.md).

## Testing

- Tests: `test_*.py` adjacent to the code they test
- Framework: pytest with pytest-asyncio (auto mode)
- All `py_test` targets MUST have `pytest_bazel.main()` entry point
- Do NOT add `@pytest.mark.asyncio` — auto mode handles it

## Props E2E Tests

Props E2E tests use per-test testcontainers (PostgreSQL, Docker registry, etc.) and are fully hermetic — no manual setup required. Just run:

```bash
bazel test //props/...
```
