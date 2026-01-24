# CI Decision Engine Design

## Current State

The CI system uses `bazel_diff.py` to compute affected targets and set boolean flags:

- `targets`: space-separated list or "//..." for full build
- `has_changes`, `has_props`, `has_editor_agent`, etc.

Problems:

1. **Scattered logic**: Trigger decisions split between Python (Bazel targets), bash (path patterns), and YAML (conditionals)
2. **Full builds on push**: Non-PR pushes always build "//..." instead of computing specific targets
3. **Hardcoded mappings**: `WORKFLOW_TRIGGERS` and `PATH_PATTERNS` must stay in sync with ci.yml
4. **Boolean explosion**: Each new conditional workflow needs a new flag and corresponding YAML condition

## Proposed Design

### Single Source of Truth

Define all workflow trigger rules in Python:

```python
# tools/ci/workflows.py
WORKFLOWS = {
    "bazel-build": {
        "triggers": {
            "bazel_pattern": "//...",  # Any Bazel target
        },
        "receives_targets": True,  # Pass affected targets
    },
    "bazel-test": {
        "triggers": {
            "bazel_pattern": "//...",
        },
        "receives_targets": True,
    },
    "props-e2e-test": {
        "triggers": {
            "bazel_pattern": "//props/...",
            "workflow_files": [".github/workflows/props-e2e-test.yml"],
        },
    },
    "editor-e2e-test": {
        "triggers": {
            "bazel_pattern": "//editor_agent/...",
            "workflow_files": [".github/workflows/editor-e2e-test.yml"],
        },
    },
    "nix-flake-check": {
        "triggers": {
            "path_patterns": ["^nix/"],
            "workflow_files": [".github/workflows/nix-flake-check.yml"],
        },
    },
    "ansible-lint": {
        "triggers": {
            "path_patterns": ["^ansible/"],
            "workflow_files": [".github/workflows/ansible-lint.yml"],
        },
    },
}
```

### Output Format

Instead of individual boolean flags, output:

```
# Build/test targets (always specific, never "//...")
build_targets=//props:lib //adgn:lib ...
test_targets=//props:test_foo //adgn:test_bar ...

# Workflows to run (JSON array)
workflows=["bazel-build","bazel-test","props-e2e-test","nix-flake-check"]
```

### ci.yml Changes

Use matrix strategy with dynamic includes:

```yaml
jobs:
  compute-targets:
    outputs:
      build_targets: ${{ steps.decide.outputs.build_targets }}
      test_targets: ${{ steps.decide.outputs.test_targets }}
      workflows: ${{ steps.decide.outputs.workflows }}
    steps:
      - run: python tools/ci/ci_decision.py
        id: decide

  # Dynamic job dispatch using matrix
  run-workflows:
    needs: compute-targets
    if: needs.compute-targets.outputs.workflows != '[]'
    strategy:
      matrix:
        workflow: ${{ fromJson(needs.compute-targets.outputs.workflows) }}
    uses: ./.github/workflows/${{ matrix.workflow }}.yml
    with:
      targets: ${{ needs.compute-targets.outputs.build_targets }}
```

### Benefits

1. **Single source of truth**: All trigger logic in Python
2. **Always specific targets**: Better cache utilization, faster builds
3. **Extensible**: Add new workflows by editing Python config, not YAML
4. **Testable**: Python config can have unit tests
5. **Self-documenting**: `WORKFLOWS` dict shows all trigger rules at a glance

### Migration Path

1. Add `tools/ci/workflows.py` with workflow config
2. Add `tools/ci/ci_decision.py` that uses config to compute outputs
3. Update ci.yml to use new outputs
4. Remove old boolean flags and bash path-checking

### Open Questions

1. Should we use `gh workflow run` to dispatch workflows from Python instead of matrix strategy?
2. How to handle workflows that need specific secrets passed?
3. Should pre-commit always run, or should it also be in the workflow list?
