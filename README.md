# Ducktape

Personal infrastructure monorepo. Manages configuration for: **agentydragon** (ThinkPad), **gpd** (GPD Win Max 2), **vps**, **atlas** (Proxmox/Talos k8s).

## Directory Index

| Directory          | Purpose                             |
| ------------------ | ----------------------------------- |
| `agent_cli/`       | Agent REPL CLI                      |
| `x/agent_server/`  | FastAPI backend, runtime, policy    |
| `cluster/`         | k8s cluster                         |
| `mcp_infra/`       | MCP compositor and utilities        |
| `agent_pkg/`       | Agent package infrastructure        |
| `props/`           | LLM critic eval system              |
| `devinfra/`        | Repo build, CI, lint infrastructure |
| `ansible/`         | System configuration (playbooks)    |
| `wt/`              | Worktree management                 |
| `openai_utils/`    | OpenAI API utilities                |
| `dotfiles/`        | Shell configs (migrating to Nix)    |
| `tana/`            | Tana export toolkit                 |
| `finance/`         | Portfolio tracking (Rust)           |
| `trilium/`         | Trilium Notes extensions            |
| `inventree_utils/` | InventTree plugins                  |
| `website/`         | Personal website (Hakyll)           |
| `x/k8s_old/`       | Legacy k3s cluster                  |

## Build System

**Bazel** is the unified build system. Python 3.13+, Rust via Cargo/Bazel.

### Python

- Deps: add to `pyproject.toml`, run `bazel run //:requirements.update`, use `@pypi//pkg` in BUILD
- Lockfile: `requirements_bazel.txt` (never edit manually)
- Lint: ruff + mypy via Bazel aspects (default on; `--config=nolint` to skip)

### Gazelle

One `py_library` per `.py` file (no aggregators). Reference `//pkg:module` not `//pkg`. Bazel auto-generates `__init__.py` stubs via `imports = [".."]`.

```bash
bazel run //devinfra:gazelle              # Update BUILD files
bazel run //devinfra:gazelle -- --mode=diff  # Preview changes
```

### Rust

```bash
# Add to root Cargo.toml, then:
CARGO_BAZEL_REPIN=1 bazel build @crates//:all  # Update Cargo.Bazel.lock
# Use @crates//crate_name in BUILD.bazel deps
```

### Remote Cache + RBE

BuildBuddy provides remote cache and execution. See <devinfra/setup_buildbuddy.sh>.

RBE worker image: `ghcr.io/agentydragon/rbe-worker` from <devinfra/rbe_image/Dockerfile>.

## Dotfiles

Managed by Nix home-manager in `nix/home/`. **Do NOT edit dotfiles in `~/`**.

Deploy: `home-manager switch --flake ~/code/ducktape#<hostname>`

## Ansible

See <ansible/README.md>. Playbooks: `agentydragon.yaml`, `vps.yaml`, `gpd.yaml`, `wyrm.yaml`.

## Development

```bash
pre-commit install  # Installs ruff, buildifier, rustfmt, prettier, etc.
```

### Lint/Format Exclusions

Exclusions require two files (pre-commit reads `.gitattributes`, ruff reads `ruff.toml`):

1. Add `path/** rules-lint-ignored=true` to `.gitattributes`
2. If Python, also add to `ruff.toml exclude`

## CI

- **BuildBuddy Workflows**: `bazel test //...` + `bazel build //...` (RBE, lint)
- **GitHub Actions**: non-Bazel tasks (ansible-lint, nix, pre-commit), artifact publishing (wheels, container images)

See `.github/workflows/` and `buildbuddy.yaml`.

## Common Commands

```bash
bazel run //:requirements.update       # Update Python lockfile
bazel run //devinfra/lint:buildifier    # Format Bazel files
```

## License

AGPL 3.0
