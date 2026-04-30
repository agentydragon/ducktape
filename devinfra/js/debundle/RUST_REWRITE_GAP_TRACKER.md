# Debundle Rust Rewrite Tracker

Status: **in progress — no claim of JS semantic parity**
Last updated: 2026-04-30 (next-step refresh)
Owner: debundle maintainers

## Scope and standard

This tracker documents **material algorithmic divergences** between:

- JS source-of-truth planner pipeline (`analysis/boundary.mjs` + `extract/decl_graph.mjs`), and
- Rust rewrite planner pipeline (`rust/pipeline.rs` + `rust/plan.rs`).

The bar is strict conformance: **same semantic inputs, same candidate space, same blocking classes/payloads, same selection outcomes**.

---

## Topological divergence list (pipeline order)

## 1) Analysis IR contract mismatch (pipeline start)

### Current Rust divergence

- Rust `OwnerAnalysis` is synthesized from per-declaration identifier scans and module/member maps.
- JS planner consumes richer owner/access state derived from boundary analysis with explicit access descriptors and planner state caches.
- Rust lacks explicit JS-equivalent per-access metadata (`kind`, `accessKind`, `phase`, ownership resolution parity).

### Why this is material

Every downstream planner stage (closure, shell scan, blocking classes, occupancy interactions) depends on these semantics.

### Required conformance implementation

1. Define a Rust owner-access IR mirroring JS planner-consumed access records.
2. Build it from AST semantics (not token heuristics) with explicit:
   - access kind (read/write-like/eager-read-like semantics required by planner),
   - target owner resolution parity,
   - ordering/ordinal references used by shell scan.
3. Ensure deterministic serialization in snapshots for parity diffing.

### 2026-04-30 progress

- Rust `OwnerAnalysis` now carries explicit `accesses` records wired from AST traversal:
  - `kind`,
  - `access_kind`,
  - `phase`,
  - `owner_id`,
  - `name`.
- Planner owner-record seeding now derives dependency/eager edges from these access records instead of raw token-derived dependency lists.
- Access IR now distinguishes unresolved symbol accesses as `runtime_import` (with `owner_id: null`) rather than forcing them into local declaration edges.

---

## 2) Ordered-init planner state parity mismatch

### Current Rust divergence

- Rust does not implement a true equivalent of JS ordered-init planner state (replayable side effects, touched owner mapping, runtime-sensitive flags parity).
- `attached_item_ids` and shell scaffolding are currently derived via approximations.

### Why this is material

JS staged-shell behavior relies on these state maps to decide attachability and blocking legality.

### Required conformance implementation

1. Port JS planner-state construction semantics into Rust:
   - replayable side-effect state,
   - touched-owner adjacency maps,
   - runtime-sensitive exclusions.
2. Drive attachability decisions from this state only.

### 2026-04-30 progress

- Added Rust ordered-init planner-state scaffolding in planner construction:
  - replayable side-effect ids by owner id,
  - replayable side-effect state by side-effect id (`runtime_sensitive`, `touched_owner_ids`).
- Candidate attached-item derivation now consumes this planner-state map and applies attachability filtering from state instead of unconditional module-level side-effect attachment.
- Runtime-sensitive flag derivation now uses explicit runtime-sensitive effect detection (`eval`, `import.meta`, `await`) instead of generic top-level-effect proxy.
- Touched-owner sets now come from AST-derived top-level side-effect identifier access (not token-touch proxy) and are consumed by attachability filtering.
- Replayable side-effect selection now filters to expression-statement side-effect nodes (`replayable_side_effect_ids`) before ordered-init attachability checks.
- Ordered-init state now consumes per-side-effect records (`replayable`, `runtime_sensitive`, `touched_owner_ids`) instead of module-level side-effect aggregates.
- Remaining divergence: replayability and runtime-sensitive/touch semantics still need exact JS ordered-init logic (phase-aware access graph + side-effect replay rules).

### Immediate next implementation slice (to close item #2)

1. Implement JS-equivalent replayability predicate for side effects (not just expression-statement gate), including exclusions used by staged-shell attachability.
2. Port runtime-sensitive detection to the same planner-state source used by JS, instead of mixed module-level fallbacks.
3. Replace current touched-owner derivation with phase-aware access graph parity and preserve JS payload ordering rules.
4. Add direct parity assertions for ordered-init state maps in internal harness snapshots:
   - `replayableSideEffectIdsByOwnerId`,
   - `replayableSideEffectStateById`.

