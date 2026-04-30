# Debundler JS↔Rust Parity Plan (Execution Plan)

Last updated: 2026-04-30 (chunked next-step refresh)
Owner: debundle maintainers

## Goal

Reach strict JS-equivalent behavior for planner first, then emitter/lowering, then runtime/cutover gates.

## Current truth snapshot

- Planner scaffolding is present in Rust (debug fields, staged metadata, occupancy packing).
- Strict planner parity assertions are stronger than before, but semantic equivalence is incomplete.
- Remaining risk is semantic divergence, not missing scaffolding.

## Remainder plan to parity

## Current status refresh (what changed)

- Ordered-init planner inputs now include per-side-effect records (`replayable`, `runtime_sensitive`, `touched_owner_ids`) rather than only module-level approximations.
- This reduces attachability drift, but JS-equivalent ordered-init semantics are still not fully ported.
- The largest remaining planner risk is now semantic exactness of ordered-init + closure/staged-shell interactions, not missing scaffolding.

### Block 1 — Planner semantic parity completion (P0)

**Scope**

1. Finish ordered-init parity internals:
   - JS-equivalent replayability predicate,
   - phase-aware touched-owner mapping,
   - runtime-sensitive parity source.
2. Port selected-owner closure semantics to owner-level parity with JS.
3. Port full blocking reason classes and exact payload ordering.
4. Port shell scan eager-access derivation and envelope constraints.
5. Finalize packing/selection edge-rule parity, including preselected closure interactions.

**Deliverables**

- Rust planner internals produce JS-equivalent candidates/selected structures.
- No placeholder blocking logic remains.
- Ordered-init planner-state maps match JS snapshots exactly.

**Exit criteria**

- Strict JS↔Rust planner parity passes on mock + at least one non-mock fixture.

---

### Block 2 — Planner parity corpus expansion (P1)

**Scope**

- Add non-mock full-bundle fixture(s) for strict planner parity.
- Keep mock as fast canary; add heavier parity test target(s).
- Keep strict snapshot normalization deterministic across both fixtures.

**Exit criteria**

- Mock + non-mock planner parity tests are stable and deterministic.

---

### Block 3 — Emitter/lowering parity (P0)

**Scope**

- Port JS staged lowering/rewrite semantics into Rust emitter.
- Replace passthrough-oriented generation with deterministic transformed output parity.

**Exit criteria**

- Artifact-tree parity (modulo explicit non-semantic exclusions) is strict.

---

### Block 4 — Runtime parity validation (P1)

**Scope**

- Browser/runtime equivalence checks across parity corpus for JS vs Rust outputs.

**Exit criteria**

- Runtime behavior parity holds across fixtures.

---

### Block 5 — Cutover gates (P1)

**Scope**

- CI sustained dual-path gates.
- Rollback-safe default flip mechanism.

**Exit criteria**

- Safe promotion and rollback process documented + enforced.

## Practical execution order

1. Block 1 (planner semantic parity)
2. Block 2 (non-mock planner parity breadth)
3. Block 3 (emitter/lowering parity)
4. Block 4 (runtime parity)
5. Block 5 (cutover gates)

## Next 3 concrete PRs to run now

1. **PR-A (Ordered-init exactness):** replace replayability/runtime-sensitive/touched-owner approximations with JS-equivalent planner-state construction and add internal harness asserts for ordered-init maps.
2. **PR-B (Closure + staged-shell):** port JS dependency-component closure and staged-shell batch-plan construction one-to-one; assert candidate-universe + stage-run parity.
3. **PR-C (Blocking + selection):** port remaining class payload rules and edge-order selection/packing semantics; assert exact selected payload parity on mock + one non-mock fixture.

## Execution chunks we can tackle now

### Chunk A (small)

- Close ordered-init map exactness and keep strict harness green.
- Estimated scope: 1 PR.
- Output: ordered-init state maps equal to JS internals on strict parity tests.

### Chunk B (medium)

- Port dependency components + closure identity rules one-to-one with JS.
- Estimated scope: 1 PR.
- Output: candidate-universe parity before packing (IDs + owner sets).

### Chunk C (medium)

- Port staged-shell item/run construction rules one-to-one with JS.
- Estimated scope: 1 PR.
- Output: `shellItemIds` and `stageRuns` exact-equal in parity snapshots.

### Chunk D (medium)

- Port blocking reason payload derivation/ordering exactly.
- Estimated scope: 1 PR.
- Output: per-class payload parity (candidate + selected).

### Chunk E (medium)

- Port selection/packing edge rules (preselected ordering + occupancy tie-breaks).
- Estimated scope: 1 PR.
- Output: selected output and debug payload deep-equal to JS.

### Chunk F (small/medium)

- Add non-mock fixture parity gate.
- Estimated scope: 1 PR.
- Output: strict parity green on mock + non-mock in CI.
