# Ducktape

My personal infrastructure's duct tape. Projects that didn't yet warrant making
into separate repositories.

## Repository Overview

"Ducktape" is a personal infrastructure repository — "duct tape" for personal infrastructure needs.

Manages configuration for: **agentydragon** (ThinkPad), **gpd** (GPD Win Max 2), **vps**, **atlas** (Proxmox/k3s).

## Directory Index

### Active Development

| Directory         | Purpose                             |
| ----------------- | ----------------------------------- |
| `agent_cli/`      | Agent REPL CLI                      |
| `x/agent_server/` | FastAPI backend, runtime, policy    |
| `cluster/`        | k8s cluster                         |
| `mcp_infra/`      | MCP compositor and utilities        |
| `agent_pkg/`      | Agent package infrastructure        |
| `tana/`           | Tana export toolkit                 |
| `wt/`             | Worktree management                 |
| `ansible/`        | System configuration                |
| `docker/`         | Container images                    |
| `dotfiles/`       | Shell configs, scripts              |
| `devinfra/`       | Repo build, CI, lint infrastructure |
| `props/`          | LLM critic eval system              |

### Less Active

| Directory          | Purpose                   |
| ------------------ | ------------------------- |
| `finance/`         | Portfolio tracking (Rust) |
| `trilium/`         | Trilium Notes extensions  |
| `inventree_utils/` | InventTree plugins        |
| `website/`         | Personal website (Hakyll) |
| `x/k8s_old/`       | legacy k3s cluster        |

### Less Active Components

These components exist but see minimal recent changes:

#### Finance Tools (`finance/`)

- Worthy: Rust-based portfolio tracker (uses Cargo/Bazel)
- Reconciliation utilities for various financial systems

#### Knowledge Management

- **Trilium Notes** (`trilium/`): Extensions and widgets
- **Tana Export** (`tana/`): Export utilities

#### Other Tools

- **InventTree** (`inventree_utils/`): Inventory management plugins
- **Website** (`website/`): Personal website (Hakyll/Haskell)

## Build System

This repository uses **Bazel** as the unified build system for all Python packages and most other components.

### Python (Bazel with rules_python)

- `requirements_bazel.txt` - Single source of truth for Python dependencies
- All Python packages have `BUILD.bazel` files defining targets
- Linting via Bazel aspects (runs by default; use `--config=nolint` to skip)
- Python 3.13+ is the target runtime version

**Adding dependencies:**

1. Add the constraint to `pyproject.toml` (the single source of truth for Python dependency constraints)
2. Run `bazel run //:requirements.update` to regenerate the lockfile (`requirements_bazel.txt` — never edit manually)
3. Use `@pypi//package_name` in BUILD.bazel deps

### Python BUILD.bazel Patterns (Gazelle-compatible)

This repository uses **Gazelle-compatible patterns** for Python BUILD files. This enables automatic BUILD file generation and maintenance via `bazel run //devinfra:gazelle`.

**Key pattern: One `py_library` per `.py` file (no aggregators)**

```python
# CORRECT - per-file targets
py_library(
    name = "client",
    srcs = ["client.py"],
    deps = ["//other_pkg:specific_target"],
)

py_library(
    name = "server",
    srcs = ["server.py"],
    deps = [":client"],
)

# WRONG - aggregator bundling multiple files
py_library(
    name = "my_package",  # Don't do this
    srcs = ["client.py", "server.py"],
    deps = [...],
)
```

**Rules:**

1. **No aggregator targets** - Each `.py` file gets its own `py_library` with `name` matching the file stem
2. **Reference specific targets** - Use `//pkg:module` not `//pkg` (e.g., `//openai_utils:model` not `//openai_utils`)
3. **Use `imports = [".."]`** - Bazel auto-generates `__init__.py` stubs; don't create real `__init__.py` files

**Running Gazelle:**

```bash
bazel run //devinfra:gazelle              # Update BUILD files
bazel run //devinfra:gazelle -- --mode=diff  # Preview changes
```

### Rust (Finance tools)

```bash
bazel build //finance/worthy:rust_main
bazel test //finance/worthy/...
```

**Adding dependencies:**

1. Add to root `Cargo.toml`
2. Run `CARGO_BAZEL_REPIN=1 bazel build @crates//:all` to update `Cargo.Bazel.lock`
3. Use `@crates//crate_name` in BUILD.bazel deps

### Remote Cache + RBE via BuildBuddy

For BuildBuddy setup, see `devinfra/setup_buildbuddy.sh`.

RBE worker image `ghcr.io/agentydragon/rbe-worker` is built from <devinfra/rbe_image/Dockerfile>, based on BuildBuddy's `rbe-ubuntu24-04` image with our additions.
It's built and pushed by the `rbe-image.yml` CI workflow.

