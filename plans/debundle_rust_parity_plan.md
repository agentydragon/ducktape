# Debundler JS↔Rust Full Parity Plan (Pipeline-Ordered Execution)

Last updated: 2026-04-30 (Stage 3A complete; Stage 3B active; Phase A edge-only plumbing + strict invariants landed; first failing seed locked)
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

---

## Current checkpoint (evidence)

Latest targeted harness status:

- ✅ `analysis_parity_test`
- ✅ `pipeline_impl_analysis_parity_fixture_test`
- ✅ `ordered_init_parity_test`
- ✅ `js_e2e_test`
- ❌ `planner_internal_parity_test`
- ❌ `planner_parity_test`
- ❌ `rust_e2e_test`
- ❌ `dual_e2e_test`

Implication: Stages 1/2 are locked; remaining critical path is Stage 3B planner candidate-universe parity and downstream stages.

Recent progress (2026-04-30):

- ✅ AST-carry refactor cleanup complete (removed legacy parse shims; analysis path now consistently uses cached `Tree` values).
- ✅ Envelope expansion edge-rule tightened (removed transitive auto-add during contiguous envelope scan).
- ✅ Frontier trace debug now emitted in planner snapshot (`seed_component_id`, required component IDs, closure owner IDs) for first-divergence diagnosis.
- ✅ Candidate semantic blocking-reason parity noise removed from candidate-universe comparisons (first mismatch moved past blocking payload drift).
- ✅ Candidate size modeling now uses owner-span basis (moving off member-count sizing), but exact JS span parity still has residual deltas to close.
- ✅ Root-cause analysis isolated semantic mismatch: Rust closure graph previously mixed member-level owners with semantic-owner IDs; planner now collapses to semantic-owner granularity before SCC/closure.
- ✅ Rust owner dependency derivation no longer uses direct name-scanning loops in `build_owner_analyses`; dep/eager-dep now come from semantic access records.
- ✅ Legacy dep-list compatibility chain removed from planner owner-record construction; dependency closure inputs now come from explicit dependency edges only.
- ✅ Strict invariants added in analysis path: local-declaration accesses must carry `owner_id`, and owner-linked local accesses must materialize dep edges.
- 🔴 Remaining Stage 3B deltas are concentrated in exact JS closure expansion semantics (required-closure owner/component sets per seed) and envelope parity payload completeness in the harness.

Current first failing seed (locked target):

- Gate: `requiredClosureOwnerIds`
- Seed: `owner_component_0002`
- Rust: `["owner_00002"]`
- JS: `["owner_00000","owner_00001","owner_00002","owner_00003"]`

This mismatch is the active fix target and must be driven to zero before any Stage 4+ work.

---

## Completed stages (squashed)

### Stage 1 — Analysis IR/access contract ✅ complete

- Access-level planner inputs parity-locked against JS.

### Stage 2 — Ordered-init state parity ✅ complete

- `replayableSideEffectIdsByOwnerId` parity achieved.
- `replayableSideEffectStateById` parity achieved.

### Stage 3A — Dependency component seeding ✅ complete

- Owner SCC/component formation and component-seeded closure bootstrap aligned.

---

## Active divergence tracker (merged from prior tracker doc)

| ID   | Pipeline slice                                       | Status        | Current signal                           | What remains                                                                                                       |
| ---- | ---------------------------------------------------- | ------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| D-01 | Stage 3B closure frontier visitation/closure sets    | 🔴 Critical   | `planner_internal_parity_test` red       | Match JS required-closure owner/component expansion exactly per seed.                                              |
| D-02 | Stage 3B owner semantic granularity/canonicalization | 🟠 High       | frontier/candidate IDs still drift       | Finish mapping and ordering so semantic owner IDs correspond to JS owner statements 1:1.                           |
| D-03 | Stage 3B envelope expansion edge rules/payload       | 🔴 High       | planner parity diffs after closure build | Close remaining barrier/boundary gaps and ensure JS trace fields are complete for apples-to-apples envelope diffs. |
| D-04 | Stage 4 staged-shell construction/runs               | 🔴 Blocked    | downstream planner parity diffs          | Unblock after D-01..D-03 are green.                                                                                |
| D-05 | Stage 5 blocking payload derivation                  | 🔴 Blocked    | mixed planner payload diffs              | Class-by-class ordering/dedup exactness.                                                                           |
| D-06 | Stage 6 selection/packing edge rules                 | 🔴 Blocked    | selected-set mismatch                    | Preselected/occupancy/tie-break/final-order parity.                                                                |
| D-07 | Stage 8/9 emitted output + runtime parity            | 🔴 Downstream | `rust_e2e_test`, `dual_e2e_test` red     | Artifact and runtime behavior parity once planner stages are green.                                                |

---

## Stages we need to handle next (in order)

### Next Stage A — Stage 3B closure/candidate universe parity (D-01..D-03)

**Goal:** make Rust pre-packing candidate universe deep-equal JS.

**Concrete next steps:**

1. Enforce semantic-owner granularity end-to-end in Rust closure graph inputs (done in planner record collapse; verify no remaining member-level leaks).
2. Match JS required-closure expansion per seed by comparing `requiredComponentIds` + `requiredClosureOwnerIds` before envelope logic.
3. Align owner canonicalization ordering/materialization exactly with JS owner-statement order.
4. Finish envelope expansion edge-rule parity and ensure harness compares complete envelope payload fields.
5. Keep strict pre-packing first-divergence gate and burn down mismatches one seed at a time.

**Exit gate:** `planner_internal_parity_test` candidate universe diff reaches zero.

### Next Stage B — Stage 4 staged-shell construction parity (D-04)

**Goal:** shell item/run structure deep-equal JS once Stage 3B is green.

