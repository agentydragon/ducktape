# Debundle

`debundle` is a JavaScript bundle restructuring tool. It reads a transform
spec, emits a decomposed module tree, and writes analysis artifacts that help
drive later module extraction and naming work.

## Where behavior lives

There are two flows: `run` transforms source from an authored spec; the authoring
commands inspect, propose, or edit that spec. Commands that resolve selectors
or validate edits reuse the shared resolver and gate, rather than implementing
their own acceptance rules.

| Responsibility                                                | Start here                                               |
| ------------------------------------------------------------- | -------------------------------------------------------- |
| Load and prepare source; retain ASTs and manifest indexes     | `prepare_chunks.rs`, `program_analysis.rs`, `artifacts/` |
| Read flat/tree specs and authoring module documents           | `spec/`                                                  |
| Match, jointly resolve, synthesize, and diagnose selectors    | `selectors/{matching,resolution,authoring,diagnostics}/` |
| Analyze statement effects, dependencies, and structural atoms | `facts/`, `purity/`, `graph/`, `chunk_analysis/`         |
| Assemble assignments and validate realizability               | `factor_assembly.rs`, `gate.rs`, `realizability/`        |
| Lower accepted assignments to entry and logical-module files  | `lowering/`                                              |
| Plan/apply vendor substitution and finalize emitted files     | `vendor/`, `pipeline.rs`, `artifacts/emission_files.rs`  |
| Propose author-reviewed moves; inspect or edit the spec       | `peel/`, `cli/`                                          |

`pipeline.rs` composes these stages. Prepared/source indexes and finalized
`EmissionFiles` describe different phases: do not pass one phase's data into
another merely because both contain JavaScript files.

For changes to splitting correctness, start with <docs/design.md>. For selector
semantics use <SPEC.md> and <docs/selector_resolution.md>. The active work queue
is <TODO.md>; `plans/` records designs, `perf/` records measurements, and
`docs/lessons_learned/` preserves rejected approaches. Frozen specimen copies
elsewhere in the repository are not the live implementation.

## CLI

`debundle <command> --help` is the per-command reference; `docs/cli.md`
covers the cross-command semantics (env vars, output formats, validation
defaults, batch atomicity, gate queries). Workflow docs: `docs/selectors.md` (portable
selector authoring), `docs/spec_editing.md` (module/binding editing).
How selectors actually resolve: `docs/selector_resolution.md`.

Import planning and emission helpers are grouped under `lowering/imports/`.
They remain separate modules and passes, composed by the lowering crate.

Vendor internals separate package lookup/containment (`vendor/packages.rs`),
export-surface inspection (`vendor/export_surface.rs`), emitted-output import
resolution (`vendor/output_imports.rs`), and import/identifier rewrites
(`vendor/import_rewrites.rs`). The post-strip consumer gate remains mandatory.
Export AST constructors live in `js_ast.rs`; callers choose name encoding and
alias policy. Lowering carries each naturalized body, its rename maps and its
post-rename facts together as `NaturalizedModuleBody` before module emission.

Selector implementation is grouped under `selectors/`: AST matching and
`source_match` live in `matching/`, selector solving in `resolution/`, selector
generation and minimization in `authoring/`, and selector-debt reporting in
`diagnostics/`. Bazel target names remain stable.

The CLI root only composes and routes commands. `cli/{bindings,modules,spec,
inspection}_commands.rs` own each family's arguments, adapters, and renderers;
`cli/binding.rs`, `cli/module.rs`, and `cli/edit_gate.rs` own spec editing and
validation independently of presentation.

### Analysis and planning vocabulary

- `chunk_analysis::ChunkAnalysisOutput` contains semantic statement facts, the
  owner graph, and structural atomic units, independent of module assignment.
- `gate::FactorizationInputs` holds the owner graph, authored module/binding
  catalogue, and lookup indexes shared by candidate `ChunkFactorization`s.
- `peel::propose` produces advisory module-move proposals; it does not assign
  owners authoritatively. `factor_assembly` constructs the actual partition.
- `selectors/matching/chunk_facts` projects AST syntax for matching;
  `facts/` computes semantic effects and dependencies. `program_analysis`
  performs the shallow manifest scan, including pass-through chunks.

These are different data products, not alternate names for one analysis.

Cheat sheet of the most-used commands:

- `debundle run` — execute the transform pipeline (parse + facts +
  owner_graph + realizability gate + lower + emit). Add `--dry-run`
  to run pipeline checks without writing emitted JS or reports.
- `debundle bindings assign <sym>:<module>[:<readable>]` — move a
  binding (single, multi-positional, or `--batch <file.json>`).
- `debundle bindings rename <original> <readable>` — rename without
  moving.
- `debundle modules propose` — proposer-derived move proposals;
  `--source-root` annotates anonymous-statement addressability.
- `debundle modules merge --target <T> <sources...>` — splice module
  YAMLs.
- `debundle describe <id>` / `debundle show-source <id>` — graph +
  source context for any binding / module path or `logical:N` module id /
  atom / owner / proposal / diagnostic ID.
- `debundle inspect-source` — pretty-print parsed top-level source items
  with stable raw indices, original byte spans, source locations, and bindings;
  it does not load a spec or owner graph.
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
while `logical_modules` is keyed by chunk ID. The `binding_patches.yaml` stream
applies to `main_chunk_id` only.

(For other invocation shapes — flat spec, vendor package roots, etc. —
see `docs/cli.md`.)

## Bazel integration and profiling

`pipeline.bzl`'s `debundle_pipeline` runs `debundle run` as a build action;
profile with `perf_wrapper.sh`: <docs/bazel_integration.md>.

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
empty `comment:` emits nothing. Claimed declarators of one `var`/`let`/`const`
list are emitted as one statement each, so each comment sits above its own
declarator; bindings of a single destructuring declarator share its statement,
and their comments stack above it in pattern order.

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

These opt-in per-chunk analyses are sound only when the input avoids shapes that
defeat static reasoning; each checks its precondition per statement and falls
back to the conservative path (`docs/design.md` → "Conditionally-correct
optimizations"):

- `chunk_analysis_options.<chunk_id>.dataflow_aware_s_chain`, with the
  author-trusted `trusted_dataflow_summaries` refinement, orders impure
  statements by the cells they touch (`docs/design.md` → "Emission modes").
- `chunk_analysis_options.<chunk_id>.local_property_effects` makes
  `X.prop = <pure-rhs>;` a local effect on `X` (`docs/design.md` → A10).

## Input-chunk admission checks

Every materialized chunk is screened for A1 (top-level `eval`), A3 (dynamic
`import(...)`) and A5 (`import.meta`) before any quotient or lowering work
(`chunk_analysis/chunk_admission.rs`). Audited corpora disable individual checks per
chunk with `chunk_analysis_options.<chunk>.admission_overrides`. Enforcement
strength, override reporting and the unchecked residual: `docs/design.md` →
"Conditions on the input chunk".

## Readable names across chunks

A spec-assigned member name (`name:` on a member or a binding in
`source_matches[].bindings`) is used for the emitted logical-module binding and
its intra-chunk imports. For a named binding that was already exported by its
source chunk, the chunk entry also exports that same live binding under the
readable name while retaining the original minified export name. Imports from
processed chunks can then use the readable export and a readable local alias.

This is additive for compatibility: chunks outside the processed set, dynamic
imports, and namespace imports continue to find the original public export.
Imports are left in their original form if the readable local would collide with
another binding or be captured by a nested binding. Unnamed targets are not
naturalized by this pass.
