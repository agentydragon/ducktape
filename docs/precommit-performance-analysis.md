# Pre-commit Performance Analysis

Analysis of pre-commit hook execution times and implemented optimizations.

## Current State (After Optimization)

| Hook               | Time | Notes                                                |
| ------------------ | ---- | ---------------------------------------------------- |
| bazel-precommit    | ~42s | 22s build + 14s parallel batches (16 batches)        |
| checkov            | ~13s | Terraform security scanner                           |
| terraform_validate | ~11s | Terraform validation                                 |
| terraform_tflint   | ~4s  | Terraform linting                                    |
| markdownlint-cli2  | ~3s  | Markdown linting                                     |
| other hooks        | ~5s  | YAML/AST/TOML checks, ruff, etc.                     |
| **Total**          | ~78s | Down from ~160s (parallel execution via script-path) |

Per-batch breakdown (runs in parallel):

- prettier: ~6s (100+ files)
- ruff: ~1s (60+ files)
- buildifier: ~0.2s
- kustomize/flux/gitops validators: ~4-5s each

## Key Optimizations

### 1. Unified Bazel Precommit (`tools/precommit/precommit.py`)

Combined format and validate into single Bazel binary to avoid client lock contention.
Previous setup with separate bazel-format and bazel-validate hooks caused ~55s each due
to Bazel client lock serialization.

**Formatters** (run in parallel via asyncio):

- prettier (JS/TS/CSS/MD/YAML/JSON)
- ruff (Python)
- shfmt (Shell)
- buildifier (Bazel)

**Validators** (run in parallel via asyncio):

- buildifier-lint
- pytest-main-check (direct import)
- terraform-centralization (direct import)
- kustomize/flux/gitops/helm/sealed-secrets (subprocess)

### 2. Fast File Listing

Replaced `pygit2.status()` (12s, stats every file) with `pygit2.index` (0.02s, reads index).

### 3. Script-path runner (`tools/precommit/run-precommit.sh`)

Uses `bazel run --script_path` to generate a runner script that executes without holding
the Bazel client lock. This allows pre-commit's parallel batch execution to work.

Key design:

- Uses PPID to identify all batches from one pre-commit invocation
- First batch generates the runner script, others wait via flock and reuse it
- Runners stored in `.git/precommit-runners/`

## Current Bottlenecks

### 1. Bazel build time (~22s)

First batch waits for `bazel run --script_path` to generate the runner script.
Subsequent batches wait via flock then reuse the same script.

**Potential**: Pre-generate runner script in CI warm cache.

### 2. Prettier (~6s per batch)

Dominates format time. 100+ files @ ~60ms/file.

### 3. Terraform Hooks (~28s combined)

- checkov: ~13s (security scanner)
- terraform_validate: ~11s (init + validate per module)
- terraform_tflint: ~4s

**Potential**: Combine into parallel script, cache init.

## Profiling

```bash
# Profile all hooks
time pre-commit run --all-files

# Profile bazel-precommit with detailed timing
PRECOMMIT_PROFILE=1 bazelisk run //tools/precommit

# Count bazel invocations (should be 2 due to batching)
pre-commit run bazel-precommit --all-files 2>&1 | grep -c "Running command line"
```

## Historical Context

Original times (before optimization):

- bazel-format: 55.7s
- bazel-validate: 55.7s
- Total: ~160s

Root cause: Bazel client lock contention when pre-commit ran hooks in parallel.
