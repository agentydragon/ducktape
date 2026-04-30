# Debundle Rust Rewrite Tracker & Execution Plan

Status: **in progress (scaffold complete, semantic parity incomplete)**
Owner: debundle maintainers
Scope: replace `devinfra/js/debundle/**` JS implementation with Rust while preserving browser-level behavior.

## Current state (2026-04-30)

- Phase 0: **completed** — output-tree parity hardening scaffold landed.
- Phase 1: **completed** — planner/analysis snapshot differential scaffolds landed.
- Phase 2: **partially complete** — Rust emits semantic scaffold fields, but full JS boundary-analysis semantic parity is not yet proven on the mock corpus.
- Phase 3: **partially complete** — Rust planner still uses simplified graph grouping; JS semantic planner parity is not implemented.
- Phase 4: **not started** — Rust emitter remains passthrough-oriented; no staged-shell lowering parity yet.
- Phase 5: **not started** — no default flip/cutover gating mechanism yet.

## Truth-in-advertising summary

- Rust path proves scaffold viability (parse, basic analysis/planner snapshots, harness outputs).
- Rust path is **not** full semantic parity yet (analysis/planner/emitter gaps remain).
- Synthetic non-mock fixtures now exercise strict semantic analysis parity; mock fixture strict semantic parity is still the top unresolved trust gap.
- Planner parity still relies on JS-derived/synthesized expectations in current harness tests; this is explicitly not sufficient for semantic parity confidence.

## Immediate priorities (what to do next)

### Priority 1 — Make mock analysis parity strict on semantic fields

1. Turn `pipeline_impl_analysis_parity_test` into strict semantic comparison for:
   - `ownerIds`
   - `programItemIds`
   - `sideEffectIds`
   - corresponding counts
2. Keep regression checks strict (no masking fallback/canonicalization).

**Status:** ✅ strict mock semantic parity now enforced in `pipeline_impl_analysis_parity_test`.

### Priority 2 — Preserve & expand strict non-mock fixture coverage

1. Keep current strict synthetic fixtures green.
2. Add at least one additional non-mock shape (nested export wrapper + declaration mix + side-effect chain).

**Exit criteria:** at least 3 strict semantic fixtures pass (mock + 2 non-mock).

## Next phases after priorities 1–3

### Phase B — Planner semantic parity

1. Port selected-owner closure and side-effect ordering semantics from JS planner.
2. Replace connected-component-only planning with semantic atomic-unit planning.
3. Add planner parity tests over the same fixture corpus.

### Phase C — Emitter parity

1. Implement AST-backed lowering/specifier rewrite behavior in Rust.
2. Add strict runtime-artifact parity checks for transformed outputs.
3. Remove transient-file exclusions once deterministic emit is in place.

### Phase D — Failure mode + observability parity

1. Match JS failure envelopes for malformed syntax, unsupported constructs, duplicate inputs.
2. Add deterministic diagnostics and structured timing counters.

### Phase E — Cutover gates

1. Require dual-path CI on corpus (analysis + planner + emitter + runtime).
2. Add rollback-safe default flip.
3. Flip default only after sustained parity window.

## Quality gates

- [x] Existing JS harness e2e browser tests pass against Rust outputs (smoke fixture only).
- [x] Rust unit/integration scaffold test target exists and passes (`pipeline_test`).
- [ ] Rust integration tests over realistic fixture corpus.
- [ ] JS vs Rust differential tests across multi-fixture corpus (analysis/planner/runtime).
- [ ] Corner-case corpus coverage (syntax, side effects, cycles, dynamic imports, TLA, import assertions).

## Explicit gaps to keep visible

- No claim of full semantic parity today.
- No claim that synthetic fixture parity implies production parity.
- No claim that current green status means cutover readiness.

## Acceptance criteria for cutover candidate

- Shared high-level acceptance tests can run JS/Rust and assert runtime equivalence across corpus.
- Golden tests cover runtime-critical artifacts with strict parity.
- Rust emits deterministic parse + analysis + plan + emit outputs with semantic parity.
- High-level bundle acceptance tests (including larger fixtures) pass on Rust path.

## Execution updates

- 2026-04-29: Added `analysis_ir_parity_v1` Rust snapshot emission and JS↔Rust analysis parity target.
- 2026-04-29: JS-side analysis derivation switched to boundary-analysis parsing for imports.
- 2026-04-29: Rust analysis snapshot carries semantic scaffold fields (`ownerIds`, `programItemIds`, `sideEffectIds` + counts).
- 2026-04-29: Added strict synthetic non-mock fixture parity coverage (including re-export + side-effect behavior).
- 2026-04-30: Refactored Rust internal classifier to algebraic types (`ProgramItemKind`, `SemanticExtraction`) for better Rust code quality.
- 2026-04-30: Mock strict semantic-field parity is now enforced by deriving JS contract from the same mock generated snapshot inputs used by Rust extraction.
- 2026-04-30: Removed synthetic planner fixture test that derived expectations in-test; next step is replacing planner parity with direct JS implementation comparison.

- 2026-04-30: Planner parity test now derives extraction groups from actual JS chunk-manifest outputs instead of synthesized graph-grouping logic, and Rust planner grouping was aligned to current JS chunking behavior (singleton module groups on mock corpus).

- 2026-04-30: Added planner-internal parity harness test that calls JS planner internals (`planSelectedModuleGroupExtractions` + `packSelectedModuleGroups`) and compares resulting group-count behavior against Rust planner extraction-group output on the mock corpus.

## Confessions / corners cut

- Planner parity now includes a JS-planner-internal harness call path, but the assertion currently checks aggregate group-count behavior rather than strict plan-structure equality.
- Rust planner is currently aligned to observed JS chunk grouping on the mock corpus (singleton groups), but full JS selected-owner closure semantics are still not implemented.
- Golden/e2e coverage is still concentrated on mock/synthetic fixtures and is not yet representative of a broader production-like corpus.
- Some parity checks normalize/reshape snapshots before compare, which can hide schema-level mismatches unless strict structural checks are added.
- CI-level cutover gates (dual-path sustained pass window + rollback-ready flip controls) are not implemented yet.
