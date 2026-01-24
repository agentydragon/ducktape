# CI Decision Engine Design

## Architecture

The CI system uses a declarative workflow manifest (`workflows.yaml`) as the single source of truth for workflow triggers. The decision engine (`ci_decide.py`) computes affected Bazel targets and determines which workflows to run.

```
workflows.yaml          ci_decide.py              ci.yml
     │                       │                      │
     │  trigger rules        │  bazel-diff         │  contains(workflows, 'name')
     └──────────────────────►│◄─────────────────────┤
                             │                      │
                             │  outputs:            │
                             │  - targets           │
                             │  - workflows (JSON)  │
                             │  - infra_changed     │
                             └──────────────────────►
```

## Components

### `workflows.yaml` - Trigger Definitions

Declares when each workflow runs:

```yaml
# Bazel-pattern triggers: uses bazel query intersection
bazel-build:
  bazel_pattern: "//..."
  receives_targets: true

props-e2e-test:
  bazel_pattern: "//props/..."

# Path-pattern triggers: regex match on changed files
nix-flake-check:
  path_pattern: "^nix/"

# Always-run workflows
pre-commit:
  always: true
```

**Automatic workflow file detection**: If `.github/workflows/foo.yml` changes, the `foo` workflow is triggered (if defined in the manifest). No explicit mapping needed.

### `ci_decide.py` - Decision Engine

1. Loads `workflows.yaml`
2. Computes base SHA (merge-base for PRs, HEAD~1 for pushes)
3. Gets changed files via `git diff`
4. Runs `bazel-diff` to compute affected Bazel targets
5. Evaluates trigger rules against changes
6. Outputs:
   - `targets`: Space-separated Bazel targets (or `//...` on infra change)
   - `workflows`: JSON array of workflow names to run
   - `infra_changed`: Boolean flag for infrastructure changes

### `ci.yml` - Workflow Dispatch

Uses `contains(fromJson(...), 'name')` for each job's `if:` condition:

```yaml
jobs:
  compute-targets:
    outputs:
      workflows: ${{ steps.decide.outputs.workflows }}
      targets: ${{ steps.decide.outputs.targets }}

  bazel-build:
    needs: compute-targets
    if: contains(fromJson(needs.compute-targets.outputs.workflows), 'bazel-build')
    uses: ./.github/workflows/bazel-build.yml
    with:
      targets: ${{ needs.compute-targets.outputs.targets }}
```

## Key Design Decisions

### Always compute specific targets

Unlike the previous design that used `//...` for push events, the new system always runs `bazel-diff` to compute exactly affected targets. This improves:

- Cache hit rates
- Build times
- Resource usage

The only exceptions that trigger `//...`:

- Infrastructure file changes (MODULE.bazel, requirements_bazel.txt, etc.)
- bazel-diff failures (graceful fallback)
- Missing base SHA (new branch)

### Single source of truth

All trigger logic lives in `workflows.yaml`. Adding a new conditional workflow:

1. Add entry to `workflows.yaml`
2. Add job to `ci.yml` with `contains()` check

No need to touch multiple Python dicts, bash scripts, or YAML conditions.

### Automatic workflow file triggers

When `.github/workflows/foo.yml` changes, the `foo` workflow runs automatically. This is derived from naming convention, not explicit mapping.

## Infrastructure Files

These patterns trigger `//...` (full build) since they can affect any target:

- `MODULE.bazel`, `MODULE.bazel.lock`
- `requirements_bazel.txt`
- `.bazelrc`, `.bazelversion`
- `tools/bazel*`
- `WORKSPACE*`

## Adding a New Workflow

1. Create `.github/workflows/my-workflow.yml`
2. Add to `workflows.yaml`:
   ```yaml
   my-workflow:
     bazel_pattern: "//my-package/..." # or path_pattern, or always
   ```
3. Add job to `ci.yml`:
   ```yaml
   my-workflow:
     needs: compute-targets
     if: contains(fromJson(needs.compute-targets.outputs.workflows), 'my-workflow')
     uses: ./.github/workflows/my-workflow.yml
   ```
