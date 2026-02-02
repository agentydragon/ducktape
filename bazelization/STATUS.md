# Bazel Migration Status

## Vision

Fully Bazel-managed repository: `bazel build //...`, `bazel test //...`, `bazel lint //...` cover everything. No direct tool invocations outside Bazel. All container images via rules_oci. Single dependency source per language.

### What Stays Outside Bazel

- Ansible (Ansible Galaxy)
- Nix configuration
- Website (Haskell/stack)

## Current State (January 2026)

6 of 8 success criteria met. Python at 91.1% Bazel coverage (944/1036 files), Rust at 100%. All linters integrated into `bazel lint //...` (ruff, mypy, clippy/rustfmt, eslint, buildifier, yamllint). 12 Docker images migrated to rules_oci. Flat package layout with colocated tests is the norm. Single root `pyproject.toml` remains (tool config only). Pre-commit framework handles git hooks; Claude Code session hooks handle proxy setup for web sessions.

Run `bazel run //tools/orphans:find_orphans` to list orphaned Python files.

## Remaining Work

### High Priority

- **Unified check command (Phase 3)**: Create `bazel check //...` that runs all linters + type checkers. Simplify pre-commit to single command. Update CI to use it.
- **Enable remote cache write in CI**: Currently read-only (`--remote_upload_local_results=false`). Enable for main branch.
- **Migrate `claude_optimizer` Docker images**: 8 variant Dockerfiles in `claude/claude_optimizer/docker/` (go, node, python, python-data, ruby, rust, system, claude-base).

### Lower Priority

- **Package consolidation**: `mcp_infra/` and `agent_core/` could merge into `adgn/`. Small experimental packages into `experimental/` monolith. Keep packages separate when they have different deployment targets or dependency sets.
- **Ruff version alignment**: 0.14.0 in `tools/multitool/lockfile.json` vs 0.14.6 in `.pre-commit-config.yaml`.
- **Remove `check-ast` pre-commit hook**: Redundant with `bazel build`.

### Intentionally Not Bazelized

| Item                                                                    | Reason                          |
| ----------------------------------------------------------------------- | ------------------------------- |
| `ansible/`                                                              | Ansible Galaxy                  |
| `nix/`                                                                  | Nix configuration               |
| `finance/gnucash_util.py`                                               | Requires system gnucash library |
| Shell scripts (CI, entrypoints, deployment, Nix-managed, tool wrappers) | Various — see categories below  |

Shell script categories: CI/Ansible (`.github/scripts/`, `ansible/scripts/`), Docker entrypoints, deployment scripts (`llm/deploy.sh`, etc.), Nix-managed (`nix/home/**/*.sh`), Bazel test helpers (`tools/lint/run_*.sh`).

### Manual Targets (require special environment)

| Target                                | Reason                      |
| ------------------------------------- | --------------------------- |
| `//gnome_terminal_profile_switcher:*` | Requires DBUS/GNOME session |
| `//mcp_starter:integration_test`      | Requires running MCP server |
| `//website:*`                         | Haskell/stack build system  |

## Reference

### Commands

```bash
bazel build //...                       # Build everything
bazel test //...                        # Test everything
bazel lint //...                        # Lint all languages
bazel build --config=check //...        # Lint + typecheck
bazel build --config=typecheck //...    # Mypy only
bazel run //:requirements.update        # Update Python requirements lock
bazel run //tools/orphans:find_orphans  # Find un-Bazelized Python files
bazel run //tools:gazelle               # Update BUILD files
bazel run //tools/format                # Format code
bazel run //tools/lint:buildifier       # Format BUILD files
```

### Linter Configuration

| Tool         | Config                            | Bazel Command                                   |
| ------------ | --------------------------------- | ----------------------------------------------- |
| Ruff         | `ruff.toml`                       | `bazel lint //...`                              |
| mypy         | `mypy.ini`                        | `bazel build --config=typecheck //...`          |
| ESLint       | `props/frontend/eslint.config.js` | `bazel lint //...`                              |
| Prettier     | `props/frontend/.prettierrc`      | `bazel test //props/frontend:prettier_test`     |
| svelte-check | `props/frontend/tsconfig.json`    | `bazel test //props/frontend:svelte_check_test` |
| buildifier   | `tools/lint/BUILD.bazel`          | `bazel run //tools/lint:buildifier`             |
| yamllint     | `.yamllint.yaml`                  | `bazel test //ansible:yamllint_test`            |
| nixfmt       | N/A                               | Pre-commit hook only                            |

Ruff uses a custom `rules_multitool` lockfile (`tools/multitool/lockfile.json`) to override the older version bundled in `aspect_rules_lint`. Mypy uses `rules_mypy` v0.40.0 with `follow_imports = silent` and `ignore_missing_imports = True`.

### Hook Lifecycle

**Git pre-commit** (via `.pre-commit-config.yaml`): safety checks, syntax validation, Ansible check, Bazel format/lint, ruff autofix, markdownlint, kubeconform, nixfmt.

**Claude Code session start** (via `.claude/settings.json`): installs Bazelisk, starts local auth proxy for Claude Code web's egress proxy, creates CA bundles + Java truststore, writes `~/.cache/bazel-proxy/bazelrc`, installs bazel wrapper, runs `pre-commit install`. Package is `tools/claude_hooks/`, entry point is `session_start.py`.

### Known Issues

- Python 3.13: watch for `datetime.datetime.utcnow()` deprecation warnings.
