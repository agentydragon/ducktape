# Debundle

`debundle` is a JavaScript bundle restructuring tool. It reads a transform
spec, emits a decomposed module tree, and writes analysis artifacts that help
drive later module extraction and naming work.

## CLI

`debundle <command> --help` is the per-command reference; `docs/cli.md`
covers the cross-command semantics (env vars, output formats, validation
defaults, batch atomicity, gate queries). Workflow docs: `docs/selectors.md` (portable
selector authoring), `docs/spec_editing.md` (module/binding editing).
How selectors actually resolve: `docs/selector_resolution.md`.

Cheat sheet of the most-used commands:

- `debundle run` — execute the transform pipeline (parse + facts +
  owner_graph + realizability gate + lower + emit). Add `--dry-run`
  to run pipeline checks without writing emitted JS or reports.
- `debundle bindings assign <sym>:<module>[:<readable>]` — move a
  binding (single, multi-positional, or `--batch <file.json>`).
- `debundle bindings rename <original> <readable>` — rename without
  moving.
- `debundle modules propose` — factorizer-derived move proposals;
  `--source-root` annotates anonymous-statement addressability.
- `debundle modules merge --target <T> <sources...>` — splice module
  YAMLs.
- `debundle describe <id>` / `debundle show-source <id>` — graph +
  source context for any binding / module path or `logical:N` module id /
  atom / owner / proposal / diagnostic ID.
- `debundle bindings comment <sym>` / `debundle modules comment <module>` —
  edit `comment:` fields (see "Comments" below).

## Getting started

Run a tree-shaped authoring spec:

```sh
debundle run \
  --tree-config spec/spec_config.yaml \
  --tree-modules spec/modules \
  --tree-vendor-marks spec/sources/vendor/vendor_marks.yaml \
  --tree-source-root . \
  --out-root bazel-bin/example/debundle.out
```

By default, every YAML file below `--tree-modules` belongs to the config's
`main_chunk_id`. An application-level pipeline can author modules for several
chunks by mapping subtrees of `--tree-modules` to the chunk each is scoped to:

```yaml
main_chunk_id: cli
module_roots:
  chunks/cli: cli
  chunks/print: print
  chunks/structured_io: structuredIO
  shared/print_routing: print

inputs:
  root: extracted
  js_list_path: js-files.txt

unassigned_mode:
  cli: { kind: inline_in_entry }
  print: { kind: inline_in_entry }
  structuredIO: { kind: inline_in_entry }
```

The roots must be normalized relative paths and may not overlap. Several trees
may scope to one chunk: their modules share the chunk, so no two may define the
same module path, and their selectors resolve jointly (no two entities claim one
place). Module paths in the compiled flat spec are relative to their tree's root,
while `logical_modules` is keyed by chunk ID. The existing `binding_patches.yaml`
stream applies to `main_chunk_id`.

(For other invocation shapes — flat spec, vendor package roots, etc. —
see `docs/cli.md`.)

## Bazel Integration

`pipeline.bzl` provides a Bazel rule for running `debundle run` as a normal
build action:

```python
load("@ducktape//devinfra/js/debundle:pipeline.bzl", "debundle_pipeline")

debundle_pipeline(
    name = "debundle",
    input_data = [
        "//path/to:bundle_inputs",
    ],
    package_roots = {
        "//:node_modules/react/dir": "react",
        "//:node_modules/zod/dir": "zod",
    },
    spec_tree_inputs = [":spec_data"],
    tree_config = "spec/spec_config.yaml",
    # Target holding the chunk the spec reads. Its files' own root becomes
    # the root `inputs.root` / `inputs.js_list_path` resolve against, so a
    # committed chunk resolves against the execroot and a build-extracted
    # one against bazel-bin -- no need to vendor the chunk into git.
    tree_source_root = "//path/to:bundle_inputs",
    tree_modules = "spec/modules",
    tree_vendor_marks = "spec/sources/vendor/vendor_marks.yaml",
)
```

The rule writes a tree artifact named `<target>.out` under `bazel-bin`. It
declares the spec, input data, package roots, and debundler binary as Bazel
inputs/tools, then runs the debundler from `BAZEL_BINDIR` so source-relative
spec paths resolve the same way they do in ordinary builds. By default the rule
uses `@ducktape//devinfra/js/debundle:debundle`; consumers can select a
different binary at repo or command-line scope with:

```sh
bazel build //path/to:debundle \
  --@ducktape//devinfra/js/debundle:debundler=@my_debundle_bin//file
```

