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

Implementation entry points for the splitting pipeline are in
<docs/design.md> § File references. Use <docs/cli.md> to find inspection and
editing workflows; `debundle <command> --help` lists current flags.

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

Each line of `js-files.txt` normally names one `.js` file below `inputs.root`.
For an extracted file given a stable alias, write `alias.js<TAB>original/path.js`
instead. Debundle loads `alias.js` as the chunk named `alias`, while resolving
imports of `original/path.js` to that same chunk. List the original path only
once: an aliased file must not also have its own line. This keeps hashed source
paths out of specs without treating identical file contents as identical
modules.

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

Use `comment:` for annotations emitted into generated JS and `note:` for
YAML-only authoring metadata. The schema, emission and move semantics, and
CLI examples live in <docs/spec_editing.md> § Comments and notes.

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

The emitted binding, chunk exports, and cross-chunk import behavior for
spec-assigned names are specified in <SPEC.md> § Readable names and the chunk
public surface.
