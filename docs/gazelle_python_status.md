# Gazelle Python Integration Status

## Current State (January 2026)

Gazelle Python is **fully configured and operational**. The repository has ~95% Gazelle-compatible BUILD files using the per-file pattern.

### What Works

- `//devinfra:gazelle` target builds and runs successfully
- `//tools:modules_map` generates wheel metadata correctly
- `devinfra/gazelle_python.yaml` manifest is populated with 500+ module mappings
- Go dependencies download without network issues
- System packages are filtered via `devinfra/filter_wheels.bzl`

### Running Gazelle

```bash
# Preview changes
bazel run //devinfra:gazelle -- --mode=diff

# Apply changes
bazel run //devinfra:gazelle

# Update manifest after requirements changes
bazel run //devinfra:gazelle_python_manifest.update
```

## Configuration

1. **Dependencies** (`MODULE.bazel`):
   - `rules_python_gazelle_plugin` version 1.9.0
   - `gazelle` version 0.47.0

2. **Configuration Files**:
   - `devinfra/gazelle_python.yaml` - generated manifest mapping imports to PyPI packages
   - `devinfra/filter_wheels.bzl` - filters system packages (pygobject, dbus-python, pycairo)
   - `//devinfra:gazelle` target in `devinfra/BUILD.bazel`

3. **Directives** (in root `BUILD.bazel`):
   - `# gazelle:python_generation_mode file` - per-file targets
   - `# gazelle:exclude` for ansible, homeassistant, claude_hooks
   - `# gazelle:resolve` for internal packages with non-standard paths

## BUILD File Conventions

See root <../README.md> (Gazelle section) and <../STYLE.md> for per-file target,
aggregator, `__init__`, import, and visibility conventions.

## Completed Fixes

### agent_core_testing ✅

Removed aggregator target. Dependents updated to use specific targets:

- `:fixtures`, `:responses`, `:steps`, `:openai_mock`, etc.

### props/backend ✅

Removed aggregator. Created `props/backend/routes/BUILD.bazel` with per-file targets:

- `:eval`, `:ground_truth`, `:llm`, `:registry`, `:runs`, `:stats`

Main backend targets: `:app`, `:auth`, `:cli`, `:export_schema`

### x/inop/engine ✅

Renamed `py_library` from `:optimizer` to `:optimizer_lib` to avoid conflict with Gazelle-generated `py_binary`.

## Summary

All known Gazelle blockers have been fixed. Gazelle can be used opportunistically:

1. Run `bazel run //devinfra:gazelle -- --mode=diff` to preview changes
2. Manually apply sensible changes
3. Fix any errors in excluded packages manually