The rule declares `@ducktape//devinfra/js/debundle:ortools_cpsat_solver` as an
action tool and passes its execroot path to the debundler. The materializer uses
that OR-Tools CP-SAT sidecar for global selector assignment. Consumers can
override the solver tool with the matching label flag when needed.

## Profiling

`debundle_pipeline` creates the normal pipeline target plus local profiling
sibling targets that reuse the exact same action command, inputs, package
roots, working directory, and debundler binary.

```python
load(
    "@ducktape//devinfra/js/debundle:pipeline.bzl",
    "debundle_pipeline",
)

debundle_pipeline(
    name = "debundle",
    # Pipeline attrs...
)
```

Generated targets:

- `:debundle`
- `:debundle_profile_time`
- `:debundle_profile_perf`
- `:debundle_profile_massif_heap`
- `:debundle_profile_heaptrack`

Profile actions are tagged `manual` and use local/no-remote/no-cache/no-sandbox
execution requirements. Build them with full output downloads when remote
execution is configured:

```sh
bazel build //path/to:debundle_profile_perf --remote_download_outputs=all
```

The standalone `perf_wrapper.sh` helper post-processes `perf` output for
ad-hoc local runs:

```sh
PERF_RECORD_FREQ=49 \
  devinfra/js/debundle/perf_wrapper.sh --output-dir /tmp/debundle-profile -- \
  <debundler> run <debundle args...>
```

Save important runs under the consuming repo's `debug/perf/` directory with the
captured command, stdout/stderr and profiler artifacts.

## Comments

Module YAMLs, binding annotations, and `anonymous_statements:` entries
may carry an optional `comment:` field for reverse-engineering
annotations; these emit into generated JS on every rebuild, so RE
notes survive `debundle run` invocations. The same places also accept
`note:`: YAML-only scratch metadata that never emits (debt rationale,
provenance; `modules merge` writes its `merged from: <sources>` provenance into
the module-level `note:`, composing with any existing note, and concatenates
source-module `comment:` fields into the target's with a `--- from <source>:`
divider). Per-binding
metadata belongs under `annotations.<export_name>` and may include `comment`,
`note`, `purity`, `effect`, `pure_members`, or
`no_sync_callback_members`. Edit module and member comments via
`debundle bindings comment` / `debundle modules comment`. See
`docs/spec_editing.md` → "Workflow: authoring `comment:` fields" for the
YAML schema, worked CLI examples, and the comment/`note:` move semantics.

A module-top `comment:` emits at the top of the generated module file,
an annotation `comment:` immediately above the binding's owner statement, and
an anonymous-statement `comment:` immediately above the matched statement; an
empty `comment:` emits nothing.

`comment:` text is part of debundle's readability surface — the point of
the tool is to turn minified chunks into legible code, so use comments to
explain intent, invariants, and module relationships. Keep provenance,
owner IDs, and source-call trivia in `note:` (or omit them), not in
emitted `comment:` text.

**Do not use `#` YAML comments in spec files.** The rewriters (`bindings
assign`, `synthesize --apply`, `modules merge`, …) re-emit the YAML and drop
every `#` comment, so any `#` annotation is silently lost on the next automated
edit. Anything that must persist belongs in a schema field — `comment:`
(emitting) or `note:` (non-emitting) — which the rewriters preserve explicitly.

## Conditionally-correct optimizations

Two opt-in per-chunk analyses are sound only when the input avoids shapes that
defeat static reasoning; each checks its precondition per statement and falls
back to the conservative path (<docs/design.md> → "Conditionally-correct
optimizations"):

- `chunk_analysis_options.<chunk_id>.dataflow_aware_s_chain`, with the
  author-trusted `trusted_dataflow_summaries` refinement, orders impure
  statements by the cells they touch (<docs/design.md> → "Emission modes").
- `chunk_analysis_options.<chunk_id>.local_property_effects` makes
  `X.prop = <pure-rhs>;` a local effect on `X` (<docs/design.md> → A10).

## Input-chunk admission checks

Every materialized chunk is screened for A1 (top-level `eval`), A3 (dynamic
`import(...)`) and A5 (`import.meta`) before any quotient or lowering work
(`stage_one/chunk_admission.rs`). Audited corpora disable individual checks per
chunk with `chunk_analysis_options.<chunk>.admission_overrides`. Enforcement
strength, override reporting and the unchecked residual: <docs/design.md> →
"Conditions on the input chunk".