---

## 3) Dependency component / closure formation mismatch

### Current Rust divergence

- Rust closure construction uses owner records with simplified dependency edges.
- JS uses dependency components + closure plans with semantic envelope summary behavior.

### Why this is material

Candidate universe differs before ranking/packing, so perfect selection parity is impossible.

### Required conformance implementation

1. Implement component construction/closure expansion equivalent to JS planner flow.
2. Ensure closure IDs and owner sets match JS closure plans.
3. Add parity assertions at candidate universe level (before packing).

### Immediate next implementation slice (to close item #3)

1. Port JS dependency-component construction exactly (owner grouping and expansion frontier rules).
2. Emit stable closure identifiers equivalent to JS closure plan identities.
3. Snapshot and assert candidate-universe equality before any blocking/packing stage.

---

## 4) Staged-shell batch-plan construction mismatch

### Current Rust divergence

- Rust stage runs and shell item fields are scaffolding and not fully derived through JS-equivalent staged-shell expansion.
- Missing parity for:
  - staged attached owner expansion,
  - replayable side-effect attachment filtering,
  - selected item interleave semantics.

### Why this is material

`stageRuns`, `shellItemIds`, and derived blocking classes depend on exact staged construction.

### Required conformance implementation

1. Port JS staged-shell batch-plan builder semantics one-to-one.
2. Produce equivalent candidate batch structures (`semanticOwnerIds`, `semanticMemberNames`, `shellItemIds`, `stageRuns`).

### Immediate next implementation slice (to close item #4)

1. Port staged owner-attachment expansion order exactly (including interleaving of selected and attached items).
2. Encode shell-item materialization parity (same item boundaries and ordering).
3. Assert deep parity for `stageRuns` and `shellItemIds` in strict harness tests.

---

## 5) Blocking reason derivation mismatch (class payload semantics)

### Current Rust divergence

- Rust now emits JS class names and ordering contract, but several class payloads are still heuristic.
- Shell/eager classes are not fully sourced from JS-equivalent access graph semantics.

### Why this is material

Blocking reasons are first-class ranking/filtering inputs; payload mismatches change selected output.

### Required conformance implementation

1. Port all JS class derivations exactly from access/planner state, including payload member ordering and dedup rules.
2. Remove all placeholder/proxy derivations (module-order-only proxies, synthetic shell assumptions).
3. Add per-class fixture tests asserting exact payload equality.

### Immediate next implementation slice (to close item #5)

1. Remove remaining heuristic payload construction paths.
2. Rebuild eager/shell blocking payloads from parity access graph and ordered-init maps only.
3. Add class-by-class golden checks for payload member ordering + dedup parity.

---

## 6) Selection/packing edge-rule mismatch

### Current Rust divergence

- Rust has preselected path and occupancy scaffolding, but does not yet guarantee full edge-rule parity for staged attached items and preselection interactions as JS applies them.

### Why this is material

Even with equal candidates, selection can diverge on overlap or preselection collisions.

### Required conformance implementation

1. Port JS selection flow exactly:
   - preselected pass,
   - blocked filtering,
   - owner/item occupancy collision policy,
   - final ordering semantics.
2. Assert parity on selected candidate IDs and full debug payload.

### Immediate next implementation slice (to close item #6)

1. Port JS preselected-candidate pass ordering exactly.
2. Port collision policy for owner/item occupancy as-is, including tie-break semantics.
3. Assert parity for selected candidate IDs, reasons, and final packed grouping order.

---

## 7) Fixture/corpus parity gap

### Current Rust divergence

- Strict planner parity is still concentrated on mock/synthetic fixture paths.
- Non-mock full-bundle planner parity gating is missing.

### Why this is material

Current green status may be overfit to fixture shape.

### Required conformance implementation

1. Add at least one non-mock full-bundle strict planner parity target.
2. Keep mock canary + non-mock gate in CI progression.

### Immediate next implementation slice (to close item #7)

1. Introduce one representative non-mock bundle fixture for strict planner parity.
2. Run strict internal parity and public parity harnesses against both mock + non-mock fixtures.
3. Promote non-mock fixture to CI gate after deterministic stability is demonstrated.

---

## 8) Post-planner (emitter/lowering) parity gap

### Current Rust divergence

- Rust emit/lowering is not yet JS-equivalent and remains outside planner parity completion.

### Required conformance implementation

- Start emitter parity only after planner parity is strict-green on mock + non-mock.

