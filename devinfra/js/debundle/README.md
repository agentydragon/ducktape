# Debundle

`debundle` is a JavaScript bundle restructuring tool. It reads a transform
spec, emits a decomposed module tree, and writes analysis artifacts that help
drive later module extraction and naming work.

## CLI

`debundle <command> --help` is the per-command reference; `docs/cli.md`
covers the cross-command semantics (env vars, output formats, batch
atomicity, gate queries). Workflow docs: `docs/selectors.md` (portable
selector authoring), `docs/spec_editing.md` (module/binding editing).

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

Mutating commands validate by default: `bindings assign`, `bindings
unassign`, `modules merge`, and `modules delete` (of non-empty
modules, with `--force`) run the realizability gate; `bindings
rename` runs name-collision detection. `--dry-run` previews,
`--no-verify` skips. Read-only and mutating commands accept
`--format text|json|ndjson` (default `text` on tty, `json` on pipe);
the five mutating verbs share one JSON outcome schema, and gate
rejections print a structured rejection object on stdout (see
`docs/cli.md`).

Common arg paths accept env-var defaults (`DEBUNDLE_GRAPH`,
`DEBUNDLE_MODULES`, `DEBUNDLE_SOURCE_ROOT`, `DEBUNDLE_OUT`;
`run --tree-source-root` reads the separate
`DEBUNDLE_TREE_SOURCE_ROOT`). Set them once per session.

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
`main_chunk_id`, preserving the original single-chunk authoring layout. An
application-level pipeline can author modules for several chunks by mapping each
chunk ID to a distinct subtree relative to `--tree-modules`:

```yaml
main_chunk_id: cli
module_roots:
  cli: chunks/cli
  print: chunks/print
  structuredIO: chunks/structured_io

inputs:
  root: extracted
  js_list_path: js-files.txt

unassigned_mode:
  cli: { kind: inline_in_entry }
  print: { kind: inline_in_entry }
  structuredIO: { kind: inline_in_entry }
```

The mapped roots must be normalized relative paths and may not duplicate or
overlap. Module paths in the compiled flat spec are relative to their individual
mapped root, while `logical_modules` remains keyed by chunk ID. The existing
`binding_patches.yaml` stream still applies to `main_chunk_id`.

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

Pipeline outputs include one exact protobuf payload per selector solve under
`debug/selector_cpsat_requests/`, plus compact human-readable metadata under
`debug/selector_cpsat_summaries/`. Per-solve files are required because the
materializer solves multiple chunks concurrently. Summaries cover variables,
finite domains, allowed tables, binary constraints, and global
`all_different` constraints. The Rust debundler and C++ sidecar communicate
through the binary protobuf request/response; JSON here is only metadata.

For slow solver investigations, build the problem output group without running
the CP-SAT search:

```sh
bazel build //path/to:debundle --output_groups=selector_problem
```

This emits `bazel-bin/path/to/debundle.selector_cpsat_request.pb` after the
same selector lowering step the full pipeline uses. The protobuf is the replay
artifact for the C++ sidecar; human-readable selector summaries remain in the
full pipeline's `debug/selector_cpsat_summaries/` output when available.

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

The standalone `perf_wrapper.sh` helper can still post-process `perf` output for
ad-hoc local runs:

```sh
PERF_RECORD_FREQ=49 \
  devinfra/js/debundle/perf_wrapper.sh --output-dir /tmp/debundle-profile -- \
  <debundler> run <debundle args...>
```

Save important runs under the consuming repo's `debug/perf/` directory with the
captured command, stdout/stderr, profiler artifacts, and selector summaries
from `debug/selector_cpsat_summaries/` when available.

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

Some analyses in this crate are **conditionally correct**: they are sound
only when the input bundle avoids a small set of dynamic-dispatch shapes
that defeat static reasoning. Each such pass checks the precondition on
the statements it would fire on and falls back to a strictly-conservative
path when the check fails — see <docs/design.md> → "Conditionally-correct
optimizations" for the soundness rule.

The first such pass is the dataflow-aware S-chain in `graph.rs`, opted
into per chunk via
`chunk_analysis_options.<chunk_id>.dataflow_aware_s_chain` in the spec.
Each impure top-level statement carries a `dataflow_summarizable` bit
(`facts/wire.rs`); a statement keeps the relaxed per-cell emission only
when it contains **none** of the following (all checked per statement,
at-init positions only):

