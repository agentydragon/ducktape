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
- **Shell init scripts**: `nix/home/shell/*.sh` (bash-init.sh, zsh-init.zsh, common-init.sh)
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
- Host-specific rcm configs: `host-agentydragon/rcrc`, `host-gpd/rcrc`

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
- **Kubernetes** (`k8s/`): k3s cluster configurations

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

## Development Practices

### Testing

- Test files: `test_*.py` in same directory as code
- Framework: pytest with pytest-asyncio
- Fixtures for shared setup

### Deployment

```bash
cd ansible
ansible-playbook <hostname>.yaml --ask-become-pass
```
