# Debundle Rust Rewrite Tracker

Status: **in progress — not at JS semantic parity yet**
Last updated: 2026-05-01 (frontier identity normalized by seed-owner scope; first remaining mismatch is semantic)
Owner: debundle maintainers

## Scope and strict parity bar

This tracker documents **material algorithmic divergences** between:

- JS source of truth pipeline (`analysis/*`, `extract/*`, `split/*`, `harness/*`), and
- Rust rewrite pipeline (`rust/pipeline.rs`, `rust/plan.rs`, `rust/emit.rs`).

Parity means **exactly the same algorithmic behavior**:

1. same semantic inputs,
2. same intermediate planner state,
3. same candidate set and blocking payloads,
4. same selected output,
5. same emitted artifacts,
6. same runtime/e2e outcomes.

---

## Current status

Active blocker: Stage 3B frontier/component identity + closure parity remains red in `planner_internal_parity_test`.

Minimal reproducer target:

- `//devinfra/js/debundle/harness:planner_internal_tiny_repro_test`
- Uses an in-test reduced chunk (`seen` + `__vitePreload` + one `await` call).
- Emits side-by-side state rows for seed metadata, component IDs, and closure owner sets.
- Asserts first divergence lands in `requiredClosureOwnerIds`.

## 2026-04-30 conformance run log

Attempted parity conformance targets:

- `bazelisk test //devinfra/js/debundle/harness:planner_internal_parity_test --remote_executor="" --remote_cache="" --noremote_accept_cached --noremote_upload_local_results --config=nolint --test_output=errors`

Result:

- Local-only `bazelisk` run completed build+execution without BuildBuddy remote execution, and the parity test failed at the Stage 3B frontier gate (`requiredClosureOwnerIds`).

Interpretation:

- This session produced a fresh local parity signal and reconfirmed the first failing seed remains `owner_component_0002` with under-expanded Rust closure owners.
- Next parity work should continue algorithm-level Stage 3B closure expansion debugging (dependency-edge and seed-closure equivalence), not infrastructure recovery.

### 2026-04-30 progress update (this patch)

- ✅ **Stage 1 fallback removal tightened in Rust analysis path**:
  - missing module analysis for a chunk now fails fast,
  - missing parsed AST for a chunk now fails fast,
  - missing per-owner uses/writes maps now fail fast,
  - missing owner declaration line now fails fast.
- ✅ Re-ran local parity gate with remote execution disabled; first failing frontier seed remains unchanged.
- 🔴 Active first red gate is still `requiredClosureOwnerIds` at `owner_component_0002` (Rust under-expands closure owners vs JS).

### 2026-05-01 progress update (this patch)

- ✅ Rust component dependency seeding now includes all `local_declaration` accesses with an owner id (JS-equivalent `selectedModuleAccessView(owner).all` shape).
- ✅ Rust eager dependency owner collection now includes eager `read` accesses in addition to eager writes/member-writes (closer to JS `eagerReadLike` semantics).
- ✅ Rust owner-component SCC/dependency construction now ignores cross-module dependency edges, matching JS selected-module componentization scope (per analyzed module/chunk).
- ✅ Planner-internal parity harness gate now compares frontier values by normalized seed-owner key (not array index), eliminating false-positive order-only deltas and reporting the true first semantic mismatch seed.
- ✅ Rust owner-record dependency derivation now uses normalized `dep_edges` as the source of planner dependency seeds (instead of raw access filtering), to mirror the dependency view already materialized during analysis.
- 🔴 First known semantic red gate remains `requiredClosureOwnerIds`; closure expansion is still under investigation.

### 2026-05-01 reanalysis checkpoint (post seed-keyed gate + owner-edge closure attempt)

Latest local parity gate run:

- `bazelisk test //devinfra/js/debundle/harness:planner_internal_parity_test --remote_executor="" --remote_cache="" --noremote_accept_cached --noremote_upload_local_results --config=nolint --test_output=errors`

Current first semantic mismatch is unchanged:

- gate: `requiredClosureOwnerIds`
- seed key: `owner_00002`
- Rust: `["owner_00002"]`
- JS: `["owner_00000","owner_00001","owner_00002","owner_00003"]`

Latest trace delta context:

- Rust `requiredComponentIds`: `["owner_component_0000"]`
- JS `requiredComponentIds`: `["owner_component_0002"]`
- Rust `seedComponentDepOwnerIds`: `[]`
- JS `seedComponentDepOwnerIds`: `[]`

Interpretation:

