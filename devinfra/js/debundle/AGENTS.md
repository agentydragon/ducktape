@README.md

# Debundler Implementation Constraints

Read <docs/design.md> (problem, correct emit strategies, the realizability theorem
the validator enforces) before working on the splitting pipeline.

## Spec authoring

- Plans hold outstanding work only. Consolidate overlapping placement plans;
  keep enduring graph contracts, proofs and failed-approach lessons in `docs/`,
  then remove completed or superseded plans. Follow the repository's
  <../../../README.md#plans> lifecycle rule.
- Invest in tools and side-output analyses that make spec authoring cheap; the
  pipeline emits machine-readable artifacts for suggesting source-unit/module splits and
  renames.
- Record encountered tooling friction in this project's `TODO.md` or `debug/`
  and commit it: confusing or insufficient output, unproductive investigation
  paths, and missing guidance. Fix small issues directly; preserve larger
  follow-ups with the observed behavior and a concrete next action.
- Treat each spec as the exact contract for one configured input program. Do not
  add cross-version reasoning, multi-spec comparison, or automatic promotion or
  migration commands. For a new upstream snapshot, use the authoring workflow in
  <README.md>: inspect that source, review diagnostics and candidate selectors,
  apply only justified edits, validate the exact source/spec pair, and report
  remaining mismatches.
- Heuristics produce diagnostics and candidate sets only; the spec author decides.
- Use `modules check` for explicit destination batches. Whole-assignment acceptance
  includes structural atoms and ESM evaluation under the supplied chunk priority;
  default checks also preflight the source writer before returning a plan.
  Use explicit `--graph-only` for graph inspection/oracles; it never returns a
  plan. Local probes and proposal readiness labels cannot replace a complete
  check. Recheck combined batches, and keep graph acceptance distinct from
  selector realization and emitted runtime evidence.
- For residual placement, use `modules propose` on the current graph or the
  configured corpus (`--graphs` plus `--tree-config`). Start without selecting
  already-known movable units. Inspect the discovered groups in source.
  Review the full mapping, check and preview/apply its saved plan, and run
  materialization plus relevant behavior
  checks. A completed negative rules out only the advertised append-one-owner,
  baseline-preserving family of entry-residual peels. Unsupported routing,
  interruption, errors and empty output are not exhaustion. Derive all-seed
  feasibility from atom closure SCCs and event bounds; expand one selected
  closure and certify it once. Never fall through to partition enumeration,
  arbitrary schedule solving or an unnamed heuristic. Keep graph
  acceptance distinct from selector realization and runtime evidence. The CLI
  workflow is in <docs/module_proposals.md>; the construction proof is in
  <docs/placement_algorithm.md>. Preserve the real-source CLI and property/oracle
  tests when changing candidate generation, scheduling, gate checks, or plan
  application.
- Selected-ownership plans use chunk-local owner paths. Stage new ordinary
  selectors in the captured view's `placements` map, preserve the complete base
  premise/anchor tree, and compare the realized complete assignment with the
  graph-checked candidate before offering or applying an executable plan.
  Corpus proposals share source/base preparation only for an identical verified
  capture. Consume each resolved target once; candidate claims and priorities
  stay local. Preserve multi-chunk runtime and input-invalidation coverage.
- Materialized chunks require `import_order` with every nonentry emitted owner
  exactly once. Check that concrete witness through the shared verifier and
  retain it through emission. Never repair it implicitly. Plans carry ordering
  and ownership together; changed snapshots or combined plans require recheck.
- Do not clone/project the entire source graph or run global certification per
  inventory seed. Prepare the canonical finest quotient once and derive
  summaries on its SCC DAG. Compare cheap decisions with independent small
  subset/import-list oracles; certify the selected complete plan once.
- When a new bundle shape exposes a missing capability, extend the debundler,
  reproducing the failure minimally first. When debundle tooling blocks a task,
  follow <docs/bug_reproductions.md> before working around it. Keep private source
  out of public bug reports and fixtures.

## Profiling

`-c opt` binary, pre-staged inputs, `perf_wrapper.sh` (<docs/bazel_integration.md>
§ Profiling). Progress markers: `perf_record_stderr.txt`; self-cost:
`perf_report_flat_symbols.txt`; callgraphs: the children report. Keep production
telemetry coarse.

## Parallel workers

Dispatch with the Agent tool's `isolation: "worktree"`. On signs of missed isolation
(another agent's files in your diff, vanished work, foreign branch files in
`git status`), bail and re-dispatch isolated; don't recover from contamination. The
orchestrator keeps the main worktree and never disturbs in-flight worker trees.

**Signing (web sessions)**: `/tmp/code-sign` (`gpg.ssh.program`) works only with cwd
`/home/user/ducktape`; in a worktree it fails `status 400: missing source`. Ratified
narrow exception: ephemeral worker worktrees commit `--no-gpg-sign` and push unsigned
(squash/merge signs on `devel`); the orchestrator in the canonical path signs
normally.

## AST only

