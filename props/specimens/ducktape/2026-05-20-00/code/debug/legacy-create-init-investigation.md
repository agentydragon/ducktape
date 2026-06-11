# legacy_create_init Investigation

## Summary — Resolved

The native Bazel flag `--incompatible_default_to_explicit_init_py` in `.bazelrc`
globally suppresses auto-generated `__init__.py` stubs in runfiles. This fixes
both protobuf namespace packages (`google.*`) and FreeCAD conda C extension
shadowing without per-target `legacy_create_init = 0` annotations.

## Background: What Stubs Do

With `legacy_create_init = 1` (rules_python default), Bazel generates empty
`__init__.py` stubs in runfiles for every directory containing `.py` or `.so`
files. This turns bare directories into Python packages.

**Why stubs exist**: Prevent stdlib shadowing. When `_main` is on `sys.path` and
`_main/x/claude_linter_v2/types.py` exists, the stub `__init__.py` in
`x/claude_linter_v2/` makes it a package, so `import types` resolves to stdlib
rather than the local `types.py`.

**Why stubs break things**:

1. **Protobuf namespace packages**: `google.*` uses PEP 420 namespace packages
   (no `__init__.py`). A generated stub at `google/__init__.py` makes Python
   treat `google` as a regular package rooted in one directory, preventing
   discovery of `google.devtools` from a different runfiles subtree.

2. **FreeCAD conda C extensions**: FreeCAD's conda env has `Part.so` (C
   extension) and `Mod/Part/` (directory). A stub `Mod/Part/__init__.py` shadows
   the C extension: `import Part` finds the empty package instead of `Part.so`.

## The Fix: Native Bazel Flag

```
common --incompatible_default_to_explicit_init_py
```

This is a **native Bazel flag** (not a rules_python Starlark config setting).
It tells `_should_create_init_files()` in `py_executable.bzl` to return `false`,
skipping the `merge_runfiles_with_generated_inits_empty_files_supplier` call
that generates stubs.

All per-target `legacy_create_init = 0` annotations are removed — the global
flag makes them redundant.

## Why the Starlark Config Setting Doesn't Work

The rules_python docs recommend:

```
common --@rules_python//python/config_settings:incompatible_default_to_explicit_init_py=true
```

**This has no effect in Bazel 8.** Here's why:

`_should_create_init_files()` calls `read_possibly_native_flag(ctx, "default_to_explicit_init_py")` (`py_executable.bzl:284`). This function checks
`hasattr(ctx.fragments, "py")` (`flags.bzl:65`) — which is `true` in Bazel 8 —
and reads from the **native** `ctx.fragments.py.default_to_explicit_init_py`
(defaults to `false`), ignoring the Starlark config setting entirely.

The Starlark flag only takes effect once Bazel enables
`--incompatible_remove_ctx_py_fragment` (expected in Bazel 9+). Until then,
use the native flag.

### Observed Symptoms (2026-04-12)

When using the Starlark flag, runfiles manifests showed 244 **more**
`__init__.py` entries (842 vs 598) because the config setting change caused
build configuration transitions and cache invalidation, producing different
(but still broken) runfiles without actually suppressing stubs.

## FreeCAD Runfiles Path Fix

As part of this change, `skills/freecad/conftest.py:_find_conda_root()` was
refactored to use `Rlocation` instead of manual `.runfiles` path walking. The
old code walked up to the `.runfiles` directory and constructed the conda env
path manually — fragile and breaks under alternative runfiles layouts (venv
site-packages, etc.). The new code Rlocates `bin/freecadcmd` directly inside
the conda env repo and derives the root from `anchor.parent.parent`.

## Future: `venvs_site_packages`

rules_python is moving toward `venvs_site_packages=yes` (pip deps laid out in a
real venv `site-packages/` directory). This is still experimental in rules_python
1.9.0 but tested to work for non-conda targets. Once stable, it makes
`--incompatible_default_to_explicit_init_py` redundant for pip deps — but the
native flag is still needed for non-pip source directories and conda envs.
