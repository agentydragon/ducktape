# Linting Architecture

This document describes the linting and formatting setup across pre-commit, Bazel, and CI.

## Quick Reference

| Tool                             | Pre-commit             | Bazel Aspect          | GitHub CI           |
| -------------------------------- | ---------------------- | --------------------- | ------------------- |
| **Python (ruff check)**          | `ruff-check` hook      | `--config=lint`       | Both                |
| **Python (ruff format)**         | `bazel-format` hook    | N/A                   | Pre-commit          |
| **Python (mypy)**                | -                      | `--config=typecheck`  | bazel-build         |
| **JS/TS (eslint)**               | -                      | `--config=lint`       | bazel-build         |
| **JS/TS (prettier)**             | `bazel-format` hook    | N/A                   | Pre-commit          |
| **Starlark (buildifier)**        | `buildifier-lint` hook | -                     | Pre-commit          |
| **Starlark (buildifier format)** | `bazel-format` hook    | N/A                   | Pre-commit          |
| **Rust (clippy)**                | -                      | `--config=rust-check` | bazel-build         |
| **Shell (shfmt)**                | `bazel-format` hook    | N/A                   | Pre-commit          |
| **Nix (alejandra)**              | `alejandra` hook       | N/A                   | Pre-commit          |
| **Ansible**                      | syntax-check (fast)    | -                     | ansible-lint (full) |
| **Terraform**                    | fmt/validate/tflint    | -                     | Pre-commit          |

## Configuration Files

### Single Source of Truth

| Tool       | Config File                       | Used By                        |
| ---------- | --------------------------------- | ------------------------------ |
| ruff       | `/ruff.toml`                      | Pre-commit hook, Bazel aspects |
| mypy       | `/mypy.ini`                       | Bazel aspects                  |
| eslint     | `/eslint.config.js`               | Bazel aspects                  |
| buildifier | `@buildifier_prebuilt` defaults   | Pre-commit, Bazel              |
| prettier   | `/prettier.config.js` (if exists) | `//tools/format`               |

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

Defined in `.bazelrc`:

```bash
# Lint: ruff + eslint
bazel build --config=lint //...

# Type check: mypy
bazel build --config=typecheck //...

# Combined: ruff + mypy (no eslint - see note)
bazel build --config=check //...

# Rust: clippy + rustfmt
bazel build --config=rust-check //finance/...

# ESLint only
bazel build --config=eslint //props/frontend:all
```

Aspect definitions in `tools/lint/linters.bzl`:

- `ruff` - Python linting via `@multitool//tools/ruff`
- `mypy_aspect` - Type checking via `//tools/lint:mypy_cli`
- `eslint` - JS/TS linting via `//tools/lint:eslint`

## GitHub CI Workflows

| Workflow                | What Runs                                              |
| ----------------------- | ------------------------------------------------------ |
| `pre-commit.yml`        | `pre-commit run --all-files`                           |
| `bazel-build.yml`       | `bazel build --config=check //...`, `bazel test //...` |
| `ansible-lint.yml`      | Full ansible-lint (thorough mode)                      |
| `visual-regression.yml` | `bazel test //props/frontend:visual_test`              |

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
- **buildifier** - Starlark/BUILD files

The formatter respects `.gitattributes` exclusions (`rules-lint-ignored=true`).

## Pre-commit Hooks

Key hooks in `.pre-commit-config.yaml`:

| Hook                | Source                       | Purpose            |
| ------------------- | ---------------------------- | ------------------ |
| `ruff-check`        | astral-sh/ruff-pre-commit    | Python linting     |
| `buildifier-lint`   | keith/pre-commit-buildifier  | Starlark linting   |
| `bazel-format`      | local (Bazel)                | Unified formatting |
| `alejandra`         | local (Nix)                  | Nix formatting     |
| `markdownlint-cli2` | DavidAnson/markdownlint-cli2 | Markdown linting   |

Cluster-specific hooks run only on `cluster/` files:

- `kubeconform` - K8s manifest validation
- `terraform_fmt`, `terraform_validate`, `terraform_tflint`
- `checkov` - Terraform security analysis

## Version Management

Pre-commit uses external tool versions for some hooks:

- `ruff-check`: from `astral-sh/ruff-pre-commit`
- `buildifier-lint`: from `keith/pre-commit-buildifier`

Bazel uses managed versions:

- ruff: `@multitool//tools/ruff`
- buildifier: `@buildifier_prebuilt//buildifier`

The formatting hook (`bazel-format`) uses Bazel-managed versions, ensuring consistency.

### Known Gaps

See `TODO.md` for tracked items. Current gaps:

1. **Version drift risk**: Pre-commit uses external ruff/buildifier versions that may differ from Bazel-managed versions. A unified linter script (like `//tools/format`) would eliminate this.

2. **ESLint not in pre-commit**: JS/TS linting only runs in CI via Bazel aspects, not locally during commit.

3. **mypy not in pre-commit**: Type checking only runs in CI via Bazel aspects, not locally during commit.

## Adding New Linters

1. **For Python/JS/Rust**: Add aspect to `tools/lint/linters.bzl`
2. **For other languages**: Add to `.pre-commit-config.yaml`
3. **Update this document**
