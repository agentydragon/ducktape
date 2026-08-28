# Bazel Migration Status

## Vision

Fully Bazel-managed repository: `bazel build //...`, `bazel test //...`, `bazel lint //...` cover everything. No direct tool invocations outside Bazel. All container images via rules_oci. Single dependency source per language.

### What Stays Outside Bazel

- Ansible (Ansible Galaxy)
- Nix configuration
- Website (Haskell/stack)

## Current State (February 2026)

6 of 8 success criteria met. Python at 95.6% Bazel coverage (990/1036 files), Rust at 100%, Terraform at 100% (76/76 modules). All linters integrated into `bazel lint //...` (ruff, mypy, clippy/rustfmt, eslint, buildifier, yamllint). 12 Docker images migrated to rules_oci. Flat package layout with colocated tests is the norm. Single root `pyproject.toml` remains (tool config only). Pre-commit framework handles git hooks; Claude Code session hooks handle proxy setup for web sessions.

Run `bazel run //devinfra/orphans:find_orphans_bin` to list orphaned files.

## Remaining Work

### High Priority

- **Unified check command (Phase 3)**: Create `bazel check //...` that runs all linters + type checkers. Simplify pre-commit to single command. Update CI to use it.

### Lower Priority

- **Package consolidation**: Small experimental packages could move into `hack/` monolith. Keep packages separate when they have different deployment targets or dependency sets.
- **Ruff version alignment**: 0.14.0 in `devinfra/lockfile.json` vs 0.14.6 in `.pre-commit-config.yaml`.
- **Remove `check-ast` pre-commit hook**: Redundant with `bazel build`.

### Intentionally Not Bazelized

| Item                                                                    | Reason                          |
| ----------------------------------------------------------------------- | ------------------------------- |
| `ansible/`                                                              | Ansible Galaxy                  |
| `nix/`                                                                  | Nix configuration               |
| `finance/gnucash_util.py`                                               | Requires system gnucash library |
| Shell scripts (CI, entrypoints, deployment, Nix-managed, tool wrappers) | Various — see categories below  |

Shell script categories: CI/Ansible (`.github/scripts/`, `ansible/scripts/`), Docker entrypoints, deployment scripts (`llm/deploy.sh`, etc.), Nix-managed (`nix/home/**/*.sh`), Bazel test helpers (`devinfra/lint/run_*.sh`).

### Manual Targets (require special environment)

| Target                   | Reason                                                                       |
| ------------------------ | ---------------------------------------------------------------------------- |
| `//gnome/gterm_theme:*`  | Requires DBUS/GNOME session                                                  |
| `website/` nested module | Haskell/stack build system; build via `cd website && bazelisk build //:site` |

## Reference

### Commands

```bash
bazel build //...                       # Build + lint (runs by default)
bazel run //:requirements.update        # Update Python requirements lock
bazel run //devinfra/orphans:find_orphans_bin  # Find un-Bazelized files
bazel run //tools:gazelle               # Update BUILD files
bazel run //devinfra/lint:buildifier       # Format BUILD files
```

### Linter Configuration

| Tool         | Config                         | Bazel Command                                   |
| ------------ | ------------------------------ | ----------------------------------------------- |
| Ruff         | `ruff.toml`                    | `bazel build //...` (default)                   |
| mypy         | `mypy.ini`                     | `bazel build //...` (default)                   |
| ESLint       | `eslint.config.js`             | `bazel build //...` (default)                   |
| Prettier     | `props/frontend/.prettierrc`   | `bazel test //props/frontend:prettier_test`     |
| svelte-check | `props/frontend/tsconfig.json` | `bazel test //props/frontend:svelte_check_test` |
| buildifier   | `devinfra/lint/BUILD.bazel`    | `bazel run //devinfra/lint:buildifier`          |
| nixfmt       | N/A                            | Pre-commit hook only                            |

Ruff uses a custom `rules_multitool` lockfile (`devinfra/lockfile.json`) to override the older version bundled in `aspect_rules_lint`. Mypy uses `rules_mypy` v0.40.0 with `follow_imports = silent` and `ignore_missing_imports = True`.

### Hook Lifecycle

**Git pre-commit** (via `.pre-commit-config.yaml`): safety checks, syntax validation, Ansible check, Bazel format/lint, ruff autofix, markdownlint, kubeconform, nixfmt.
