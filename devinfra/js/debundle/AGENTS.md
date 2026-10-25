@README.md

# Debundler Implementation Constraints

Read <docs/design.md> (problem, correct emit strategies, the realizability theorem
the validator enforces) before working on the splitting pipeline.

## Spec authoring

- Invest in tools and side-output analyses that make spec authoring cheap; the
  pipeline emits machine-readable artifacts for suggesting owner/module splits and
  renames.
- Treat each spec as the exact contract for one configured input program. Do not
  add cross-version reasoning, multi-spec comparison, or automatic promotion or
  migration commands. For a new upstream snapshot, use the authoring workflow in
  <README.md>: inspect that source, review diagnostics and candidate selectors,
  apply only justified edits, validate the exact source/spec pair, and report
  remaining mismatches.
- Heuristics produce diagnostics and candidate sets only; the spec author decides.
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
the parsed input file, not a prepared owner graph; check the printed range first. A
`matched` explanation is local and never replaces `spec validate` or the gate;
`limited`/`unsupported` are inconclusive; a failed backtracking branch is not proof
of a matcher bug — reduce the range/pattern and establish a supported control first.

## Soundness over completeness

A validator-accepted spec must emit a bundle that runs correctly; over-restriction
is acceptable, under-restriction is a soundness bug. Conditionally-correct analyses
check their precondition and fall back conservatively — never widen a rule because
the common case is fine. If the easy fix isn't the deepest correct fix, do the deeper
one or stop and explain the blocker. Theorem: <docs/design.md> § Soundness over
completeness. Read <docs/purity_soundness.md> before touching
<purity/whitelists.rs> or adding a purity annotation to a spec.

## Testing

- **Default: e2e through the `debundle` CLI** (`e2e/`, `e2e/support/`), even for a
  bug in one internal stage. Assert the external contract (module exports, entry
  output under Node, layout, source shape).
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
