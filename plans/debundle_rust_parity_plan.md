# Debundler JS↔Rust Full Parity Plan (Pipeline-Ordered Execution)

Last updated: 2026-04-30 (Stage 2 lock-in complete; Stage 3 started)
Owner: debundle maintainers

## Objective

Achieve **exact JS parity** for the Rust debundler:

- same algorithm,
- same effective inputs,
- same intermediate planner state,
- same selected output,
- same emitted files,
- same browser/runtime behavior,
- and passing the same existing e2e tests.

This plan is intentionally ordered from **pipeline start to pipeline end**.

---

## Current checkpoint (evidence)

Latest targeted harness run status:

- ✅ pass: `analysis_parity_test`
- ✅ pass: `pipeline_impl_analysis_parity_fixture_test`
- ✅ pass: `js_e2e_test`
- ✅ pass: `ordered_init_parity_test`
- ❌ fail: `planner_internal_parity_test`
- ❌ fail: `planner_parity_test`
- ❌ fail: `rust_e2e_test`
- ❌ fail: `dual_e2e_test`

Implication: ordered-init map parity is now isolated and green; the active critical path is Stage 3 candidate-universe/closure parity.

---

## Stage-by-stage execution plan (strict order)

## Stage 1 — Lock analysis IR equivalence (entry contract)

### Goal

Make Rust planner input IR exactly match JS planner-consumed access semantics.

### Work

1. Freeze JS-equivalent access record schema and ordering contract.
2. Remove any residual non-JS fallback derivations in Rust analysis.
3. Add deep parity assertions at access-record level.

### Exit gate

- Access-level parity snapshots deep-equal on parity fixtures.

---

## Stage 2 — Port ordered-init state exactly ✅ completed (2026-04-30)

### Goal

Make ordered-init state maps exact JS equivalents.

### Work

1. Port JS replayability predicate (including exclusions) one-to-one.
2. Port touched-owner derivation from JS access/phase model one-to-one.
3. Port runtime-sensitive detection source one-to-one.
4. Assert exact equality for:
   - `replayableSideEffectIdsByOwnerId`
   - `replayableSideEffectStateById`

### Exit gate

- Ordered-init state deep-equal in strict planner parity snapshots.

---

## Stage 3 — Port dependency components + closure construction

### Goal

Make candidate universe generation identical before packing.

### Work

1. Port dependency-component formation rules exactly.
2. Port closure expansion frontier and owner grouping exactly.
3. Port closure identity/ID generation rules exactly.
4. Add pre-packing candidate-universe parity assertion.

### Exit gate

- Candidate universe (IDs, owner sets, membership, ordering) deep-equal JS.

### 2026-04-30 implementation progress

- Implemented owner SCC component construction in Rust planner and switched closure seeding to component-level transitive dependency closure.
- Tightened dependency-edge extraction to eager forward-style local-declaration accesses.
- Current status: stage-3 now includes contiguous-envelope expansion with explicit program-item-kind/missing-item barriers plus missing-component barriers, and candidate-closure dedupe/contiguous ID normalization. Planner-internal parity remains red; next slice is exact closure ordering/identity parity and remaining envelope edge rules.

---

## Stage 4 — Port staged-shell batch construction

### Goal

Make staged item/run construction exactly match JS.

### Work

1. Port staged owner attachment expansion and interleave order.
2. Port shell item materialization boundaries/ordering.
3. Port stage run segmentation and ordinals.

### Exit gate

- `shellItemIds`, `stageRuns`, semantic owner/member lists deep-equal JS.

---

## Stage 5 — Port blocking reason derivation exactly

### Goal

Make blocking classes and payloads identical.

### Work

1. Remove remaining heuristic payload construction.
2. Derive eager/shell blocking payloads only from parity access/planner state.
3. Add class-by-class payload parity assertions (order + dedup).

### Exit gate

- All blocking classes/payloads deep-equal JS for candidate + selected sets.

---

## Stage 6 — Port selection/packing edge rules exactly

### Goal

Make final selected result set identical.

### Work

1. Port preselected pass ordering and interactions.
2. Port occupancy collision policy and tie-break behavior.
3. Port final selected ordering semantics.

### Exit gate

- Selected candidate IDs and full selection debug payload deep-equal JS.

---

## Stage 7 — Achieve planner parity green gates

### Goal

Turn strict planner parity tests fully green.

### Work

1. Keep `planner_parity_test` strict (no new normalizations).
2. Keep internal planner parity fixture tests strict for all key maps.
3. Add/retain at least one representative non-mock planner parity fixture.

### Exit gate

- Planner parity test suite fully green on mock + non-mock fixtures.

---

## Stage 8 — Port emitter/lowering to artifact parity

### Goal

Make generated output tree equivalent to JS output.

### Work

1. Port JS lowering/rewrite semantics one-to-one.
2. Tighten artifact comparisons to strict parity.
3. Keep any allowed non-semantic exclusions explicit and minimal.

### Exit gate

- Rust-vs-JS artifact-tree parity green on parity corpus.

---

## Stage 9 — Runtime/e2e parity closure

### Goal

Pass the same e2e expectations with Rust implementation.

### Work

1. Drive failures from `rust_e2e_test` and `dual_e2e_test` to zero.
2. Ensure dual mode asserts both outputs and runtime behavior parity.
3. Keep JS e2e as control target to detect fixture drift.

### Exit gate

- `js_e2e_test`, `rust_e2e_test`, and `dual_e2e_test` all green.

---

## Stage 10 — CI enforcement + cutover readiness

### Goal

Enforce parity continuously before any default switch.

### Work

1. Make strict parity targets required in CI.
2. Keep rollback-safe implementation toggle for staged rollout.
3. Document promotion criteria and rollback criteria.

### Exit gate

- CI consistently green with strict parity gates; default flip is safe and reversible.

---

## Immediate execution queue (next PRs)

Current focus is **PR-2 (Stages 3–4)**. Stage-3 SCC/closure seeding is landed; next PR slice targets contiguous-envelope expansion + closure identity/order parity.

1. **PR-1 (Stage 2 focus):** ordered-init exactness lock-in + strict map assertions.
2. **PR-2 (Stages 3–4 focus):** dependency components/closures + staged-shell parity.
3. **PR-3 (Stages 5–6 focus):** blocking payload parity + selection/packing edge-rule parity.
4. **PR-4 (Stages 7–9 focus):** planner gate hardening + emitter/runtime parity closure.
5. **PR-5 (Stage 10 focus):** CI/cutover gate enforcement and rollout docs.
