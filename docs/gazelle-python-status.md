# Gazelle Python Integration Status

## Current State (January 2026)

Gazelle Python is **fully configured and operational**. The repository has ~95% Gazelle-compatible BUILD files using the per-file pattern.

### What Works

- `//tools:gazelle` target builds and runs successfully
- `//tools:modules_map` generates wheel metadata correctly
- `gazelle_python.yaml` manifest is populated with 500+ module mappings
- Go dependencies download without network issues
- System packages are filtered via `filter_wheels.bzl`

### Running Gazelle

```bash
# Preview changes
bazel run //tools:gazelle -- --mode=diff

# Apply changes
bazel run //tools:gazelle

# Update manifest after requirements changes
bazel run //tools:gazelle_python_manifest.update
```

## Configuration

1. **Dependencies** (`MODULE.bazel`):
   - `rules_python_gazelle_plugin` version 1.4.1
   - `gazelle` version 0.34.0

2. **Configuration Files**:
   - `gazelle_python.yaml` - generated manifest mapping imports to PyPI packages
   - `filter_wheels.bzl` - filters system packages (pygobject, dbus-python, pycairo)
   - `//tools:gazelle` target in `tools/BUILD.bazel`

3. **Directives** (in root `BUILD.bazel`):
   - `# gazelle:python_generation_mode file` - per-file targets
   - `# gazelle:exclude` for ansible, homeassistant, claude_hooks, k8s-old
   - `# gazelle:resolve` for internal packages with non-standard paths

## BUILD File Conventions

### Per-File Targets (Gazelle Pattern)

Each `.py` file should have its own `py_library` target with:

- `name` matching the file stem (e.g., `client.py` → `name = "client"`)
- `srcs` containing only that file
- `imports = [".."]` or appropriate path to enable package imports

### No Aggregator Targets

Don't bundle multiple files into a single `py_library`:

```python
# ❌ WRONG: Aggregator pattern
py_library(
    name = "my_package",
    srcs = ["client.py", "server.py", "utils.py"],
    ...
)

# ✓ CORRECT: Per-file pattern
py_library(name = "client", srcs = ["client.py"], ...)
py_library(name = "server", srcs = ["server.py"], ...)
py_library(name = "utils", srcs = ["utils.py"], ...)
```

### No `__init__` Targets

Don't create `py_library` targets for `__init__.py` files:

- Most `__init__.py` should not exist at all (Bazel generates stubs via `imports = [".."]`)
- If `__init__.py` exists, it usually shouldn't have its own target
- Exception: When `__init__.py` contains actual code (not just re-exports)

### Import From Definition, Not Re-exports

Depend on the target where code is defined, not where it's re-exported:

```python
# ❌ WRONG: Depending on re-export
from mypackage import MyClass  # if __init__.py re-exports from mypackage.client
deps = ["//mypackage"]  # or "//mypackage:__init__"

# ✓ CORRECT: Depend on the defining module
from mypackage.client import MyClass
deps = ["//mypackage:client"]
```

### BUILD Files Parallel to Sources

Each directory with `.py` files should have its own `BUILD.bazel`:

```
# ❌ WRONG: Root BUILD touching subdirectory files
adgn/BUILD.bazel:
    srcs = ["agent/cli.py", "testing/bootstrap.py"]

# ✓ CORRECT: BUILD files in each directory
adgn/BUILD.bazel           # only for files in adgn/
adgn/agent/BUILD.bazel     # for agent/cli.py
adgn/testing/BUILD.bazel   # for testing/bootstrap.py
```

### Minimal Visibility

Omit `visibility` when the default is sufficient. Only add explicit visibility when needed:

```python
# ❌ WRONG: Unnecessary visibility
py_library(
    name = "internal_helper",
    srcs = ["internal_helper.py"],
    visibility = ["//:__subpackages__"],  # Often not needed
)

# ✓ CORRECT: Omit when default works
py_library(
    name = "internal_helper",
    srcs = ["internal_helper.py"],
)

# ✓ CORRECT: Add only when actually needed
py_library(
    name = "public_api",
    srcs = ["public_api.py"],
    visibility = ["//visibility:public"],  # Intentionally public
)
```

Default visibility is package-private. Only broaden when:

- Target is used by other packages (use `//pkg:__subpackages__` or specific packages)
- Target is a public API (use `//visibility:public`)

## Completed Fixes

### agent_core_testing ✅

Removed aggregator target. Dependents updated to use specific targets:

- `:fixtures`, `:responses`, `:steps`, `:openai_mock`, etc.

### adgn ✅

Removed aggregator. Created BUILD files in subdirectories:

- `adgn/agent/BUILD.bazel` - `:cli`
- `adgn/gitea_pr_gate/BUILD.bazel` - `:policy_common`, `:policy_server_fastapi`
- `adgn/testing/BUILD.bazel` - `:bootstrap`
- `adgn/tools/BUILD.bazel` - `:trivial_patterns`

### props/backend ✅

Removed aggregator. Created `props/backend/routes/BUILD.bazel` with per-file targets:

- `:eval`, `:ground_truth`, `:llm`, `:registry`, `:runs`, `:stats`

Main backend targets: `:app`, `:auth`, `:cli`, `:export_schema`

### inop/engine ✅

Renamed `py_library` from `:optimizer` to `:optimizer_lib` to avoid conflict with Gazelle-generated `py_binary`.

## Summary

All known Gazelle blockers have been fixed. Gazelle can be used opportunistically:

1. Run `bazel run //tools:gazelle -- --mode=diff` to preview changes
2. Manually apply sensible changes
3. Fix any errors in excluded packages manually