- Seed-keyed comparator confirms this is not ordering noise.
- Both sides agree the seed is `seen` (`owner_00002`) but Rust still materializes a singleton closure where JS materializes a 4-owner closure.
- The remaining divergence is now best treated as **component membership / owner-edge extraction parity**, not frontier identity or trace keying.

### Precise divergence point identified (JS vs Rust, parallel dive)

After instrumenting `planner_internal_parity_test` to print full trace records at the first mismatch, we confirmed:

- Rust and JS report the same `requiredComponentIds` (`["owner_component_0002"]`) at the failing index.
- But they attach different owner closures to that same component id.

This isolates a concrete algorithmic mismatch boundary:

1. **JS path** runs planner componentization per chunk (component IDs are chunk-local and restart per chunk analysis).
2. **Rust path** runs planner componentization over the aggregated multi-chunk analysis snapshot (component IDs are global in one graph).

Therefore, the current parity comparator was aligning traces by `seedComponentId` labels that are **not globally stable across JS chunk runs**, while Rust labels were global.

### Identity normalization progress (done)

- Parity frontier gate comparison now keys traces by `seedOwnerIds` (owner-closure seed scope) rather than raw `seedComponentId`.
- This makes JS/Rust frontier identity comparable before semantic comparison.

### First remaining mismatch after identity normalization (semantic)

- Gate: `requiredClosureOwnerIds`
- Comparable seed key: `owner_00002`
- Rust values: `["owner_00002"]`
- JS values: `["owner_00000","owner_00001","owner_00002","owner_00003"]`

Interpretation: first red gate is now a **true semantic closure-expansion divergence**, not a namespace/labeling mismatch.

### Closure-propagation edge instrumentation (done)

- Added seed-scope dependency-edge trace fields in parity plumbing:
  - JS frontier trace now carries `seedComponentDepOwnerIds` (when available from planner payload).
  - Rust frontier trace already carries `seed_component_dep_owner_ids`.
- Current observation:
  - For seed owner key `owner_00003`, Rust reports dep-owner frontier `["owner_00000","owner_00001","owner_00002"]`,
  - JS frontier payload currently reports empty `seedComponentDepOwnerIds` even when closure includes `owner_00000..00003`.

Interpretation: JS exported frontier payload does not currently expose the same direct-edge payload shape as Rust for this seed, so semantic closure parity should continue to gate on `requiredClosureOwnerIds`/`closureOwnerIds` while edge payload capture is being standardized.

## Pipeline-ordered divergence list (start → end)

The list below is the **current exhaustive inventory of material differences** between Rust and JS, ordered by pipeline execution order.

| Order | Pipeline slice                                                  | Rust path materially differs from JS by...                                                                                       | Impact on parity signal                                                           |
| ----- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| 1     | Analysis IR/access modeling                                     | Rust still has residual fallback paths and ordering risk around access descriptor materialization vs JS access view exactness.   | Can shift dependency edges and every downstream planner decision.                 |
| 2     | Ordered-init planner state                                      | Replayability/runtime-sensitive derivation is close, but not yet proven identical for every side-effect/touched-owner edge case. | Can change attachability + blocking payloads even when closure IDs match.         |
| 3     | Dependency components + closure expansion (active critical gap) | Rust required-closure expansion remains under-inclusive for at least one seed (`owner_component_0002`) compared to JS.           | First failing gate in `planner_internal_parity_test` (`requiredClosureOwnerIds`). |
| 4     | Contiguous envelope/barrier semantics                           | Rust envelope barrier and expansion payload parity is not fully locked to JS trace semantics.                                    | Candidate universe can diverge before ranking/packing.                            |
| 5     | Candidate identity/canonicalization                             | Rust candidate identity/order/signature materialization is not yet guaranteed one-to-one with JS.                                | Produces non-identical candidate sets/debug state.                                |
| 6     | Staged-shell lowering (`stageRuns`, `shellItemIds`)             | Rust scaffolding exists, but rule-level lowering parity is not yet proven exact.                                                 | `planner_parity_test` remains red downstream.                                     |
| 7     | Blocking reason payload construction                            | Some class payload derivation in Rust remains heuristic vs JS exact class-specific construction.                                 | Blocking reason payload diffs remain possible even with similar candidate owners. |
| 8     | Selection/packing tie-break + occupancy edge rules              | Rust preselection/collision/tie-break behavior is not fully locked to JS.                                                        | Selected set and final planner debug can diverge.                                 |
| 9     | Emitter/lowering artifact generation                            | Rust output tree/textual artifact equivalence to JS is not yet guaranteed.                                                       | `rust_e2e_test`/`dual_e2e_test` failures persist.                                 |
| 10    | Runtime/e2e behavior parity                                     | JS runtime path is green; Rust runtime path still diverges under parity fixtures.                                                | End-to-end parity not achieved.                                                   |
| 11    | Corpus + CI enforcement                                         | Rust parity gates are not yet sustained across full representative corpus in CI.                                                 | No safe default-flip/cutover readiness.                                           |

