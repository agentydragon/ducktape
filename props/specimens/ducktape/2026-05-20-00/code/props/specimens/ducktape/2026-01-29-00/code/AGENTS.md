@README.md

This file provides guidance to LLM agents for working with this repository.

@STYLE.md

## Before Hand-off

```bash
bazel build --config=check //...
bazel test //...
```

This runs ruff + mypy lint checks and all tests. For Rust code, also run `bazel build --config=rust-check //finance/...`.

If you touched `ansible/`, also follow the checklist in `ansible/AGENTS.md`.

## Repository Overview

"Ducktape" is a personal infrastructure repository — "duct tape" for personal infrastructure needs.

Manages configuration for: **agentydragon** (ThinkPad), **gpd** (GPD Win Max 2), **vps**, **atlas** (Proxmox/k3s).

## Directory Index

### Active Development

| Directory       | Purpose                          |
| --------------- | -------------------------------- |
| `adgn/`         | LLM agent framework              |
| `agent_server/` | FastAPI backend, runtime, policy |
| `cluster/`      | k8s cluster                      |
| `mcp_infra/`    | MCP compositor and utilities     |
| `agent_pkg/`    | Agent package infrastructure     |
| `tana/`         | Tana export toolkit              |
| `wt/`           | Worktree management              |
| `gatelet/`      | Gateway/tunneling                |
| `ansible/`      | System configuration             |
| `docker/`       | Container images                 |
| `dotfiles/`     | Shell configs, scripts           |
| `props/`        | Properties/specimens             |

### Less Active

| Directory          | Purpose                   |
| ------------------ | ------------------------- |
| `finance/`         | Portfolio tracking (Rust) |
| `trilium/`         | Trilium Notes extensions  |
| `inventree_utils/` | InventTree plugins        |
| `website/`         | Personal website (Hakyll) |
| `k8s-old/`         | legacy k3s cluster        |

## Dotfiles and Shell Configuration

**Most configuration has migrated to Nix home-manager** (see `nix/home/home.nix`).

### What Nix Manages

- **Shell configs**: `programs.bash`, `programs.zsh`, `programs.atuin`, `programs.direnv`, `programs.zoxide`, `programs.eza`
- **Shell init scripts**: `nix/home/shell/` (bash-init.sh, zsh-init.zsh, common-init.sh)
- **Aliases**: `home.shellAliases`
- **Environment variables**: `home.sessionVariables`
- **Powerlevel10k**: `nix/home/p10k.zsh` → `~/.p10k.zsh`

### What Remains in `dotfiles/`

- **`~/.profile`** - Complex conditional PATH management and legacy integrations (CUDA, lesspipe, dotnet, pnpm, machine-specific config)
- **`~/.secret_env`** - Secret environment variables (not tracked in git)
- **`~/.config/*`** - Application configs not yet migrated
- **`~/.local/bin/*`** - Utility scripts (theme switchers, backup utilities)
- **rcm config** - `rcrc` controls symlink behavior for remaining dotfiles

### Important Notes

- **DO NOT modify dotfiles directly in `~/`** - edit source files in `dotfiles/` or `nix/home/`
- **Shell configs are Nix-managed** - do not edit `~/.bashrc`, `~/.zshrc`, `~/.shellrc` directly

### Deployment

- **Nix config**: `home-manager switch --flake ~/code/ducktape/nix/home#<hostname>`
- **Remaining dotfiles**: Via rcm (managed by Ansible role `cli/tasks/dotfiles.yml`)

See `dotfiles/docs/shell-configuration.md` for detailed loading order and migration status.

## Infrastructure Components

### Ansible Automation

The `ansible/` directory contains system configuration.
See: @ansible/README.md

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

## Less Active Components

These components exist but see minimal recent changes:

### Finance Tools (`finance/`)

- Worthy: Rust-based portfolio tracker (uses Cargo/Bazel)
- Reconciliation utilities for various financial systems

### Knowledge Management

- **Trilium Notes** (`trilium/`): Extensions and widgets
- **Tana Export** (`tana/`): Export utilities

### Other Tools

- **InventTree** (`inventree_utils/`): Inventory management plugins
- **Website** (`website/`): Personal website (Hakyll/Haskell)

## Build System

This repository uses **Bazel** as the unified build system for all Python packages and most other components.

### Python (Bazel with rules_python)

```
ducktape/
├── MODULE.bazel             # Bazel module definition
├── requirements_bazel.txt   # Single source of truth for Python deps
├── adgn/BUILD.bazel         # Main LLM/agent package
├── agent_core/BUILD.bazel   # Core agent loop machinery
├── mcp_infra/BUILD.bazel    # MCP infrastructure
└── ...                      # Other packages with BUILD.bazel files
```

**Key points:**

- `requirements_bazel.txt` is the single source of truth for Python dependencies
- All Python packages have `BUILD.bazel` files defining targets
- Linting via Bazel aspects (`--config=check` runs ruff + mypy + eslint)
- Python 3.12+ is the target runtime version

**Development workflow:**

```bash
# Build all targets
bazel build //...

# Run all tests
bazel test //...

# Format code (ruff, prettier, shfmt, buildifier)
bazel run //tools/format

# Build specific target
bazel build //adgn:adgn
```

**Adding dependencies:**

1. Add to `requirements_bazel.txt`
2. Run `bazel run //:requirements.update` to regenerate lockfile
3. Use `@pypi//package_name` in BUILD.bazel deps

### Python BUILD.bazel Patterns (Gazelle-compatible)

