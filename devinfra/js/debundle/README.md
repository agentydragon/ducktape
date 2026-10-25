# Debundle

`debundle` is a reverse-engineering tool for maintaining a repeatable mapping from
minified JavaScript bundles to readable, source-like modular code with equivalent
behavior.

The versioned spec records recovered names, module boundaries, structural source
matches, and audited contracts; it is the source of truth for the reconstruction.
As upstream bundles change, rerun and adapt the spec so the recovered understanding
carries forward across releases.

The tool resolves bindings, analyzes value flow and initialization effects, checks
the requested module boundaries against behavioral constraints under the declared
runtime and library assumptions, and emits readable modules and diagnostic
artifacts.

## Where behavior lives

`run` transforms source from an authored spec; the authoring commands inspect,
propose, or edit that spec. Commands that resolve selectors or validate edits reuse
the shared resolver and gate rather than their own acceptance rules.

| Responsibility                                                 | Start here                                               |
| -------------------------------------------------------------- | -------------------------------------------------------- |
| Load and prepare source; retain ASTs and manifest indexes      | `prepare_chunks.rs`, `program_analysis.rs`, `artifacts/` |
| Read flat/tree specs and authoring module documents            | `spec/`                                                  |
| Match, jointly resolve, synthesize, and diagnose selectors     | `selectors/{matching,resolution,authoring,diagnostics}/` |
| Analyze statement effects, dependencies, and structural atoms  | `facts/`, `purity/`, `graph/`, `chunk_analysis/`         |
| Assemble assignments and validate realizability                | `assignment_assembly.rs`, `gate.rs`, `realizability/`    |
| Lower accepted assignments to entry and logical-module files   | `lowering/`                                              |
| Plan/apply vendor substitution and finalize emitted files      | `vendor/`, `pipeline.rs`, `artifacts/emission_files.rs`  |
| Propose moves or safe module refinements; inspect or edit spec | `peel/`, `cli/`                                          |

`pipeline.rs` composes these stages. Prepared/source indexes and finalized
`EmissionFiles` belong to different phases: never pass one phase's data into another
because both contain JavaScript files.

Docs: splitting correctness and implementation entry points <docs/design.md>
(§ File references); selector semantics <SPEC.md>, <docs/selector_resolution.md>;
cross-command CLI semantics (env vars, output formats, validation defaults, batch
atomicity, gate queries, other invocation shapes) <docs/cli.md>, per-command flags
`debundle <command> --help`; selector authoring <docs/selectors.md>; module/binding
editing <docs/spec_editing.md>. Work queue <TODO.md>; `plans/` designs, `perf/`
measurements, `docs/lessons_learned/` rejected approaches. Frozen specimen copies
elsewhere in the repo are not the live implementation.

For existing-module layout work, start with `modules decompose --atomic`,
review semantic destinations and the candidate's complete gate verdict, then
apply the reviewed non-residual owner map with `modules refine --dry-run`
and `modules refine`. Fresh residual claims use `modules propose` and
`modules apply`. Workflow and batch contracts: <docs/cli.md>.

## Getting started

```sh
debundle run \
  --tree-config spec/spec_config.yaml \
  --tree-modules spec/modules \
  --tree-vendor-marks spec/sources/vendor/vendor_marks.yaml \
  --tree-source-root . \
  --out-root bazel-bin/example/debundle.out
```

Every YAML file below `--tree-modules` belongs to the config's `main_chunk_id`
unless `module_roots` maps subtrees to other chunks:

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

- Each `js-files.txt` line names one `.js` file below `inputs.root`, or
  `alias.js<TAB>original/path.js` to load it as chunk `alias` while resolving
  imports of `original/path.js` to it (keeps hashed paths out of specs). An aliased
  file must not also have its own line.
- Roots are normalized relative paths and may not overlap. Trees scoped to one chunk
  share it: no duplicate module paths, and their selectors resolve jointly.
- Compiled flat-spec module paths are relative to their tree root;
  `logical_modules` is keyed by chunk ID. `binding_patches.yaml` applies to
  `main_chunk_id` only.

## Bazel integration and profiling

`pipeline.bzl`'s `debundle_pipeline` runs `debundle run` as a build action; profile
with `perf_wrapper.sh`: <docs/bazel_integration.md>.

## Spec features

- **Runtime loading:** `execution_runtime: native_browser_esm` declares native
  browser module loading independently of harness emission; omitted or `generic`
  retains host-loader fallback. Checked closure and replacement prerequisites:
  <SPEC.md#runtime-loading-contract>.

- **`comment:`** emits into generated JS; **`note:`** is YAML-only authoring
  metadata: <docs/spec_editing.md> § Comments and notes.
- **Conditionally-correct optimizations** (opt-in per chunk; each checks its
  precondition per statement and falls back to the conservative path,
  <docs/design.md> § Conditionally-correct optimizations):
  `chunk_analysis_options.<chunk_id>.dataflow_aware_s_chain` (+ author-trusted
  `trusted_dataflow_summaries`) orders impure statements by the cells they touch
  (§ Emission modes); `chunk_analysis_options.<chunk_id>.local_property_effects`
  makes `X.prop = <pure-rhs>;` a local effect on `X` (A10);
  `deferred_global_publications` narrows callback reentry for audited inert
  browser-global data slots while retaining write ordering.
- **Input-chunk admission**: every materialized chunk is screened for A1 (top-level
  `eval`), A3 (dynamic `import(...)`), A5 (`import.meta`) before quotient/lowering
  (`chunk_analysis/chunk_admission.rs`); audited corpora disable checks with
  `chunk_analysis_options.<chunk>.admission_overrides` (<docs/design.md>
  § Conditions on the input chunk).
- **Readable names across chunks**: <SPEC.md> § Readable names and the chunk public
  surface.
