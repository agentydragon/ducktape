# Gazelle Convergence

Goal: `bb run //devinfra:gazelle` completes, a rerun is a no-op, and CI enforces clean
diffs — a BUILD delta then only ever means real dependency drift. Verified on a scratch
run of this branch (BuildBuddy invocation `d479a44c-da6c-4648-bdde-19a18ece2829`): with
the burn-down below applied, gazelle completes with a purely mechanical residual diff
(~226 files, +3226/−1190) and no destructive edits. Until step 1 lands, use
`--mode=diff` only — a write-mode run aborts on the name collisions below _after_
rewriting the packages it already visited.

## Landed with this plan

Plugin bumped 1.9.2 → 2.3.2 plus
<../../patches/rules_python_gazelle_ducktape.patch> (`single_version_override`):

1. Per-file mode generates a `py_library` for every `.py` file. Upstream (still on
   `main`) splits files containing `if __name__ == "__main__"` (893 here) into
   `py_binary` targets, stealing the module's library. Upstream candidate: a directive
   gating binary generation.
2. `py_{library,test,binary}` loads emit from `//devinfra/python:defs.bzl`.
   Repo-specific, stays a patch: same-name `map_kind` load redirection is impossible
   (gazelle rejects `py_library py_library <bzl>` as a kind loop), and 2.x's new
   `alias_kind` directive only registers differently-named wrappers for rule
   _recognition_ — it rejects same-name aliases and does not affect emitted loads.

A third 1.9.2 problem — `getRulesWithInvalidSrcs` deleting every `py_binary` without
`srcs`, i.e. all `main_module` binaries — is fixed upstream in 2.x; the bump picks that
up. 2.x still misses namespace-package dists in the generated manifest (step 1 stands).

The root `map_kind py_binary py_library` directive is removed — gazelle applies
map_kind to existing rules too, rewriting hand-written binaries' kind to `py_library`.

## Target conventions

- One `py_library` per `.py` file, named exactly the module stem. File mode hardcodes
  the stem name; `python_library_naming_convention` applies only in package mode, so
  there is no directive escape. Existing `<stem>_lib` names must be renamed _before_
  gazelle runs over them: name-matched rules keep their attributes, while srcs-matched
  rules get deleted and regenerated, silently dropping `visibility`.
- `py_binary`: hand-written, `main_module`, no `srcs`, `deps = [":<stem>"]`, and a name
  that is no module stem in its package — gazelle then never touches it. Naming:
  `<stem>_bin`; where an aspect image twin already holds `_bin`, the twin moves to
  `<stem>_image_bin` (the `finance/augur/api` precedent).
- `test_*.py` / `*_test.py` filenames are reserved for `py_test` targets (the
  `python_test_file_pattern` globs). Shared test helpers live in `<pkg>/testing/`
  packages (`default_testonly`), never in test-glob-named files.
- `conftest.py` never appears in `py_test.srcs`; the plugin generates a per-package
  `:conftest` library and deps each test on the whole ancestor conftest chain
  (including `//:conftest`).
- Rules list only own-package files. A subdirectory's `.py` files get per-file rules in
  the subdirectory's own BUILD, not reached from the parent.
- Import validation stays on. Escapes are explicit and local:
  - `# gazelle:ignore <mod>` in the `.py` file for environment-provided imports
    (`FreeCAD`, `gi`, `PySide6`, `pivy`, `_bazel_site_init`);
  - `# gazelle:include_dep <label>` in the `.py` file for runtime-only deps the import
    scan cannot see (SQLAlchemy driver dists);
  - `# gazelle:resolve py` BUILD directives for import-name/dist mismatches;
  - `# gazelle:ignore` / `# gazelle:exclude` BUILD directives for trees deliberately
    not built.

## Burn-down

Independently landable PRs; step 1's per-tree PRs go in parallel.

1. **Per-tree structural migration** (one PR per tree: `devinfra`, `finance`, `haku`,
   `mcp_infra`, `tana`, `skills`, `x`, remainder), references updated in the same
   commit:
   - Rename the 77 binaries squatting module stem names; rename `_bin` aspect image
     twins to `_image_bin` where they collide.
   - Convert the 43 remaining `srcs`-style `py_binary` rules to `main_module` form.
   - Rename the 53 `<stem>_lib` libraries to `<stem>`.
   - Dissolve the 7 multi-src aggregators (`finance/augur/sim` compiler/codec/engine,
     `devinfra/js/debundle/live_proxy:proxy_lib`, `tana/litellm_proxy:provider`,
     `mcp_infra/exec:docker`, `haku/console/mcp_auth:fastmcp_adapter`) into per-file
     libraries — all seven verified acyclic. Subdirectory groups get their own BUILD
     files.
   - Add `# gazelle:include_dep` driver annotations where `haku/console` and `props`
     build engine URLs (the single-owner cases — `finance/plaid/db`,
     `haku/x/dispatch` — carry them already).
   - Move the 9 subdir-reaching rules (`mcp_infra/exec:docker_types`,
     `skills/forgejo:forgejo_lib`, `inventree_utils:samplebooks_parts_data`, the six
     `finance/augur/sim` subdir tests) into the file's own package.
2. **The run**: `bb run //devinfra:gazelle`; review the mechanical residue — ancestor
   conftest deps (~350 tests gain `//:conftest`), direct deps that were only transitive
   (`numpy`, `jaxtyping`), dead hand deps dropped — and land it. Rerun to confirm
   no-op.
3. **Enforcement**: bazel-ci step running `--mode=diff` (fails nonzero on drift);
   graduate these conventions into README §Gazelle / STYLE.md and delete this plan.

## Known limitations

- `agent_core/script_handler.py` uses PEP 695 type-parameter-default syntax
  (`type ScriptGen[T = None]`) that even 2.3.2's tree-sitter grammar cannot parse
  (the 1.9.2 plugin also choked on `mcp_infra/request_scoped_openapi.py`; the bump
  fixed that one): gazelle warns and may emit wrong deps for it. Try a further plugin
  upgrade (rebasing the patch) before hand-managing that target.
- Plugin 2.x can also delete `py_library`/`py_test` rules whose `srcs` files were
  deleted, but only under a gazelle release carrying
  [bazel-gazelle#2362](https://github.com/bazel-contrib/bazel-gazelle/pull/2362);
  until then the build error on the missing src is the signal.