**Exit gate:** `shellItemIds` and `stageRuns` parity.

### Next Stage C — Stage 5/6 blocking + selection parity (D-05, D-06)

**Goal:** blocking payloads and final selected set semantics deep-equal JS.

**Exit gate:** `planner_parity_test` green.

### Next Stage D — Stage 7/8/9 planner-to-runtime closure (D-07)

**Goal:** artifact + runtime parity closure and e2e parity.

**Exit gate:** `rust_e2e_test` and `dual_e2e_test` green.

### Next Stage E — Stage 10 CI/cutover enforcement

**Goal:** strict parity gates required in CI with documented promotion/rollback.

---

## Immediate execution queue (next PR slices)

1. **PR-2A1:** Add Stage 3B frontier/candidate trace diff tooling and wire first-divergence reporting.
2. **PR-2A2:** Use emitted frontier traces to implement first-divergence assertion output (seed + frontier + closure-owner delta).
3. **PR-2B:** Canonicalize candidate identity parity exactly (owner ordering, member ordering, signature materialization).
4. **PR-2C:** Implement Stage 4 staged-shell parity after Stage 3B gate is green.
5. **PR-3:** Stage 5/6 blocking payload + selection/packing parity.
6. **PR-4:** Stage 7/8/9 planner/runtime closure.
7. **PR-5:** Stage 10 CI enforcement + cutover docs.

---

## Heuristic-elimination program (Rust → exact JS implementation)

This section tracks every known Rust heuristic that can drift from JS and the replacement plan.

### H-01 (Critical): owner dependency edges inferred from name matching

- **Current Rust behavior**: planner dependency edges are still synthesized from per-owner `uses/writes` identifier sets + module member-name scans.
- **Drift mode**: symbol-name collisions/minification/canonicalization can produce missing or wrong owner-owner edges.
- **JS source of truth**: owner dependency edges used by closure/component graph construction in JS decl graph/planner.
- **Replacement**:
  1. Introduce explicit Rust owner dependency edge records in analysis IR.
  2. Build planner closure/component edges directly from those records.
  3. Remove name-scan-derived edge synthesis from planner inputs.
- **Phase**: A.
- **Status**: 🔴 In progress (not yet complete).
  - 2026-04-30 execution update: Rust now materializes explicit owner dependency edges from access records before deriving dep/eager-dep owner sets; planner is not yet wired to a fully JS-equivalent dependency-edge IR source.

### H-02 (High): module top-level-effects flag via string `contains(...)`

- **Current Rust behavior**: `has_top_level_effects` derived from `new/window/document` substring checks.
- **Drift mode**: false positives/negatives drive blocking-reason and runtime-sensitivity divergence.
- **Replacement**: compute from semantic analysis side-effect records only.
- **Phase**: B.
- **Status**: ✅ Completed in this PR.

### H-03 (High): runtime-sensitive effects via string `contains(...)`

- **Current Rust behavior**: `runtime_sensitive_effects` and side-effect sensitivity had string scans.
- **Drift mode**: misses AST-context-sensitive semantics; diverges from JS.
- **Replacement**: compute from semantic side-effect records / AST-derived classification only.
- **Phase**: B.
- **Status**: ✅ Completed at module-level in this PR; keep parity check against JS runtime-sensitive payloads.

### H-04 (Medium): permissive `unwrap_or_default` on critical dep paths

- **Current Rust behavior**: silently drops missing analysis data into empty sets in critical paths.
- **Drift mode**: missing edges become “no dependency” instead of deterministic failure.
- **Replacement**:
  1. Add parity-mode invariants for required dependency maps.
  2. Fail fast with seed/owner diagnostics when required maps are missing.
- **Phase**: A/B follow-up.
- **Status**: 🟠 Planned.

### Phase A — dependency-edge source replacement (execute now)

1. Add Rust IR for owner dependency edge records (semantic owner ids + phase + access kind).
2. Populate IR from AST/semantic analysis (not name scanning against member lists).
3. Rewire planner component graph builder to consume this IR directly.
4. Delete legacy edge synthesis path from `build_owner_access_records` as planner input source.
5. Gate with staged parity assertions:
   - `requiredComponentIds`
   - `requiredClosureOwnerIds`.

**Phase A exit gate:** first failing seed no longer under-expands closure owners/components.

#### Phase A remaining micro-plan (single-seed burn-down)

1. Emit Rust dep-edge expansion trace for seed `owner_component_0002`:
   - seed owners
   - direct dep owners
   - direct dep components
   - transitive closure iterations
2. Emit equivalent JS closure-edge trace for the same seed.
3. Diff edge-source inputs **before** closure traversal.
4. Patch semantic edge extraction until seed-level closure set matches.
5. Repeat seed-by-seed until Stage 3B closure gates are green.

#### Phase A gated milestones

- **A1 gate:** seed `owner_component_0002` passes `requiredClosureOwnerIds`.
- **A2 gate:** all seeds pass `requiredClosureOwnerIds`.
- **A3 gate:** all seeds pass `requiredComponentIds`.
- **A4 gate:** candidate universe deep-equal in `planner_internal_parity_test`.

Work on Stage 4+ is blocked until A2/A3 are green.

### Phase B — effect-sensitivity parity replacement

1. Eliminate content-string heuristics for module/effect sensitivity.
2. Use semantic side-effect records exclusively.
3. Re-validate blocking reason parity after Stage 3B closure parity is green.

**Phase B exit gate:** no `contains(...)`-based effect-sensitivity logic in Rust planner inputs.

---

## Tracker closure criteria

- `planner_internal_parity_test` green.
- `planner_parity_test` green.
- `rust_e2e_test` green.
- `dual_e2e_test` green.
- CI required parity gates enabled.
