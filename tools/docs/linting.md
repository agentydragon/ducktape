# Linting Architecture

This document describes the linting and formatting setup across pre-commit, Bazel, and CI.

## Quick Reference

| Tool                             | Pre-commit              | Bazel Aspect | GitHub CI           |
| -------------------------------- | ----------------------- | ------------ | ------------------- |
| **Python (ruff check)**          | `ruff-check` hook       | default      | Both                |
| **Python (ruff format)**         | `ruff-format` hook      | N/A          | Pre-commit          |
| **Python (mypy)**                | -                       | default      | bazel-check         |
| **JS/TS (eslint)**               | -                       | default      | bazel-check         |
| **JS/TS (prettier)**             | `prettier` hook         | N/A          | Pre-commit          |
| **Starlark (buildifier)**        | `buildifier-lint` hook  | -            | Pre-commit          |
| **Starlark (buildifier format)** | `buildifier` hook       | N/A          | Pre-commit          |
| **Rust (clippy)**                | -                       | default      | bazel-check         |
| **Rust (rustfmt)**               | `fmt` hook              | default      | Both                |
| **Shell (shfmt)**                | `shfmt` hook            | N/A          | Pre-commit          |
| **Nix (nixfmt)**                 | `nixfmt` hook           | N/A          | Pre-commit          |
| **Ansible**                      | syntax-check (fast)     | -            | ansible-lint (full) |
| **Terraform (tflint/validate)**  | `checkov_diff`/`tflint` | Bazel tests  | Both                |

## Configuration Files

### Single Source of Truth

| Tool       | Config File                     | Used By                        |
| ---------- | ------------------------------- | ------------------------------ |
| ruff       | `/ruff.toml`                    | Pre-commit hook, Bazel aspects |
| mypy       | `/mypy.ini`                     | Bazel aspects                  |
| eslint     | `/eslint.config.js`             | Bazel aspects                  |
| buildifier | `@buildifier_prebuilt` defaults | Pre-commit, Bazel              |
| prettier   | `/.prettierrc.cjs`              | Pre-commit `prettier` hook     |

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

Formatting is handled by pre-commit hooks (run automatically on `git commit`):

- **prettier** - JS/TS, CSS, HTML, Markdown, YAML, JSON (local node hook)
- **ruff format** - Python (`astral-sh/ruff-pre-commit`)
- **shfmt** - Shell scripts (`scop/pre-commit-shfmt`)
- **buildifier** - Starlark (`keith/pre-commit-buildifier`)

All hooks respect `.gitattributes` and `.pre-commit-config.yaml` exclusions.

## Pre-commit Hooks

Key hooks in `.pre-commit-config.yaml`:

| Hook                | Source                       | Purpose                  |
| ------------------- | ---------------------------- | ------------------------ |
| `ruff-check`        | astral-sh/ruff-pre-commit    | Python linting           |
| `ruff-format`       | astral-sh/ruff-pre-commit    | Python formatting        |
| `buildifier`        | keith/pre-commit-buildifier  | Starlark formatting      |
| `buildifier-lint`   | keith/pre-commit-buildifier  | Starlark linting         |
| `bazel-precommit`   | local (Bazel)                | Validations only         |
| `prettier`          | local (node)                 | JS/TS/MD/YAML formatting |
| `fmt`               | doublify/pre-commit-rust     | Rust formatting          |
| `nixfmt`            | local (static binary)        | Nix formatting           |
| `markdownlint-cli2` | DavidAnson/markdownlint-cli2 | Markdown linting         |

Cluster-specific hooks run only on `cluster/` files:

- `kubeconform` - K8s manifest validation
- `checkov_diff` - Terraform security analysis
- `tflint` - Terraform linting

Terraform validation and linting are also covered by Bazel `tf_module` test targets (`rules_tf`).

## Version Management

Pre-commit uses external tool versions for some hooks:

- `ruff-check`/`ruff-format`: from `astral-sh/ruff-pre-commit`
- `buildifier`/`buildifier-lint`: from `keith/pre-commit-buildifier`

Bazel uses managed versions:

- buildifier: `@buildifier_prebuilt//buildifier` (used by `//tools/lint`)

### Known Gaps

See `TODO.md` for tracked items. Current gaps:

1. **Version drift risk**: Pre-commit uses external ruff/buildifier versions that may differ from Bazel-managed versions. TODO in `.pre-commit-config.yaml` tracks the buildifier version sync issue.

2. **ESLint not in pre-commit**: JS/TS linting only runs in CI via Bazel aspects, not locally during commit.

3. **mypy not in pre-commit**: Type checking only runs in CI via Bazel aspects, not locally during commit.

## Adding New Linters

1. **For Python/JS/Rust**: Add aspect to `tools/lint/linters.bzl`
2. **For other languages**: Add to `.pre-commit-config.yaml`
3. **Update this document**
