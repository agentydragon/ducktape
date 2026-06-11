# Ducktape

My personal infrastructure's duct tape. Projects that didn't yet warrant making
into separate repositories.

## Development

This repository uses **Bazel** as the primary build system. Install the git pre-commit hook:

```bash
pre-commit install
```

This installs the pre-commit framework which runs ruff, buildifier, prettier and other linters on staged files, checks for conflict markers, validates syntax, and more (see `.pre-commit-config.yaml`). For ESLint and mypy, run `bazel build --config=check //...`.

### Lint/Format Exclusions

Files excluded from linting and formatting are controlled in two places:

| File                | Purpose                            | Read by                  |
| ------------------- | ---------------------------------- | ------------------------ |
| `.gitattributes`    | Source of truth for all exclusions | `tools/format/format.py` |
| `ruff.toml exclude` | Must mirror Python patterns        | ruff check (lint aspect) |

**Why two files?** Our `format.py` reads `.gitattributes` (via `git check-attr`), but ruff's linter only reads `ruff.toml`. For Python files, patterns must exist in both.

To exclude a file/directory:

1. Add `path/** rules-lint-ignored=true` to `.gitattributes`
2. If it contains Python, also add to `ruff.toml exclude`

## License

AGPL 3.0

## Updates

To update Python requirements lock:

```bash
bazel run //:requirements.update
```

To format Bazel configuration files:

```bash
bazel run //tools:buildifier
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
