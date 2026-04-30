# Debundle Rust Rewrite Tracker

Status: **in progress — not at JS semantic parity yet**
Last updated: 2026-04-30 (program-item envelope barriers ported in Rust planner)
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

## Current observed state (2026-04-30)

From the latest parity/e2e harness run:

### Passing

- `//devinfra/js/debundle/harness:analysis_parity_test`
- `//devinfra/js/debundle/harness:pipeline_impl_analysis_parity_fixture_test`
- `//devinfra/js/debundle/harness:js_e2e_test`

### Failing

- `//devinfra/js/debundle/harness:planner_parity_test`
- `//devinfra/js/debundle/harness:rust_e2e_test`
- `//devinfra/js/debundle/harness:dual_e2e_test`

### Interpretation

- Analysis parity is partially green.
- Planner parity is not yet exact.
- Rust and dual e2e failures confirm planner/output deltas still leak into runtime behavior.

---

## Pipeline-ordered divergence list (start → end)

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