## Parity test matrix (current known state)

| Test target                                                                 | Scope                                           | Current known state                      | Notes                                                                               |
| --------------------------------------------------------------------------- | ----------------------------------------------- | ---------------------------------------- | ----------------------------------------------------------------------------------- |
| `//devinfra/js/debundle/harness:analysis_parity_test`                       | Analysis IR parity                              | 🟢 Passing (last recorded)               | Mentioned green in prior local-only parity run.                                     |
| `//devinfra/js/debundle/harness:ordered_init_parity_test`                   | Ordered-init planner state parity               | 🟢 Passing (last recorded)               | Dedicated ordered-init harness was green in prior update.                           |
| `//devinfra/js/debundle/harness:planner_internal_tiny_repro_test`           | Minimal closure mismatch reproducer             | 🟢 Passing (expected mismatch assertion) | Passes when divergence is detected and first mismatch is `requiredClosureOwnerIds`. |
| `//devinfra/js/debundle/harness:planner_internal_parity_test`               | Planner-internal full frontier/candidate parity | 🔴 Failing                               | First mismatch remains `requiredClosureOwnerIds` for seed-owner scope.              |
| `//devinfra/js/debundle/harness:pipeline_impl_analysis_parity_fixture_test` | JS/Rust analysis pipeline fixture parity        | 🟢 Passing (last recorded)               | Mentioned green in prior local-only parity run.                                     |
| `//devinfra/js/debundle/harness:js_e2e_test`                                | JS pipeline end-to-end behavior                 | 🟢 Passing (last recorded)               | JS path remains green.                                                              |
| `//devinfra/js/debundle/harness:planner_parity_test`                        | Higher-level planner/lowering parity            | 🔴 Failing (by dependency)               | Expected red while planner-internal Stage 3 closure parity is red.                  |
| `//devinfra/js/debundle/harness:rust_e2e_test`                              | Rust rewrite end-to-end parity                  | 🔴 Failing                               | Downstream parity remains red until planner and lowering parity closes.             |
| `//devinfra/js/debundle/harness:dual_e2e_test`                              | JS vs Rust artifact/runtime parity              | 🔴 Failing                               | End-to-end parity not yet achieved.                                                 |

## Stage 1 — Analysis IR contract parity (pipeline entry)

### Current Rust state

- Rust now carries explicit owner-access records (`kind`, `access_kind`, `phase`, `owner_id`, `name`).
- Dependency/eager seeds derive from access records rather than token-only heuristics.
- Unresolved accesses are represented as runtime-import-like accesses.

### Remaining gap to exactness

- Confirm full one-to-one parity with JS access descriptor semantics and ordinal ordering used downstream.
- Eliminate any residual fallback paths that do not originate from JS-equivalent access modeling.

### Required done condition

- Analysis parity fixtures assert exact access-record equivalence (including ordering) for JS vs Rust.

---

## Stage 2 — Ordered-init planner state parity

### Current Rust state

- Rust includes ordered-init scaffolding with replayable side-effect maps and runtime-sensitive/touched-owner fields.
- Attachability uses this state more than before.

### Remaining gap to exactness

- Replayability predicate is still not proven equivalent to JS.
- Runtime-sensitive source and touched-owner derivation still have parity risk.
- Planner parity test currently fails with ordered-init state present in mismatch context.

### Required done condition

- `replayableSideEffectIdsByOwnerId` and `replayableSideEffectStateById` deep-equal JS with no normalization exceptions.

### 2026-04-30 progress update

- Added dedicated strict ordered-init parity harness target: `//devinfra/js/debundle/harness:ordered_init_parity_test` (green).
- Rust ordered-init map builder now:
  - pre-seeds owner-key map entries for all owners,
  - sorts/dedups side-effect ids per owner for deterministic parity assertions.
- Remaining failure has moved upstream in the pipeline: candidate universe / closure parity still diverges in `planner_internal_parity_test` before ordered-init assertions are the limiting factor.

---

## Stage 3 — Dependency component + closure formation parity

### Current Rust state