---

## Ordered execution plan (non-hacky rewrite path)

1. **Analysis IR parity** (owner access model parity).
2. **Ordered-init planner state parity** (replayable side-effects/touched-owner maps).
3. **Component/closure parity** (candidate universe parity).
4. **Staged-shell batch-plan parity** (stage runs + shell items parity).
5. **Blocking reason payload parity** (all classes exact).
6. **Selection/packing edge-rule parity** (preselection + occupancy exactness).
7. **Strict non-mock planner parity gate**.
8. **Emitter/lowering parity workstream**.

---

## Next work chunks (sized for execution)

### Chunk 1 (small, 1 PR): Ordered-init map exactness lock-in

- Objective: stabilize and prove JS-equivalent ordered-init map payloads.
- Scope:
  1. align replayability predicate to JS record-type gate + exclusions,
  2. align touched-owner derivation and ordering to JS access-view flow,
  3. align runtime-sensitive source to record-level JS predicate only,
  4. keep strict ordered-init deep-equality assertions in harness green.
- Success check:
  - internal and public planner parity harnesses pass for ordered-init maps with no normalization exceptions.

### Chunk 2 (medium, 1 PR): Dependency components + closure identity parity

- Objective: make candidate-universe identity match JS before packing.
- Scope:
  1. port JS dependency-component construction one-to-one,
  2. port closure expansion frontier/ordering,
  3. port closure/candidate IDs to exact JS identity rules,
  4. add candidate-universe parity assertion (IDs + owner sets) pre-packing.
- Success check:
  - candidate universe (count, IDs, owner membership) exactly matches JS on mock fixture.

### Chunk 3 (medium, 1 PR): Staged-shell item/run construction parity

- Objective: match JS `shellItemIds` + `stageRuns` semantics exactly.
- Scope:
  1. port selected+attached item interleave behavior,
  2. port staged run coalescing boundaries,
  3. port shell item materialization/ordering,
  4. enforce deep stage-run/shell-item equality in strict parity tests.
- Success check:
  - `shellItemIds` and `stageRuns` are exact-equal in internal parity snapshot.

### Chunk 4 (medium, 1 PR): Blocking payload exactness

- Objective: remove all heuristic blocking payload derivations.
- Scope:
  1. port eager/shell/runtime-import payload generation from JS access/planner state,
  2. port payload ordering + dedup behavior,
  3. add per-class parity checks for payload equality.
- Success check:
  - class names and payload strings deep-equal for all candidate and selected plans.

### Chunk 5 (medium, 1 PR): Selection/packing edge-rule parity

- Objective: align final selected outputs once candidate/debug parity is exact.
- Scope:
  1. port preselected pass ordering,
  2. port occupancy collision resolution/tie-break semantics,
  3. port final output ordering semantics,
  4. assert selected IDs + full selected debug parity.
- Success check:
  - selected output deep-equality passes on strict planner parity tests.

### Chunk 6 (small/medium, 1 PR): Non-mock gate activation

- Objective: prevent mock-only overfitting.
- Scope:
  1. add one representative non-mock fixture,
  2. run strict internal + public parity on mock + non-mock,
  3. wire non-mock parity target into CI gate.
- Success check:
  - strict parity green on both fixtures in CI.

---

## Definition of done for planner parity

Planner parity is complete only when all are true:

- Rust and JS candidate universes are structurally equal.
- Rust and JS selected outputs are structurally equal.
- Rust and JS blocking reason classes + payloads are exactly equal.
- Rust and JS staged-shell debug payloads (`shellItemIds`, `stageRuns`, semantic fields) are exactly equal.
- Above holds on mock + at least one non-mock full-bundle fixture.

---

## Latest execution progress and immediate next plan

### Reached in this pass

- Completed AST-based owner access extraction + IR emission with explicit access records.
- Added runtime-import access classification in the IR (`kind = runtime_import`) to align blocking class derivation inputs with JS planner concepts.
- Added ordered-init planner-state scaffolding and partial attachability filtering driven by replayable side-effect maps.

### Still open before declaring gap closed

1. Replace module/token approximations in touched-owner and runtime-sensitive state with full JS-equivalent ordered-init semantics.
2. Drive all blocking payload derivations from access IR + ordered-init state only (remove remaining proxy logic).
3. Port staged-shell batch-plan expansion semantics fully (attached-owner expansion, shell scan parity).
4. Add strict non-mock planner parity gate and require green before moving to emitter parity.
