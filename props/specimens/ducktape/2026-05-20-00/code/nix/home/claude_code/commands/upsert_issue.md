---
description: Capture a code quality issue in the specimens repository (user)
---

Capture a code quality issue in the specimens repository at `props/specimens/`.

## First: Read the Specimens Documentation

**Before creating any issue, read the local documentation:**

- `props/specimens/AGENTS.md` - Main guide
- `props/specimens/docs/format_spec.md` - YAML format specification
- `props/specimens/docs/authoring_guide.md` - How to write good issues

These docs are the source of truth for the current format (YAML, not libsonnet).

## Workflow

1. **Determine current repo**: `basename $(git rev-parse --show-toplevel)`
2. **Find latest snapshot**: `ls -1d props/specimens/$REPO_NAME/20* | tail -1`
3. **Check if issue exists**: Search `issues/*.yaml` in that snapshot
4. **If not documented**: Create new `.yaml` file in `<snapshot>/issues/`
5. **Verify YAML syntax**: `python3 -c "import yaml; yaml.safe_load(open('path/to/issue.yaml'))"`

## Key Points

- Issues go in `<snapshot>/issues/<slug>.yaml` (not at snapshot root)
- Snapshots are immutable - never update to mark issues resolved
- File paths must match bundle structure (check `manifest.yaml`)
- Don't create snapshots yourself - ask user if one is needed

## If Snapshot is Stale

If the file you're documenting doesn't exist in the latest snapshot:

```
Cannot upsert - file not in latest snapshot.
Latest: <snapshot-slug>
A new snapshot is needed to capture current state.
```
