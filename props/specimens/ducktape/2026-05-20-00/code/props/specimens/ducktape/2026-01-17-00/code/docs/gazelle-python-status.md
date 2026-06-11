# Gazelle Python Integration Status

## Current State

Partial Gazelle Python configuration has been added to the repository, but full integration blocked by environment limitations.

## What Was Added

1. **Dependencies** (`MODULE.bazel`):
   - `rules_python_gazelle_plugin` version 1.4.1
   - `gazelle` version 0.34.0

2. **Configuration Files**:
   - `gazelle_python.yaml` - marker file for Gazelle root
   - `filter_wheels.bzl` - helper to filter system packages
   - `//:gazelle` target in root `BUILD.bazel`

3. **Known Issues**:
   - Network blocks prevent downloading Gazelle's Go dependencies (`com_github_dougthor42_go_tree_sitter`)
   - System packages (pygobject, dbus-python, pycairo) require libraries unavailable in Bazel sandbox

## Blockers

### 1. Network/Proxy Issues

Gazelle's Go dependencies fail to download:

```
dial tcp: lookup storage.googleapis.com: connection refused
```

### 2. System Package Build Failures

Packages requiring system libraries:

- `pygobject` → needs `girepository-2.0`
- `dbus-python` → needs `dbus-1`
- `pycairo` → needs `cairo`

These fail during `modules_mapping` wheel metadata extraction.

## Alternative Approaches

### Option 1: Manual BUILD Management (Current State)

Continue maintaining BUILD.bazel files manually. This works well and has low overhead given:

- Most BUILD files are already well-structured
- Type checking via mypy aspect provides dependency validation
- Lint checks catch common issues

### Option 2: Local Gazelle Application

Run Gazelle on a local development machine (not in Claude Code web):

```bash
# On local machine with network access:
bazel run //:gazelle_python_manifest.update
bazel run //:gazelle
git commit -am "Apply gazelle to Python packages"
git push
```

### Option 3: Gazelle with Partial Manifest

Use Gazelle without full manifest generation:

- Skip `modules_mapping` and `gazelle_python_manifest`
- Use `# gazelle:ignore` directives for system packages
- Manually manage dependencies for packages not in manifest

This approach works but reduces automation benefits.

### Option 4: Remove Gazelle Configuration

Keep the repository as-is with manual BUILD files. The current approach:

- Works reliably in all environments
- Has good IDE support
- Bazel's aspect system provides linting/type checking
- Dependencies are explicit and traceable

## Recommendation

Given the blockers and the repository's current good state, **Option 4 (remove Gazelle config)** or **Option 2 (local application)** are most practical.

If Gazelle is desired:

1. Apply it from a local dev machine with full network access
2. Check in the generated BUILD files
3. Use Gazelle for new packages, keep existing ones manual

## Files to Remove (if not proceeding with Gazelle)

- `gazelle_python.yaml`
- `filter_wheels.bzl`
- Gazelle-related sections in `/BUILD.bazel`
- Gazelle dependencies from `MODULE.bazel`