Transform JS via AST operations on the SWC parse — never text/regex rewrites or ad
hoc patching. Syntax-derived facts come from the single SWC parse in
`prepare_js_chunks` via `ChunkManifest` / the `ArtifactIndexes` query API, not
phase-local rescans; mutation passes use an AST visitor to apply a rewrite but take
the decision from the manifest. A question the manifest can't answer → extend the
shallow analysis once, don't re-derive it in another stage.

## Selector resolution

Read <docs/selector_resolution.md> before touching
`selectors/resolution/selector_resolve.rs`, `selectors/matching/source_match/`,
`selector_ir_lowering` or `selector_constraint_model_builder`. Commands never decide
exactly-one themselves; they call `selector_resolve`. § Rejected: tree matching as
solver constraints and § Rejected: localizing a contradiction with assumption cores
are measured dead ends — reopen only with a new measurement.

**Debugging a selector**: <docs/debugging_selectors.md>. Statement indices belong to
the parsed input file, not a prepared dependency graph; check the printed range first. A
`matched` explanation is local and never replaces `spec validate` or the gate;
`limited`/`unsupported` are inconclusive; a failed backtracking branch is not proof
of a matcher bug — reduce the range/pattern and establish a supported control first.

## Soundness over completeness

A validator-accepted spec must emit a bundle that runs correctly; over-restriction
is acceptable, under-restriction is a soundness bug. Conditionally-correct analyses
check their precondition and fall back conservatively — never widen a rule because
the common case is fine. Choose general inference, sound specific recognition or
an audited annotation using <skills/shared/workflow.md#choose-the-solution-depth>.
A sound scoped solution can be the lasting choice. Theorem: <docs/design.md>
§ Soundness over completeness. Read <docs/purity_soundness.md> before touching
<facts/abstract_domain/builtins.rs> or adding a purity annotation to a spec.

## Testing

- Name changed Rust library targets explicitly in Bazel validation, alongside
  their tests. Dependent E2E tests can pass without running a library's own
  Clippy check. Remove orphaned production helpers with their callers.
- **Default: e2e through the `debundle` CLI** (`e2e/`, `e2e/support/`), even for a
  bug in one internal stage. Assert the external contract (module exports, entry
  output under Node, layout, source shape).
- **Gate fixes need a behavioral regression test.** For an incorrect acceptance
  or rejection, give a minimal bundle and spec to the real pipeline and assert
  the correct result: a valid split is accepted and emitted, or an invalid split
  is rejected with the relevant error. Verify that the same bundle and spec
  reproduce the bug before the fix. Check emitted behavior where relevant.
  Analysis reports and implementation-specific facts may supplement this test,
  but are not its sole oracle: internal representations and inference strategies
  can change without changing whether the split is valid.
- Synthetic fixtures use neutral descriptive names (`factory`, `lazyLoader`) or
  simple placeholders (`a`, `b`, `foo`, `bar`). Never carry arbitrary minified
  identifiers from the source being reproduced into a fixture.
- Use the assertion helpers re-exported by `e2e/support/mod.rs`
  (`assert_module_exports`, `assert_module_source`, `assert_entry_output`, …); add a
  helper for a repeated shape — a test is one or two helper calls, not `fs::read` +
  substring matches.
- Unit tests only for combinatorial corner cases of pure functions or invariants
  awkward to expose via the CLI.
- **Forbidden**: mocking the pipeline or a stage; hand-constructing intermediate
  artifact/manifest types when a real-pipeline fixture reaches the same path.

## Rust shapes

- Semantic passes take `AnalysisInputs`: keep author contracts in
  `AnalysisAssumptions`, separate from imported summaries and analysis policy.
  An asserted contract is not an inferred source fact.
- Typed structs/enums, not string discriminators or `Map<String, Value>` mirrors of
  JSON. File roles are `FileRole`. Owned serialized enums use
  `#[serde(rename_all = "snake_case")]` unless an external contract says otherwise.
- `serde_json::Value` only for genuinely polymorphic values (spec args,
  `#[serde(flatten)]` slots) or raw external input.
- Owned JSON gets no compatibility envelopes (`kind`, `schema_version`, stage lists)
  without a real external consumer. Flat specs carry inputs and author decisions
  only; analysis provenance (`evidence`, `confidence`, `export_shape`) goes in docs
  or diagnostic side outputs, not `VendorMark`.

## Spec

- Flat spec top-level maps (`inputs`, `vendor`, `logical_modules`,
  `unassigned_mode`, `chunk_renames`) are typed serde structs in <spec/spec.rs>;
  transforms run in a fixed canonical order. Semantics/workflow:
  <docs/spec_editing.md>.
- Module-specifier path math uses `relative-path` and the artifact path helpers,
  never hand-rolled segment splitting.
- **Deviation** from STYLE.md (every field needs a reader): spec `note:` fields are a
  ratified exemption, since the rewriters drop `#` comments.

## Enum names

All Debundle-owned enum identifiers in specs, CLI values, and serialized
reports use `snake_case`. Derive the spelling with `serde`, `strum`, or Clap
rather than maintaining per-variant names or aliases. Human explanations and
external JavaScript module specifiers may use their natural syntax; convert at
the boundary. Update downstream consumers when changing enum names.