- Rust now builds owner SCC components and derives closure candidates from component-level transitive dependency closure.
- Dependency edges are constrained to eager local-declaration forward-style accesses for component construction.
- Rust now performs contiguous-envelope component expansion over owner ordinals; remaining behavior is still simplified vs JS in envelope barrier handling and closure identity ordering.

### 2026-04-30 progress update

- Rust closure seeding now starts from owner **SCC components** with component-level transitive dependency closure expansion (Stage 3 kickoff implementation).
- Owner dependency edges used for component construction are now constrained to eager local-declaration forward-dependency-like accesses (`phase: eager`, `access_kind in {read, write, member_write}`).
- Rust contiguous-envelope growth now applies explicit program-item barrier categories via ordinal map: missing program item, non-declaration program item, and missing declaration barriers all stop expansion.
- Missing-owner-to-component mapping also stops envelope expansion (JS-equivalent barrier category).
- Closure candidates are now deduped by closure owner-set signature before packing, and candidate IDs are re-numbered contiguously from surviving closure order to reduce identity drift.
- Planner-internal harness now includes explicit pre-packing candidate-universe assertions before selected/packed comparisons to isolate Stage-3 drift earlier in the pipeline.
- Result: ordered-init parity remains green, but `planner_internal_parity_test` remains red; remaining drift is concentrated in envelope barrier completeness and closure identity/order parity.

### Remaining gap to exactness

- Candidate universe can diverge before ranking/packing.
- Closure identities and expansion frontier rules are not yet locked to JS behavior.

### Required done condition

- Pre-packing candidate universe parity (IDs, owner sets, closure membership/order) is exact.

---

## Stage 4 — Staged-shell batch construction parity

### Current Rust state

- `stageRuns`/`shellItemIds` scaffolding exists.

### Remaining gap to exactness

- Expansion/interleave/materialization rules are not guaranteed one-to-one with JS.

### Required done condition

- Deep-equal parity for `stageRuns`, `shellItemIds`, `semanticOwnerIds`, `semanticMemberNames`.

---

## Stage 5 — Blocking reason class + payload parity

### Current Rust state

- Class names/order are closer to JS.

### Remaining gap to exactness

- Some payload construction remains heuristic.
- Eager/shell payloads are not fully guaranteed to derive only from parity access/planner state.

### Required done condition

- Per-class payload parity (content, ordering, dedup) is exact for candidate and selected views.

---

## Stage 6 — Selection/packing parity

### Current Rust state

- Preselection and occupancy scaffolding exist.

### Remaining gap to exactness

- Edge rules for collisions, preselected interactions, and tie-break ordering are not yet guaranteed exact.

### Required done condition

- Selected IDs and full debug payload match JS exactly.

---

## Stage 7 — Emitter/lowering artifact parity

### Current Rust state

- Rust emitter path exists but is not yet validated as exact JS-equivalent lowering.

### Remaining gap to exactness

- Generated artifact trees can still diverge semantically and textually beyond allowed normalizations.

### Required done condition

- Strict artifact parity on parity corpus (with explicit, documented non-semantic exclusions only).

---

## Stage 8 — Runtime/e2e parity

### Current Rust state

- JS e2e target passes; Rust and dual e2e targets currently fail.

### Remaining gap to exactness

- Runtime behavior differs once Rust planner/emitter output is exercised end-to-end.

### Required done condition

- Rust e2e and dual e2e both pass consistently using the same fixtures and assertions as JS.

---

## Stage 9 — Corpus + CI gate parity (promotion readiness)

### Current Rust state

- Parity signal is still concentrated on mock/synthetic paths with partial non-mock coverage.

### Remaining gap to exactness

- Need sustained strict parity across mock + representative non-mock bundle fixtures.

### Required done condition

- CI enforces planner + artifact + runtime parity gates over mock and non-mock corpus before default flip.

## Consolidated forward plan (merged from plans/debundle_rust_parity_plan.md)

1. Normalize component identity namespace to match JS per-chunk componentization semantics.
2. Re-run Stage 3B gates (`requiredComponentIds`, `requiredClosureOwnerIds`) and lock first-divergence seed.
3. Close remaining envelope payload parity (`contiguousEnvelope*` fields) once component identity is stable.
4. Reach candidate-universe deep equality, then progress to staged-shell (`stageRuns`, `shellItemIds`).
5. Close blocking-reason payload and selection/packing edge-rule parity.
6. Validate emitter/runtime parity (`rust_e2e_test`, `dual_e2e_test`) and then enable CI parity gates.