- a call, `new`, optional call (`f?.()`), or tagged template the purity
  classifier does not prove `Pure` — this covers `console.log(...)`
  and other I/O, calls into chunk functions whose bodies write global
  props, direct `eval(...)`, **and** indirect `(0, eval)(...)`
- a member write through a binding: `obj.x = 1`, `obj.x++`,
  destructuring assignment targets containing member expressions
  (`[obj.x] = arr`)
- `with (obj) { ... }`
- `new Function(...)` / `Function(...)`
- a computed-key access on an unshadowed global-object alias:
  `globalThis[<expr>]` / `window[<expr>]` / `self[<expr>]` /
  `frames[<expr>]` / `top[<expr>]`
- `Object.defineProperty` / `Reflect.defineProperty` with a
  global-object alias as the target
- `new Proxy(<global>, ...)`
- a read of a binding the global object may have escaped into:
  `const g = globalThis; ... g.tag` taints `g` (transitively, through
  bindings derived from it), and every statement reading a tainted
  binding falls back

Statements that fail the check fall back to the strictly-conservative
S-chain (an edge to every prior impure owner; later statements treat
them as opaque barriers), so the optimization is safe to enable even
on bundles that mix audited and unaudited code — only the
unsummarizable statements pay the conservative cost. Call-heavy
top-level code therefore sees little benefit from the relaxation:
every statement containing an unproven call is conservatively
chained. That is the intended trade — the relaxation only fires where
the analyzer can actually prove non-interference.

Specs that have independently audited a call-heavy or annotation-heavy
chunk may opt into `trusted_dataflow_summaries` alongside
`dataflow_aware_s_chain`. That author-trusted flag restores the
pre-tightening behavior for conservative-but-present summaries:
unproven top-level calls/news, member writes, and similar ordinary
summary bails stay impure, but use their syntactic
binding/global-property read-write summary instead of becoming opaque
barriers. Shapes that defeat write-cell extraction outright (`eval`,
`with`, `Function`, computed global-object keys, global
`defineProperty`, global `Proxy`) still fall back to conservative
barriers regardless of the flag.

Unshadowed `window` / `self` / `frames` / `top` are treated as the
same object as `globalThis` for cell tracking: `window.tag = 1` and
`globalThis.tag` are the same cell. A chunk-top-level declaration or
import of one of these names disables its global treatment chunk-wide.

See `docs/design.md` → "Emission modes" for the precise dataflow-aware
emission rule (including the write-after-read edges).

The second such pass is local-property-write effect scoping, opted into
per chunk via `chunk_analysis_options.<chunk_id>.local_property_effects`.
A whole statement of the shape `X.prop = <pure-rhs>;` (or a
comma-sequence of such) where `X` is a chunk-top declared binding — the
React `C.displayName = "…"` annotation idiom — classifies as a local
effect on `X` (Pure + a bidirectional `LocalEffect` co-location edge to
`X`'s declaration) instead of joining the S-chain. Statements outside
the shape (compound assignment, computed non-literal keys, `__proto__`
segments, impure RHS, writes through imports) keep the conservative
classification. The author-audited precondition is documented on the
spec field (`spec::OwnerGraphOptions::local_property_effects`) and in
`docs/design.md` → A10.

## Input-chunk admission checks

Every materialized chunk is screened against the statically checkable
input assumptions of `docs/design.md` → "Conditions on the input
chunk" before any quotient or lowering work (`chunk_admission.rs`,
run from `stage_one::compute_chunk_analysis` next to the A2
top-level-await bail). The enforced shapes:

- **A1** — direct `eval(...)` / seq-indirect `(0, eval)(...)` calls at
  module top level.
- **A3** — string-literal dynamic `import(...)` resolving back into
  the same chunk (any depth), and non-literal specifiers at module
  top level.
- **A5** (minimal) — `import.meta` use at module top level beyond
  `import.meta.url`.

A2 (top-level `await`) bails in the same place; A4 (`with`) is
rejected at parse time. Rejections name the chunk, the offending
statement ordinal, and the matched shape. For audited corpora,
disable individual checks per chunk in the spec:

```yaml
chunk_analysis_options:
  static/app:
    admission_overrides: [a1_eval]
```

Each override prints a one-line notice per run; an override that no
longer suppresses any violation is reported as redundant (remove it).
The deliberately unchecked residual shapes are listed in
`docs/design.md` → "Coverage gaps".
