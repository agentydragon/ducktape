# Linting Architecture

This document describes the linting and formatting setup across pre-commit, Bazel, and CI.

## Quick Reference

| Tool                             | Pre-commit             | Bazel Aspect | GitHub CI           |
| -------------------------------- | ---------------------- | ------------ | ------------------- |
| **Python (ruff check)**          | `ruff-check` hook      | default      | Both                |
| **Python (ruff format)**         | `ruff-format` hook     | N/A          | Pre-commit          |
| **Python (mypy)**                | -                      | default      | bazel-check         |
| **JS/TS (eslint)**               | -                      | default      | bazel-check         |
| **JS/TS (prettier)**             | `prettier` hook        | N/A          | Pre-commit          |
| **Starlark (buildifier)**        | `buildifier-lint` hook | -            | Pre-commit          |
| **Starlark (buildifier format)** | `buildifier` hook      | N/A          | Pre-commit          |
| **Rust (clippy)**                | -                      | default      | bazel-check         |
| **Rust (rustfmt)**               | `rustfmt` hook         | default      | Both                |
| **Shell (shfmt)**                | `bazel-precommit` hook | N/A          | Pre-commit          |
| **Nix (nixfmt)**                 | `nixfmt` hook          | N/A          | Pre-commit          |
| **Ansible**                      | syntax-check (fast)    | -            | ansible-lint (full) |
| **Terraform**                    | fmt/validate/tflint    | -            | Pre-commit          |

## Configuration Files

### Single Source of Truth

| Tool       | Config File                     | Used By                        |
| ---------- | ------------------------------- | ------------------------------ |
| ruff       | `/ruff.toml`                    | Pre-commit hook, Bazel aspects |
| mypy       | `/mypy.ini`                     | Bazel aspects                  |
| eslint     | `/eslint.config.js`             | Bazel aspects                  |
| buildifier | `@buildifier_prebuilt` defaults | Pre-commit, Bazel              |
| prettier   | `/.prettierrc.cjs`              | `//tools/format`               |

**Do not add `[tool.ruff]` or `[tool.mypy]` to package-level `pyproject.toml` files.**

### Exclusion Patterns

Exclusions are defined in multiple places:

| File                               | Scope                                                |
| ---------------------------------- | ---------------------------------------------------- |
| `.pre-commit-config.yaml` (line 3) | Global pre-commit exclusions                         |
| `.gitattributes`                   | Format/lint exclusions via `rules-lint-ignored=true` |
| `ruff.toml`                        | Ruff-specific exclusions                             |
| `mypy.ini`                         | Mypy-specific exclusions                             |
| `eslint.config.js`                 | ESLint ignores                                       |

Common exclusion patterns (should match across files):

- `**/third_party/**`
- `**/testdata/**`
- `**/fixtures/**`
- `**/vendor/**`
- `**/node_modules/**`

## Bazel Aspect Configs

Defined in `.bazelrc`. Lint runs by default on every `bazel build`.

```bash
# Lint runs by default (ruff + eslint + mypy + clippy + rustfmt):
bazel build //...

# Skip lint for faster iterative builds:
bazel build --config=nolint //...
```

Aspect definitions in `tools/lint/linters.bzl`:

- `ruff` - Python linting via `@multitool//tools/ruff`
- `mypy_aspect` - Type checking via `//tools/lint:mypy_cli`
- `eslint` - JS/TS linting via `//tools/lint:eslint`

## GitHub CI Workflows

| Workflow           | What Runs                                       |
| ------------------ | ----------------------------------------------- |
| `pre-commit.yml`   | `pre-commit run --all-files`                    |
| `bazel-check.yml`  | `bazel build //...` (lint runs by default)      |
| `ansible-lint.yml` | Full ansible-lint (thorough mode)               |
| `bazel-test.yml`   | `bazel test //...` (includes visual regression) |

## Formatting

Formatting is unified through `//tools/format`:

```bash
# Format all tracked files
bazel run //tools/format

# Format specific files
bazel run //tools/format -- file1.py file2.js
```

Formatters included:

- **prettier** - JS/TS, CSS, HTML, Markdown, YAML, JSON
- **ruff format** - Python
- **shfmt** - Shell scripts

The formatter respects `.gitattributes` exclusions (`rules-lint-ignored=true`).

Note: **buildifier** is managed separately via `keith/pre-commit-buildifier` hooks.

## Pre-commit Hooks

Key hooks in `.pre-commit-config.yaml`:

| Hook                | Source                       | Purpose                        |
| ------------------- | ---------------------------- | ------------------------------ |
| `ruff-check`        | astral-sh/ruff-pre-commit    | Python linting                 |
| `ruff-format`       | astral-sh/ruff-pre-commit    | Python formatting              |
| `buildifier`        | keith/pre-commit-buildifier  | Starlark formatting            |
| `buildifier-lint`   | keith/pre-commit-buildifier  | Starlark linting               |
| `bazel-precommit`   | local (Bazel)                | Shell formatting + validations |
| `prettier`          | local (node)                 | JS/TS/MD/YAML formatting       |
| `rustfmt`           | local (system)               | Rust formatting                |
| `nixfmt`            | local (static binary)        | Nix formatting                 |
| `markdownlint-cli2` | DavidAnson/markdownlint-cli2 | Markdown linting               |

Cluster-specific hooks run only on `cluster/` files:

- `kubeconform` - K8s manifest validation
- `terraform_fmt`, `terraform_validate`, `terraform_tflint`
- `checkov` - Terraform security analysis

## Version Management

Pre-commit uses external tool versions for some hooks:

- `ruff-check`/`ruff-format`: from `astral-sh/ruff-pre-commit`
- `buildifier`/`buildifier-lint`: from `keith/pre-commit-buildifier`

Bazel uses managed versions:

- ruff: `@multitool//tools/ruff` (used by `//tools/format`)
- buildifier: `@buildifier_prebuilt//buildifier` (used by `//tools/format`, `//tools/lint`)
- shfmt: `@aspect_rules_lint//format:shfmt` (used by `//tools/precommit`, `//tools/format`)

The `bazel-precommit` hook uses Bazel-managed shfmt version for shell formatting.

### Known Gaps

See `TODO.md` for tracked items. Current gaps:

1. **Version drift risk**: Pre-commit uses external ruff/buildifier versions that may differ from Bazel-managed versions. TODO in `.pre-commit-config.yaml` tracks the buildifier version sync issue.

2. **ESLint not in pre-commit**: JS/TS linting only runs in CI via Bazel aspects, not locally during commit.

3. **mypy not in pre-commit**: Type checking only runs in CI via Bazel aspects, not locally during commit.

## Adding New Linters

1. **For Python/JS/Rust**: Add aspect to `tools/lint/linters.bzl`
2. **For other languages**: Add to `.pre-commit-config.yaml`
3. **Update this document**
