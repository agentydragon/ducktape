# Gazelle

Python BUILD files are gazelle-managed. `bb run //devinfra:gazelle` regenerates them;
a run over a converged tree is a no-op, so `--mode=diff` (exit nonzero on any diff or
unresolvable import) is the drift check. Code-shape conventions (per-file libraries,
`main_module` binaries, test-glob reservation, the conftest chain) live in
<../../STYLE.md> § Gazelle.

## Plugin and patch

`rules_python_gazelle_plugin` 2.3.2 under per-file generation mode, plus
<../../patches/rules_python_gazelle_ducktape.patch> (`single_version_override`):

1. Per-file mode generates a `py_library` for every `.py` file. Upstream (still on
   `main`) splits files containing `if __name__ == "__main__"` into `py_binary`
   targets, stealing the module's library. Upstream candidate: a directive gating
   binary generation.
2. `py_{library,test,binary}` loads emit from `//devinfra/python:defs.bzl`.
   Repo-specific, stays a patch: same-name `map_kind` load redirection is impossible
   (gazelle rejects `py_library py_library <bzl>` as a kind loop), and 2.x's
   `alias_kind` directive only registers differently-named wrappers for rule
   _recognition_ — it rejects same-name aliases and does not affect emitted loads.

No root `map_kind py_binary py_library` directive: gazelle applies map_kind to
existing rules too, rewriting hand-written binaries' kind.

`python_generate_pyi_deps` stays `false` (root BUILD): rules_python keeps `pyi_deps`
out of runtime runfiles and the mypy aspect does not traverse them, so `TYPE_CHECKING`
and lazy imports belong in `deps`.

## Escape hatches

Import validation is on; every escape is explicit and local:

- `# gazelle:ignore <mod>` in the `.py` file for imports the environment provides
  (`FreeCAD`, `gi`, `PySide6`, `pivy`, `_bazel_site_init`) or that resolve to a
  Bazel-generated `__init__` stub with no target of its own.
- `# gazelle:include_dep <label>` in the `.py` file for runtime-only deps the import
  scan cannot see, placed in the module that triggers the hidden import: SQLAlchemy
  dialect drivers, wheel extras imported at module load (py-key-value backends,
  pydantic-settings yaml, SessionMiddleware's `itsdangerous`, TestClient's `httpx`),
  stub-chain dists (`types-jsonschema` → `referencing`), pytest plugins loaded by
  name, modules run via `-m`, and scripts driven by runfiles path.
- `# gazelle:resolve py <module> <label>` BUILD directives for import-name/dist
  mismatches and for imports into gazelle-excluded trees.
- `# gazelle:exclude` BUILD directives for trees and files deliberately not
  gazelle-managed.
- A rule-level `# keep` on hand-maintained rules whose `srcs` are excluded files:
  gazelle deletes managed-kind rules over files it cannot see
  (`laser/material_test`, `skills/forgejo:forgejo_lib`,
  `skills/testing:frontmatter_test`).
- A dep-level `# keep: <reason>` suffix where the source file cannot carry the
  annotation: sibling stub dists a package's `__init__` chains into
  (`agent_framework_{anthropic,claude,openai}` beside `_core`), and any dep of a rule
  whose source gazelle cannot parse. Macro-kind rules (`live_openai_py_test`) are
  unmanaged and hand-carry `//:conftest`.

## Known limitations

- `agent_core/script_handler.py` uses PEP 695 type-parameter-default syntax
  (`type ScriptGen[T = None]`) that 2.3.2's tree-sitter grammar cannot parse: gazelle
  warns and may emit wrong deps for it; its deps carry `# keep`. Try a further plugin
  upgrade (rebasing the patch) before hand-managing that target.
- Plugin 2.x can also delete `py_library`/`py_test` rules whose `srcs` files were
  deleted, but only under a gazelle release carrying
  [bazel-gazelle#2362](https://github.com/bazel-contrib/bazel-gazelle/pull/2362);
  until then the build error on the missing src is the signal.
