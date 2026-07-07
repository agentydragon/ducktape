# Ducktape

Personal infrastructure monorepo. Manages configuration for: **agentydragon** (ThinkPad), **gpd** (GPD Win Max 2), **vps**, **atlas** (Proxmox/Talos k8s).

## Selected Directory Index

| Directory          | Purpose                                       |
| ------------------ | --------------------------------------------- |
| `x/agent_cli/`     | Agent REPL CLI                                |
| `x/agent_server/`  | FastAPI backend, runtime, policy              |
| `cluster/`         | k8s cluster                                   |
| `tf/`              | GitOps Terraform modules (tofu-ctrl)          |
| `mcp_infra/`       | MCP compositor and utilities                  |
| `grocy_mcp/`       | Auth-aware remote MCP server for Grocy        |
| `x/editor_agent/`  | Experimental editor agent and package helpers |
| `airlock/`         | Human-in-the-loop MCP approval proxy          |
| `haku/`            | Personal background agent                     |
| `props/`           | LLM critic eval system                        |
| `devinfra/`        | Repo build, CI, lint infrastructure           |
| `ansible/`         | System configuration (playbooks)              |
| `nix/`             | NixOS and home-manager configurations         |
| `skills/`          | Agent skills and skill packaging              |
| `wt/`              | Worktree management                           |
| `openai_utils/`    | OpenAI API utilities                          |
| `tana/`            | Tana export toolkit                           |
| `finance/`         | Portfolio tracking (Rust)                     |
| `finance/augur/`   | Probabilistic financial-futures simulator     |
| `loom/`            | Prediction markets to rollout interpolator    |
| `cpap/`            | CPAP data sync and analysis skill             |
| `difftree/`        | Tree-style git diff visualization             |
| `gmail_archiver/`  | Gmail cleanup and filter sync tooling         |
| `gmail_api/`       | Shared Gmail API models and service builder   |
| `gnome/`           | GNOME desktop utilities and Shell extensions  |
| `qr_codes/`        | Household SVG QR codes                        |
| `idea/`            | Lightweight future project ideas              |
| `trilium/`         | Trilium Notes extensions                      |
| `inventree_utils/` | InventTree plugins                            |
| `website/`         | Personal website (Hakyll)                     |

## Build System

**Bazel** is the unified build system. Python 3.13+, Rust via Cargo/Bazel.

### Python

- Deps: add to `pyproject.toml`, regenerate the lockfile via <devinfra/docs/lockfiles.md>, use `@pypi//pkg` in BUILD
- Lockfile: `requirements_bazel.txt` (never edit manually)
- Lint: ruff + mypy via Bazel aspects (default on; `--config=nolint` to skip)

### Gazelle

One `py_library` per `.py` file (no aggregators). Reference `//pkg:module` not `//pkg`. Bazel auto-generates `__init__.py` stubs via `imports = [".."]`.

```bash
bb run //devinfra:gazelle              # Update BUILD files
bb run //devinfra:gazelle -- --mode=diff  # Preview changes
```

### Rust

Add deps to root `Cargo.toml`, regenerate `Cargo.Bazel.lock` via
<devinfra/docs/lockfiles.md>, then use `@crates//crate_name` in BUILD deps.

### Remote Cache + RBE

BuildBuddy provides remote caching and remote build execution (RBE). Build actions run on BuildBuddy runner VMs; results are cached so unchanged targets are instant on repeat runs.

- `bbr` — convenience wrapper around `bb remote`; runs the entire Bazel invocation on a BuildBuddy runner with RBE enabled. Default for builds, tests, and queries.
- `bb run //target` — Bazel runs locally, build actions dispatched to RBE, binary always executed locally.

RBE worker image: `ghcr.io/agentydragon/rbe-worker` from <devinfra/rbe_image/Dockerfile>. Setup: <devinfra/setup_buildbuddy.sh>.

## Dotfiles

Managed by Nix home-manager in `nix/home/`. **Do NOT edit dotfiles in `~/`**.

Deploy: see <nix/README.md> (NixOS hosts use `sudo nixos-rebuild switch`; standalone non-NixOS configs use `home-manager switch`).

## Development

```bash
pre-commit install  # Installs ruff, buildifier, rustfmt, prettier, etc.
```

### Lint/Format Exclusions

Exclusions require two files (pre-commit reads `.gitattributes`, ruff reads `ruff.toml`):

1. Add `path/** rules-lint-ignored=true` to `.gitattributes`
2. If Python, also add to `ruff.toml exclude`

Other gitattributes consumed by pre-commit checks:

- `filename-conventions-ignored=true` — skips kebab-case filename enforcement
  (defaulted on for `cluster/`, `terraform/`, `tf/`, and a few other trees
  where kebab-case is conventional).
- `cluster-manifest-ignored=true` — for YAML files under `cluster/k8s/` that
  aren't K8s manifests (e.g. `rules_distroless` apt manifests next to a
  CronJob image's `BUILD.bazel`). The cluster validator skips them from
  orphan detection and resource parsing.

## CI

- **GitHub Actions + `bbr`**: `bazel {build,test} //...` via `bbr` (remote Bazel on BuildBuddy RBE, includes lint)
- **GitHub Actions (non-Bazel)**: ansible-lint, nix, pre-commit, artifact publishing (wheels, container images)

See `.github/workflows/`.

## Common Commands

```bash
bb run //devinfra/lint:buildifier    # Format Bazel files
```

Lockfile and generated manifest workflows: <devinfra/docs/lockfiles.md>.

## Conventions

### `x/` — Experimental

`x/` subdirectories (e.g. `x/agent_server/`, `finance/augur/x/`) mark experimental, in-flux, or one-off code that hasn't stabilized. Any directory at any level can have an `x/` subfolder. Don't expect stable APIs or finished design from code under `x/`.

### `TODO.md`

`<dir>/TODO.md` tracks persistent project-level TODOs. Inline code comments are fine for TODOs local to a specific location; cross-cutting or project-wide items go in `TODO.md`. Remove entries once fully completed.

### `plans/`

`<dir>/plans/` holds future work and work-in-progress design notes. Delete or tombstone a plan once it's fully done.

When a component has one central plan, put it at `<dir>/PLAN.md` instead of a single-file `plans/` directory (e.g. `loom/PLAN.md`, `haku/PLAN.md`). Same lifecycle: delete or tombstone once fully done.

### `debug/`

`<dir>/debug/<topic>.md` holds investigation notes, RCAs, and debug logs. The `cluster/` subproject uses `cluster/docs/lessons_learned/` instead.

### `archive/`

`<dir>/archive/` holds inactive historical notes, abandoned approaches, and past blind alleys that are useful to keep but should not be read as current plans. Prefer dated Markdown names like `YYYY_MM_whatever.md` or `YYYY_MM_DD_whatever.md` when adding a new archive note.

### `SPEC.md`

`<dir>/SPEC.md` is the high-level, user-facing specification of what a component guarantees. An outside observer should be able to read it to understand the component's contract without reading the implementation. Keep it at the "what it promises" level — implementation details belong in README.md or the code.

## License

AGPL 3.0