## Dotfiles and Shell Configuration

Most configuration has migrated to Nix home-manager - see `nix/home/home.nix`.

### Important Notes

- **DO NOT modify dotfiles directly in `~/`** - edit source files in `dotfiles/` or `nix/home/`
- **Shell configs are Nix-managed** - do not edit `~/.bashrc`, `~/.zshrc`, `~/.shellrc` directly

### Deployment

- **Nix config**: `home-manager switch --flake ~/code/ducktape#<hostname>`
- **Remaining dotfiles**: Via rcm (managed by Ansible role `cli/tasks/dotfiles.yml`)

See `dotfiles/docs/shell_configuration.md` for detailed loading order and migration status.

## Infrastructure Components

### Ansible Automation

The `ansible/` directory contains system configuration.
See <ansible/README.md> for details.

#### Playbooks

- `agentydragon.yaml` - Main laptop configuration
- `vps.yaml` - VPS server deployment
- `gpd.yaml` - GPD laptop setup
- `wyrm.yaml` - Wyrm desktop provisioning

#### Key Roles

- **System Base**: `cli/`, `gui/`, `common/`
- **Development**: `golang/`, `dev_env/`, `dev_clojure/`
- **Services**: `trilium_server/`, `headscale_server/`, `syncthing_server/`
- **Networking**: `tailscale_client/`

### Network Infrastructure

- **Headscale**: Self-hosted Tailscale controller (100.64.0.0/10)
- **Syncthing**: Cross-device file synchronization

## Development

This repository uses **Bazel** as the primary build system. Install the git pre-commit hook:

```bash
pre-commit install
```

This installs the pre-commit framework which runs ruff, buildifier, rustfmt, prettier and other linters on staged files, checks for conflict markers, validates syntax, and more (see `.pre-commit-config.yaml`). ESLint and mypy run automatically on every `bazel build` (use `--config=nolint` to skip).

### Lint/Format Exclusions

Files excluded from linting and formatting are controlled in two places:

| File                | Purpose                            | Read by                  |
| ------------------- | ---------------------------------- | ------------------------ |
| `.gitattributes`    | Source of truth for all exclusions | pre-commit hooks         |
| `ruff.toml exclude` | Must mirror Python patterns        | ruff check (lint aspect) |

**Why two files?** Pre-commit hooks check `.gitattributes` for exclusions, but ruff's linter only reads `ruff.toml`. For Python files, patterns must exist in both.

To exclude a file/directory:

1. Add `path/** rules-lint-ignored=true` to `.gitattributes`
2. If it contains Python, also add to `ruff.toml exclude`

## Continuous Integration

### BuildBuddy Workflows (Recommended for Bazel tasks)

BuildBuddy Workflows provides fast, Bazel-native CI with remote execution. The `buildbuddy.yaml` configuration runs:

- `bazel test //...` - Run all tests (excluding `live_openai_api`)
- `bazel build //...` - Unified quality checks (ruff, ESLint, mypy, clippy, rustfmt; runs by default)

**Setup**:

1. Enable BuildBuddy Workflows for this repository at <https://app.buildbuddy.io>
2. BuildBuddy will automatically use your RBE configuration from `devinfra/setup_buildbuddy.sh`
3. Workflow runs appear in GitHub PRs as status checks

**Architecture**:

- **BuildBuddy**: Bazel build/test/lint (fast RBE, dependency-aware parallelization)
- **GitHub Actions**: Non-Bazel tasks (ansible-lint, nix-flake-check, pre-commit) and artifact publishing

Artifacts (wheels, container images) are built by BuildBuddy, then published by GitHub Actions:

- Wheels → GitHub Releases
- Container images → GitHub Container Registry (GHCR)

### GitHub Actions

GitHub Actions handles:

- Non-Bazel workflows (ansible, nix, pre-commit)
- Release publishing (wheels, Docker images)
- RBE image builds

See `.github/workflows/` for workflow definitions.

## License

AGPL 3.0

## Updates

To update Python requirements lock:

```bash
bazel run //:requirements.update
```

To format Bazel configuration files:

```bash
bazel run //devinfra/lint:buildifier
```

## Running GitHub Actions Locally

Use [act](https://github.com/nektos/act) to dry-run `.github/workflows/ci.yml`. With Nix:

```bash
# From repo root
nix run nixpkgs#act -- -W .github/workflows/ci.yml \
  -P ubuntu-latest=catthehacker/ubuntu:act-latest
```

Tips:

- `act` needs Docker. Make sure `docker pull catthehacker/ubuntu:act-latest` works first.
- Use `act -j <job-name>` to run a single job.
