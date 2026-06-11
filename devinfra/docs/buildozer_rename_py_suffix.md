# Renaming `*_py` Bazel targets via buildozer

This monorepo historically suffixed `py_library` targets with `_py` (e.g.
`augur/model:schemas_py`). The suffix is redundant in an all-Python package
and noise in `deps =` lists. The convention going forward is:

- **Drop `_py` from `py_library` target names.** A target backing
  `schemas.py` is just `:schemas`.
- **Use `_lib` only when needed for disambiguation** — typically when a
  `py_binary` or `py_test` in the same package would otherwise collide with
  the library's bare name. Example: `augur/api/BUILD.bazel` has both a
  `py_binary(name = "server")` and a `py_library(name = "server_lib")`.

`py_binary` and `py_test` targets already follow this convention (no `_py`
suffix), so they don't need touching.

## Tooling

[`buildozer`](https://github.com/bazelbuild/buildtools) edits BUILD files
in-place. The Nix devshell (`.envrc`) provides it via
`bazel-buildtools`; if you don't have direnv loaded, run
`nix shell nixpkgs#bazel-buildtools` or invoke the binary directly:
`/nix/store/.../bazel-buildtools-*/bin/buildozer`.

## Recipe per target

For each `py_library` named `foo_py` in package `//<pkg>`, three buildozer
calls are needed:

```bash
# 1. Rename the target declaration.
buildozer 'set name foo' //<pkg>:foo_py

# 2. Rewrite same-package short-form deps (":foo_py" → ":foo").
buildozer 'replace deps :foo_py :foo' '//<pkg>:*'

# 3. Rewrite cross-package long-form deps ("//<pkg>:foo_py" → "//<pkg>:foo").
buildozer 'replace deps //<pkg>:foo_py //<pkg>:foo' '//...:*'
```

Step 1 alone is not enough: buildozer's `set name` doesn't follow the
target's references. Steps 2 and 3 use `replace deps`, which is an exact
string match on the `deps` attribute and accepts target patterns
(`//pkg:*`, `//...:*`).

### Why two `replace deps` passes

Bazel allows both short-form (`:foo_py`) and long-form (`//pkg:foo_py`)
labels in `deps`. Buildozer matches the **literal text** in the file, so a
single `replace deps //pkg:old //pkg:new` will not catch same-package
references that are written in short form. Run both.

## Batch script

`devinfra/python/rename_py_suffix.sh` automates the per-target loop. Pass
any Bazel target pattern; the script finds every `py_library` whose name
ends in `_py` and applies the three buildozer commands above.

```bash
# Dry preview — list which targets would be renamed.
bazelisk query "kind('py_library', //augur/...) intersect attr(name, '_py\$', //augur/...)"

# Run the rename across a subtree. Re-run until output reports
# "No matching py_library targets ending in _py" — see "Why re-run" below.
devinfra/python/rename_py_suffix.sh //augur/...
```

### Why re-run

The script snapshots the target list once at start, then loops. If any
buildozer call inside the loop has a non-zero exit (e.g., a target was
already renamed by a prior partial run and bazelisk no longer sees it),
the iteration may abort silently. The script avoids `set -e` to be
tolerant of this, but the safest pattern is to re-invoke until the query
returns no matches.

### Post-rename grep sweep

`buildozer 'replace deps'` only matches inside the literal `deps = [...]`
attribute of a rule. Labels assembled into Starlark variables
(`MY_DEPS = ["//pkg:foo_py", ...]`) or inlined into other attributes are
not touched. Always finish with a textual sweep and patch the holdouts
manually:

```bash
grep -rn '_py["\)]' --include='BUILD.bazel' | \
  grep -v "rules_py\|aspect_py\|@pypi\|py_image\|py_library\|py_binary\|py_test"
```

## Collision check

Before running on a new subtree, verify dropping `_py` won't collide with
an existing target in the same package:

```bash
bazelisk query 'kind("py_(library|binary|test)", //subtree/...)' \
  | python3 -c '
import sys
by_pkg = {}
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("//"):
        continue
    pkg, name = line.rsplit(":", 1)
    by_pkg.setdefault(pkg, set()).add(name)
for pkg, names in by_pkg.items():
    for n in names:
        if n.endswith("_py") and n[:-3] in names:
            print(f"COLLISION: {pkg}:{n} → {pkg}:{n[:-3]}")
'
```

If a collision exists, use `_lib` as the new suffix for the library target.

## Verification

Buildozer's edits are in-tree only; nothing is rebuilt. Verify with:

```bash
# bbr requires the branch to track origin cleanly; if it fails, fall back
# to bazelisk (which runs the build directly on RBE without git sync).
bbr build //<subtree>/...
# or:
bazelisk build //<subtree>/...
```

## Known limitations

- **Starlark variables**: `replace deps` only touches the literal `deps`
  attribute of a rule. Labels inside Starlark variables like
  `MY_DEPS = ["//pkg:foo_py", ...]` are skipped. The post-rename grep sweep
  catches these — patch them with `Edit` or `sed`.
- **Other label-bearing attributes**: same caveat for labels in `data`,
  `srcs`, or custom attributes. Either run additional `replace <attr>`
  passes or fix via the grep sweep.
- **External references**: labels referenced from outside the workspace
  (`MODULE.bazel`, dependent repos) won't be updated. Audit cross-repo
  consumers manually.

## What was renamed

The 2026-05-23 sweep renamed every `*_py` library target under `//augur/...`
(38 targets) — see <devinfra/python/rename_py_suffix.sh> and the commit
that introduced this doc. No `*_py` library targets remain elsewhere in
the repo (verified by `bazelisk query 'kind("py_library", //...)'`).
