# CI Decision Engine Design

## Architecture

The CI system uses a declarative workflow manifest (`workflows.yaml`) as the single source of truth. Two scripts consume this manifest:

1. **`generate_ci.py`** - Generates `.github/workflows/ci.yml` from the manifest
2. **`ci_decide.py`** - Computes affected targets and which workflows to run at CI time

```
workflows.yaml
     │
     ├──► generate_ci.py ──► ci.yml (committed)
     │
     └──► ci_decide.py (at CI time)
              │
              └──► outputs: targets, workflows (JSON), infra_changed
```

## Components

### `workflows.yaml` - Single Source of Truth

Defines all workflows with discriminated union triggers:

```yaml
workflows:
  bazel-build:
    trigger: { kind: bazel, pattern: "//..." }
    targets: true
    secrets: [BUILDBUDDY_API_KEY]

  props-e2e-test:
    trigger: { kind: bazel, pattern: "//props/..." }
    secrets: [BUILDBUDDY_API_KEY]

  nix-flake-check:
    trigger: { kind: path, pattern: "^nix/" }

  pre-commit:
    trigger: { kind: always }
```

### `generate_ci.py` - CI YAML Generator

Generates `ci.yml` from `workflows.yaml` using proper YAML serialization:

```bash
python tools/ci/generate_ci.py        # Generate ci.yml
python tools/ci/generate_ci.py --check  # Verify ci.yml is up to date
```

The generated `ci.yml` includes:

- `compute-targets` job (downloads bazel-diff, runs ci_decide.py)
- One job per workflow with `contains(fromJson(...))` condition

### `ci_decide.py` - Runtime Decision Engine

At CI time, computes:

1. Base SHA (merge-base for PRs, HEAD~1 for pushes)
2. Changed files via `git diff`
3. Affected Bazel targets via `bazel-diff`
4. Which workflows to run based on trigger rules

Outputs (to `$GITHUB_OUTPUT`):

- `targets`: Space-separated Bazel targets (or `//...` on infra change)
- `workflows`: JSON array of workflow names
- `infra_changed`: Boolean flag

## Trigger Types

| Type            | Description                                      |
| --------------- | ------------------------------------------------ |
| `bazel_pattern` | Bazel query pattern (uses set intersection)      |
| `path_pattern`  | Regex pattern matched against changed files      |
| `always`        | Always run this workflow                         |
| (automatic)     | Workflow file changes trigger their own workflow |

## Infrastructure Files

These patterns trigger `//...` (full build):

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
     bazel_pattern: "//my-package/..."
     secrets: [BUILDBUDDY_API_KEY]
   ```
3. Regenerate ci.yml:
   ```bash
   python tools/ci/generate_ci.py
   ```
4. Commit both `workflows.yaml` and `ci.yml`
