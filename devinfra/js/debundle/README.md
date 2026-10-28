# Debundle

`debundle` is a reverse-engineering tool for maintaining a repeatable mapping from
minified JavaScript bundles to readable, source-like modular code with equivalent
behavior.

## Choose a task

| Task                                 | Start here                                                                                                                                   | Result to review                                                                                                                                                                    |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Run a spec against captured source   | [Getting started](#getting-started) and [pipeline validation](docs/cli.md#running-the-pipeline)                                              | Emitted modules, selector outcomes, and gate result                                                                                                                                 |
| Understand a binding                 | [Inspect a binding end to end](docs/cli.md#workflow-investigating-a-binding-end-to-end)                                                      | Source, dependencies, and current assignment                                                                                                                                        |
| Extract unassigned code              | [Propose and claim residuals](docs/spec_editing.md#workflow-proposing-new-modules)                                                           | Reviewed proposal, selectors, and proposed-assignment gate                                                                                                                          |
| Move existing bindings               | [Focused binding moves](docs/spec_editing.md#workflow-moving-a-binding-from-one-module-to-another)                                           | Reviewed move batch and gate verdict                                                                                                                                                |
| Review architecture and module shape | [Architecture review](docs/architecture_review.md)                                                                                           | Current architecture notes, measured conventions, and worker-ready reorganization tasks                                                                                             |
| Reorganize existing assignments      | [Decompose, then refine](docs/spec_editing.md#workflow-reorganizing-existing-modules)                                                        | Complete source-unit assignment and gate verdict                                                                                                                                    |
| Recover readable binding names       | [Naming workflow](docs/naming.md)                                                                                                            | Source-grounded names, reviewed YAML changes, and naming verification                                                                                                               |
| Rename a binding                     | [Rename without moving](docs/spec_editing.md#workflow-renaming-a-binding-without-moving)                                                     | Name change and collision check                                                                                                                                                     |
| Rename or merge module files         | [Rename a module](docs/spec_editing.md#renaming-or-disabling-a-module) or [merge modules](docs/spec_editing.md#workflow-merging-two-modules) | Gated YAML edit preserving claims and metadata                                                                                                                                      |
| Stabilize selectors                  | [Bulk conversion loop](docs/selectors.md#bulk-conversion-loop)                                                                               | Reviewed anchors, exact-source validation, and remaining debt                                                                                                                       |
| Diagnose a rejected edit             | [Rejection diagnostics](docs/cli.md#rejection-diagnostics), then [gate queries](docs/cli.md#gate-queries)                                    | Source-backed blocking constraint; if an assertion is justified, use the [contract chooser](skills/shared/initialization_contracts.md#choose-the-contract-for-the-established-fact) |
| Plan, delegate, or integrate work    | [Shared work procedures](skills/shared/workflow.md)                                                                                          | Scoped plans, isolated copied inputs, completed execution evidence, and validated integration                                                                                       |

Use the project's adapter for concrete paths and gate, regeneration, and runtime
checks. Graph-backed commands require a matching saved graph through `--graph` or
`DEBUNDLE_GRAPH`; [input compatibility and regeneration](docs/cli.md#common-arguments-and-env-vars)
and [invocation records](docs/cli.md#resolved-invocation-records) describe those
boundaries. Read the linked recipe for the chosen operation; per-flag details are
in `debundle <command> --help`.

Each versioned spec records recovered names, module boundaries, structural source
matches, and audited contracts for one captured input program. A run resolves one
exact spec against that program's configured chunks. Structural selectors match
only by the documented AST, hole, and alpha-renaming rules; the engine does not
compare specs or infer edits across upstream versions.

When upstream publishes a new bundle snapshot, use the installed
`debundle_stabilize` skill and the
authoring commands below against that snapshot. A prior spec can be a human-edited
starting point, but the new source and draft spec are validated as one pair.
Review each mismatch and each synthesized selector for the intended behavior;
apply only justified candidates, rerun full validation and the project gate, and
report the remaining mismatches.

The tool resolves bindings, analyzes value flow and initialization effects, checks
the requested module boundaries against behavioral constraints under the declared
runtime and library assumptions, and emits readable modules and diagnostic
artifacts.

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

## Where behavior lives

`run` transforms source from an authored spec; the authoring commands inspect,
propose, or edit that spec. Commands that resolve selectors or validate edits reuse
the shared resolver and gate rather than their own acceptance rules.

| Responsibility                                                                  | Start here                                                          |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Load source and retain in-memory chunk/file artifacts and import indexes        | `prepare_chunks.rs`, `program_analysis.rs`, `artifacts/artifact.rs` |
| Normalize module paths and parse JS-list input                                  | `artifacts/paths.rs`                                                |
| Define serialized report models and write JSON reports                          | `artifacts/reports.rs`                                              |
| Compute output/decomposition metrics and file/directory reports                 | `artifacts/metrics.rs`                                              |
| Read flat/tree specs and authoring module documents                             | `spec/`                                                             |
| Compile spec claims and share exact selector resolution                         | `spec_resolution.rs`, `selectors/resolution/`                       |
| Match, synthesize, and diagnose selectors                                       | `selectors/{matching,authoring,diagnostics}/`                       |
| Analyze statement effects, dependencies, and structural atoms                   | `facts/`, `purity/`, `graph/`, `chunk_analysis/`                    |
| Model synchronous values, collect callable execution facts, and interpret calls | `facts/sync_effects/{value_model,execution_index,interpreter}.rs`   |
| Assemble assignments and validate realizability                                 | `assignment_assembly.rs`, `gate.rs`, `realizability/`               |
| Lower accepted assignments to entry and logical-module files                    | `lowering/`                                                         |
| Plan/apply vendor substitution and finalize emitted files                       | `vendor/`, `artifacts/emission_files.rs`                            |
| Render and write finalized JavaScript files and output reports                  | `artifacts/script_output.rs`                                        |
| Accept concrete syntax, replacements, and browser loading before writing        | `reconstruction.rs`, `artifacts/browser_harness_plan.rs`            |
| Propose moves or safe module refinements; inspect or edit spec                  | `peel/`, `cli/`                                                     |

`pipeline.rs` composes these stages. Preparation returns a `SourceProgram` with
its matching syntax and path indexes; finalized `EmissionFiles` own output files
and their output indexes. These are different phase types even when a pass-through
program reuses the prepared indexes.

Assignment gates check module realizability. Final run acceptance also checks
unresolved claims, reconstructed exports, and replacement consumers;
writers require the resulting immutable `ValidatedPlan`.

Docs: splitting correctness and implementation entry points <docs/design.md>
(§ File references); selector semantics <SPEC.md>, <docs/selector_resolution.md>;
cross-command CLI semantics (env vars, output formats, validation defaults, batch
atomicity, gate queries, other invocation shapes) <docs/cli.md>, per-command flags
`debundle <command> --help`; selector authoring <docs/selectors.md>; module/binding
editing <docs/spec_editing.md>. Work queue <TODO.md>; `plans/` designs, `perf/`
measurements, `docs/lessons_learned/` rejected approaches. Frozen specimen copies
elsewhere in the repo are not the live implementation.

## Bazel integration and profiling

`pipeline.bzl`'s `debundle_pipeline` runs `debundle run` as a build action; profile
with `perf_wrapper.sh`: <docs/bazel_integration.md>.

## Spec features

- **Unassigned source:** `unassigned_mode` keeps unclaimed code in the entry,
  collects it in a catchall file, or decomposes it into one module per atomic
  group. Policies and generated paths: <SPEC.md#unassigned-source>.

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