This repository uses **Gazelle-compatible patterns** for Python BUILD files. This enables automatic BUILD file generation and maintenance via `bazel run //tools:gazelle`.

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
4. **Add NOTE comment** when removing aggregators:
   ```python
   # NOTE: No aggregator target - use specific per-file targets like :client, :server
   # This is the Gazelle-compatible pattern (python_generation_mode = file)
   ```

**Running Gazelle:**

```bash
bazel run //tools:gazelle              # Update BUILD files
bazel run //tools:gazelle -- --mode=diff  # Preview changes
```

### Rust (Finance tools)

```bash
bazel build //finance/worthy:rust_main
bazel test //finance/worthy/...
bazel build --config=rust-check //finance/...  # Rust linting
```

**Adding dependencies:**

1. Add to root `Cargo.toml`
2. Run `CARGO_BAZEL_REPIN=1 bazel sync --only=crates` to update lockfile
3. Use `@crates//crate_name` in BUILD.bazel deps

### Remote Cache / BuildBuddy (Optional)

To enable BuildBuddy remote caching and build event streaming, create `~/.config/bazel/buildbuddy.bazelrc`:

```
# BuildBuddy configuration
build --bes_results_url=https://app.buildbuddy.io/invocation/
build --bes_backend=grpcs://remote.buildbuddy.io
common --remote_cache=grpcs://remote.buildbuddy.io
common --remote_timeout=10m
common --remote_header=x-buildbuddy-api-key=YOUR_API_KEY_HERE
```

This file is loaded via `try-import` in `~/.bazelrc` and is silently ignored if missing.

## Development Practices

### Testing

- Test files: `test_*.py` in same directory as code
- Framework: pytest with pytest-asyncio
- Fixtures for shared setup

**IMPORTANT: Running tests and Python code**

Always use Bazel to run tests and Python code, not direct pytest or Python invocations:

```bash
# Run tests (CORRECT)
bazel test //path/to:test_target
bazel test //...  # Run all tests

# Run Python code (CORRECT)
bazel run //path/to:binary_target

# Do NOT use these (INCORRECT - they may not have correct paths/deps):
# pytest path/to/test_*.py
# python -m path.to.module
# direnv exec . python -m ...
```

Bazel properly sets up PYTHONPATH, dependencies, and the test environment. Direct pytest/python invocations may fail to find modules or have incorrect configurations.

#### pytest and Bazel

**CRITICAL**: All `py_test` targets MUST have a `pytest_bazel.main()` entry point:

```python
import pytest_bazel

# ... test code ...

if __name__ == "__main__":
    pytest_bazel.main()
```

Without this, Bazel runs the test file directly as a script, which imports and exits 0 (success) without actually running any tests. This caused 99% of tests to silently pass without executing.

Also add `@pypi//pytest_bazel` to the test's deps in BUILD.bazel.

#### pytest-asyncio auto mode

pytest-asyncio is configured in **auto mode** via `pytest_configure(config)` hooks in package-level `conftest.py` files (e.g., `agent_core/conftest.py`, `mcp_infra/conftest.py`, `props/conftest.py`). This automatically detects and runs `async def test_*()` functions without requiring explicit `@pytest.mark.asyncio` decorators.

**Do NOT add** `@pytest.mark.asyncio` decorators to new async tests - they are unnecessary and redundant with auto mode.

**Note**: There's also a root `//:conftest` py_library target available, but most tests use their package-level conftest.py which already configures auto mode. No special Bazel dependency is needed - package conftest.py files are automatically discovered by pytest when included in test `srcs`.

For async fixtures, use:

```python
@pytest.fixture
async def my_fixture():
    # async setup
    yield value
    # async teardown
```

#### Live OpenAI API tests

Tests that call the real OpenAI API use the `@pytest.mark.live_openai_api` marker and Bazel macros from `//openai_utils/testing:testing.bzl`.

**Two-tier pattern: mock + live in one file.** A single test file can contain both mock tests (verifying our code behaves correctly given expected OpenAI responses) and live tests (verifying OpenAI actually responds as we expect). Use `live_openai_py_test` in BUILD.bazel — it generates `.mock` and `.live` Bazel targets from one declaration:

```python
# test_foo.py
async def test_our_logic_with_mock(mock_client):
    ...

@pytest.mark.live_openai_api
async def test_our_logic_against_real_api(live_openai):
    ...
```

```python
# BUILD.bazel
load("//openai_utils/testing:testing.bzl", "live_openai_py_test")

live_openai_py_test(
    name = "test_foo",
    srcs = ["test_foo.py"],
    deps = [...],
)
# Generates: test_foo.mock (runs non-live tests) and test_foo.live (runs live tests with API key)
```

**Live-only files.** For files where every test calls the real API (no mock counterpart), use `live_openai_only_py_test` — it generates a single unsuffixed target with the `live_openai_api` tag and `env_inherit`:

```python
# BUILD.bazel
load("//openai_utils/testing:testing.bzl", "live_openai_only_py_test")

live_openai_only_py_test(
    name = "test_live_api",
    srcs = ["test_live_api.py"],
    deps = [...],
)
```

**Gating:** `.live` / `live_openai_only_py_test` targets get `OPENAI_API_KEY` via `env_inherit` and the `live_openai_api` tag. CI excludes them with `--test_tag_filters=-live_openai_api`. The root `conftest.py` also skips live-marked tests at runtime when the key is absent.

### Deployment

```bash
cd ansible
ansible-playbook <hostname>.yaml --ask-become-pass
```
