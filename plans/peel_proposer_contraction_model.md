# Peel Proposer: Graph-Edge Contraction Model

Refactor of the debundle peel proposer around a uniform abstraction:
**progressive vertex contraction in the owner graph's quotient**. Replaces the
current collection of special-case proposal shapes (fresh module / atomic-unit
split / anon-only promotion) with one operation — contract two equivalence
classes — and one algorithm — greedy hierarchical merging — that subsumes all
of them.

## Motivation

Today's `factorize.rs` has several proposal-emitting paths, each with its own
preconditions and rendering logic:

- "Fresh module" proposals from `factorize_atomic_unit` closures of residual
  atomic units.
- "Atomic-unit straddles existing module" → `extends_module_id` set during
  `active_destinations_for_cell` (factorize.rs:413-424).
- "Anonymous-only orphan with single active consumer" →
  `promote_anonymous_only_cell_to_extension` (factorize.rs:575-615, added in
  commit `3c75ae9ae`).
- Named-binding orphans with a single active consumer are **explicitly gated
  out** at line 605 with the comment _"needs the author's judgement on
  naming."_

This shape doesn't generalize:

- A 5-line helper used only by one existing module gets proposed as a
  brand-new module, violating the project rule "no tiny modules <30 lines."
- Two existing modules whose owners mutually constrain each other (the
  TDZ-cycle shape) have no proposal path: the proposer can't suggest "merge
  them"; the human must spot it.
- New shapes require new code branches and new fields on `FactorizeProposal`.

## Mental model

The owner graph is a directed multigraph (vertices = owners — named binding
declarations + anonymous statements + the residual catch-all; edges typed:
`EagerUse`, `LazyUse`, `Sequenced`, `EagerRebind`, `LazyRebind`).

At any point in the peel we operate on its **quotient under an equivalence
relation `~`**. Vertices of the quotient are equivalence classes (clusters);
edges are the union of cross-class edges between members. The peel state is
just `~`.

Two operations on `~`:

- **Contract** `c₁` and `c₂` → coarsen `~` by merging them. Edges between
  them become self-loops (dropped). External edges union onto the merged
  class.
- **Split** is forbidden.

The spec is the initial `~`: every owner in module `M` starts contracted with
every other owner of `M`. The residual catch-all is one big class. Atomic
units (sets of owners forced together by at-init read closure) are also
pre-contracted.

A _proposal_ is a single contraction. A _plan_ is a sequence of contractions
(= a path from seed to converged quotient).

### Why this subsumes today's shapes

Under the contraction model, every existing proposal shape is just a
contraction operation with different operands:

| Today's shape                                | Contraction equivalent                                              |
| -------------------------------------------- | ------------------------------------------------------------------- |
| Fresh module from residual atomic units      | Contract several residual atomic-component classes together         |
| Extend module M with anonymous statement X   | Contract class `[M]` with singleton `{X}`                           |
| Extend module M with named binding helper    | Same: contract `[M]` with `{helper}` (the line-605 gate disappears) |
| Atomic-unit straddles module M               | Contract the residual half of the unit with `[M]`                   |
| Merge modules A and B (possibly absorbing C) | Contract `[A]`, `[B]`, and (optionally) `{C}` together              |
| Cycle-resolving co-location                  | Contract the cyclically-coupled clusters                            |

The kernel exposes one primitive; the public `FactorizeProposal` becomes a
_renderer_ that inspects the operands of a contraction and produces the
appropriate `members:` / `anonymous_statements:` / `extends_module_id:` /
`merge_into:` fields downstream consumers (plan-work JSON, lane workers) read.

## Algorithm — seed-and-greedy

### Seeding (per-contraction, with rejection diagnostics)

Build the seed quotient by applying forced contractions **one at a time**,
gating each on realizability:

```text
Q := QuotientGraph(every owner a singleton class)
rejected := []

// 1. Atomic components. At-init closure means these MUST be co-located;
//    they're provably never the cause of unrealizability (contracting
//    them only intra-clusterizes edges, never cross-clusterizes).
//    Apply through the same gated protocol so future gate regressions
//    can't silently break the invariant.
for unit in canonical_order(atomic_units):
    pivot := owner with smallest OwnerId in unit
    for member in unit.others_in_owner_id_order():
        if Q.merge_preserves_invariants(class_of(pivot), class_of(member)):
            Q.contract(class_of(pivot), class_of(member))
        else:
            rejected.push(SeedContractionRejected::AtomicUnit { … })
            // Should be unreachable in well-formed input; treat as a
            // debundle bug if it ever fires.

// 2. Pre-existing spec modules. These CAN unrealize the quotient when
//    the author has accidentally declared a cyclic grouping. Skip and
//    report — never silently corrupt the build.
for module in canonical_order(spec_modules):
    pivot := owner with smallest OwnerId in module
    for member in module.others_in_owner_id_order():
        if Q.merge_preserves_invariants(class_of(pivot), class_of(member)):
            Q.contract(class_of(pivot), class_of(member))
        else:
            rejected.push(SeedContractionRejected::SpecModule {
                module_id, rejected_pair: (pivot, member),
                cycle: Q.would_be_cycles_after_contract(...),
            })

return (Q, rejected)
```

**Properties:**

1. The seed quotient is **always realizable** by construction. Skipped
   contractions cannot leave unrealizability for the greedy to clean up.
2. Spec authors get **pinpoint diagnostics**: instead of "your spec
   produces a 1109-module SCC, good luck," the report names the specific
   `(module_id, owner_pair)` whose contraction would have created which
   specific cycle.
3. The author chooses how to resolve a rejected contraction: split the
   module, move bindings, or accept that the rejected pair won't be
   co-located as originally declared. Critically, **the build never
   silently emits invalid JS**.
4. `canonical_order` makes the diagnostic stable: atomic units by lowest
   OwnerId member, then spec modules by module path lexicographically;
   within a module, members by OwnerId.

### Greedy merge to convergence

```text
loop:
    candidates := all (c₁, c₂) where mergeable(Q, c₁, c₂)
    if candidates is empty: break
    (c₁, c₂) := pick_best(Q, candidates)
    Q := contract(Q, c₁, c₂)
return Q
```

`mergeable(Q, c₁, c₂)`:

- `c₁ ≠ c₂`
- `c₁` and `c₂` are connected by at least one cross-edge (don't merge
  unrelated islands)
- `lines(c₁) + lines(c₂) ≤ size_cap_lines` (existing cap, default 10000)
- Neither class is `residual` (residual is sticky; we peel **out of** it,
  never absorb into it)
- `Q.merge_preserves_invariants(c₁, c₂)` — post-merge cycle set is a
  subset of pre-merge cycle set (always true for a merge, since merging
  can only ever shrink the cycle set — but we check defensively against
  future gate clauses that might add new requirements)

`pick_best(Q, candidates)` — deterministic with canonical tiebreaks:

1. Prefer merges that **strictly reduce** the realizability cycle set.
   Vestigial in normal flow (seed is realizable), useful as a defensive
   tiebreaker.
2. Then prefer merges with the highest **coupling**, where

   ```
   coupling(c₁, c₂) = Σ edge_weight(e) for e in cross_edges(c₁, c₂)
                      / min(|out_edges(c₁)|, |out_edges(c₂)|)

   edge_weight(EagerUse)      = 4
   edge_weight(EagerRebind)   = 4
   edge_weight(Sequenced)     = 2
   edge_weight(LazyUse)       = 1
   edge_weight(LazyRebind)    = 1
   ```

   Biases toward structurally-strong (constraining) relationships.

3. Then prefer the merge whose **result is smallest** (preserves
   remaining budget for later merges).
4. Tiebreak: lexicographic by canonical `(ClassId, ClassId)` pair.

`pick_best` is total and deterministic.

## Kernel API

```rust
/// All owners start as their own class. Contraction merges two classes.
/// Splits are not exposed as an operation.
pub struct QuotientGraph {
    classes: Vec<ClassData>,
    owner_to_class: Vec<ClassId>,
    edges: CrossClassEdgeIndex, // (from_class, to_class) → edge multiset
    cap_lines: usize,
    cycle_set: CycleSet, // maintained incrementally
}

impl QuotientGraph {
    pub fn from_owner_graph(g: &OwnerGraph, cap_lines: usize) -> Self;

    pub fn class_of(&self, o: OwnerId) -> ClassId;
    pub fn cycle_set(&self) -> &CycleSet;

    /// Cheap query: would contracting (c1, c2) keep the cycle set ⊆ current,
    /// stay under cap, and respect the residual rule? No state mutation.
    pub fn merge_preserves_invariants(&self, c1: ClassId, c2: ClassId) -> bool;

    /// Diagnostic: what cycles would the contraction create (or remove)?
    /// Returns None if merge is fine.
    pub fn would_be_cycles_after_contract(&self, c1: ClassId, c2: ClassId)
        -> Option<CycleEvidence>;

    /// Apply a contraction; updates cycle set + post-order state.
    /// Returns Err if invariants would be violated (caller should have
    /// checked first; this is the belt-and-braces).
    pub fn contract(&mut self, c1: ClassId, c2: ClassId)
        -> Result<(), ContractRejected>;
}

pub fn build_seed_quotient(
    g: &OwnerGraph,
    atomic_units: &[AtomicUnit],
    spec_modules: &[SpecModule],
    cap_lines: usize,
) -> (QuotientGraph, Vec<SeedContractionRejected>);

pub fn greedy_merge_to_convergence(q: &mut QuotientGraph)
    -> Vec<(ClassId, ClassId)>;
```

### Diagnostic shape

```rust
pub enum SeedContractionRejected {
    AtomicUnit {
        owners: Vec<OwnerId>,
        rejected_pair: (OwnerId, OwnerId),
        cycle: CycleEvidence,
    },
    SpecModule {
        module_id: String,
        rejected_pair: (OwnerId, OwnerId),
        cycle: CycleEvidence,
    },
}
```

Emitted in `reports/tree/.../seed_rejections.json` alongside the existing
`cycles.json`; surfaced in plan-work output so spec authors can decide what
to do.

### Incremental realizability — staging

The API doesn't change between the two implementation cuts:

- **First cut (correctness, commit 1/1b)**: `merge_preserves_invariants`
  rebuilds the cycle set by running the existing realizability gate on
  a synthetic post-merge quotient. O(|edges|) per query. `contract`
  also rebuilds. Slow on large graphs but obviously correct. Sufficient
  for the seed-and-rejection diagnostics that ship in commit 1; the
  greedy is not yet enabled.
- **Second cut (incremental, commit 2)**: keep the constraining-edge
  subgraph SCCs + the ESM-eval-simulator's post-order DFS as persistent
  state on `QuotientGraph`. `merge_preserves_invariants` consults the
  cached state in O(|Δ|) where Δ is the affected reachability cone
  around the candidate merge endpoints. `contract` runs an incremental
  SCC update (Pearce-Kelly style) + restricted post-order recompute
  over Δ. Amortized cost per merge in a sparse graph is closer to
  O(log V).

**The second cut must land with the greedy**, not after it. Greedy is
O(|candidates| × per-query) per iteration, candidates is O(|E|), and
there can be O(|V|) iterations to convergence. With the first cut's
O(|E|) query this is O(|V| · |E|²) per planner run — unusable on
gaffer-scale inputs (|V| ≈ 2K classes, |E| ≈ 50K edges → ~10¹¹ ops).
Shipping the greedy on the first cut would be a feature nobody can run
against real input. Hence the merged commit-2 scope below.

## When merges create cycles, and how the kernel handles it

Earlier drafts of this plan claimed "merges only destroy cycles." That
proof was wrong. Here is the correct picture.

Merging two classes `c₁` and `c₂` into `c`:

- All edges between `c₁` and `c₂` become self-loops on `c` (dropped from
  cross-class edge set).
- All other edges incident on `c₁` or `c₂` get re-pointed to `c`.
- No new cross-class edges are introduced.

**Cycles that pre-existed and touched both endpoints**: become
intra-cluster on `c`, gone. **Cycles that touched only one endpoint**:
unchanged modulo relabeling.

**New cycles**: a path `c₁ → x → c₂` plus an existing edge `c₂ → c₁`
(or vice versa) creates a new cycle through the merged `c`, because the
two ends of the path are now the same class. This is the case the
original proof missed — merging two classes that share a path through
some third class `x` can introduce a cycle that didn't exist before in
the SCC structure even though no edge was created.

**Practical implication**: `merge_preserves_invariants` cannot simply
check "is the post-merge cycle set ⊆ pre-merge cycle set" via the
cached cycles alone, because new SCCs can appear from the through-third-
class case. The kernel's implementation handles this in two parts:

1. **Cached-cycle scan** — covers "merge endpoints are both in a
   pre-existing SCC, does the SCC survive": O(1) lookup against
   `class_to_cycle_indices`.
2. **Localized class-quotient BFS** — covers "merge creates a new SCC":
   from the merge endpoints, BFS through the quotient and check
   reachability back. Only the reachability cone Δ around the candidate
   endpoints needs to be visited; this stays cheap on a sparse class
   graph.

Together they form the incremental `merge_preserves_invariants`
predicate. The full realizability gate (`check_realizability`) remains
the correctness reference; the property test
`incremental_state_matches_rebuild_on_synthetic_specs` pins
`kernel_query == full_rebuild_check` for every state the kernel
reports.

## Migration path (TDD, four commits on `tdz-gate-fix`)

### Commit 1 — Kernel introduction, no behavior change

- Add `QuotientGraph`, `build_seed_quotient`, `merge_preserves_invariants`,
  `contract` (first-cut incremental: rebuild on each call).
- Add `SeedContractionRejected` diagnostic struct + JSON emission.
- Re-express today's `factorize::build_proposal` output as a renderer over
  a quotient built from the spec but with the greedy disabled. Output
  should be byte-identical to today (validated by golden tests).
- Tests:
  - `seed_pre_contracts_atomic_units` — every atomic-unit owner pair
    shares a class at seed.
  - `seed_pre_contracts_spec_modules` — every spec-module owner pair
    shares a class at seed.
  - `seed_skips_unrealizable_spec_module_contraction_and_reports` —
    fixture: spec declares M, M' whose owners' edges form an asymmetric
    cycle; assert one appears in rejections with cycle evidence.
  - `seed_atomic_unit_contractions_never_rejected_on_well_formed_input` —
    regression guard.
  - `seed_rejection_diagnostic_is_canonical` — determinism check.
  - `contract_never_un_contracts` — API surface check.
  - Golden: existing factorize output unchanged after the refactor.

#### Commit 1b — Renderer over a cells-derived quotient (Path B)

Split out from commit 1 to keep the staging reviewable. Lands the
renderer-over-quotient half of commit 1 verbatim:

- Cell-discovery (`proposal_cells_from_atomic_graph`) is preserved
  bit-for-bit — atomic-DAG reachability closure + overlap
  coalescing. The cells it produces are then materialized as a
  `QuotientGraph` partition via a new
  `QuotientGraph::from_report_with_partition` constructor (one
  class per cell, owners not in any cell stay singletons). The
  constructor bypasses the realizability gate because the cells
  were computed under today's pre-kernel algorithm; the gate
  applies to the seeding protocol, not to this Path B bridge.
- `emit_proposals` reads class membership through the quotient
  (`class_members`, `class_lines`) instead of from `&[(Cell,
Verdict)]`. Per-cell metadata (`extends_module_id`,
  `extension_owner_idxs`, verdict) rides alongside as a parallel
  `CellClassRecord` vec.
- `promote_anonymous_only_cell_to_extension` survives **unchanged**
  as a post-pass over the emitted proposals. Its semantics are
  shape-driven (binding ids empty, single active reference,
  etc.) — invariant under the renderer-source swap. Commit 2
  will fold its work into the greedy "extend single consumer"
  case; commit 1b doesn't touch it.
- Output is byte-identical to the pre-commit-1 binary,
  load-bearing-checked by `factorize_golden_output_unchanged` in
  `peel/quotient_integration_test.rs`. Snapshots live at
  `devinfra/js/debundle/peel/golden/*.json` for three fixtures:
  `residual_singletons`, `closed_residual_unit`,
  `extend_active_via_anon`.
- New internal test
  `partition_constructor_contracts_each_group` covers the
  refactor's bridge invariant: each input group becomes one
  class; ungrouped owners stay singletons.

**Path A deferred — not abandoned.** Path A (extending the
seeding protocol with a third pass that contracts
atomic-DAG-reachability closures under gating) was considered
first. The reason for deferral is precisely **byte-identity**:
today's cell discovery forms closures freely — even ones that
end up cyclic — and reports problems via the realizability
gate downstream. The seeding protocol is _gated_: it refuses
contractions that would create cycles and reports
`SeedContractionRejected` instead. So Path A _changes
behavior_ on unrealizable inputs, turning "form cyclic cell,
report cycle later" into "refuse cell merge, report rejection
with cycle evidence." This is a strictly better outcome
(pinpoint diagnostics vs blob report), but it violates commit
1b's byte-identity contract.

Path A is the **planned destination** — see commit 4 (the
unification commit) below. Cell discovery is logically the same
process as quotient seeding; the only reason they're separate
today is that cells predate the kernel. Eventually the cell
pipeline gets deleted and seeding subsumes it under uniform
gated semantics. The intentional behavior change on unrealizable
inputs is part of the trade.

### Commit 2 — Enable greedy on uncontroversial shapes (with incremental realizability)

Greedy and incremental realizability ship together — the greedy is
only practical with the incremental cut of `merge_preserves_invariants`
(see "Incremental realizability — staging" above for the complexity
argument). Splitting them was a staging artifact, not an engineering
boundary.

Algorithm work:

- Implement `greedy_merge_to_convergence` with the coupling / size /
  cycle-resolution / lex tiebreak order.
- Initially restrict `mergeable` to "extension of existing module by
  orphaned residual class" (the cases today's code already handles).
- **Lift the line-605 named-binding gate** — extensions of single
  helpers into their unique consumer fall out for free.

Performance work (incremental realizability):

- Promote the constraining-edge SCC partition and the ESM-eval
  simulator's post-order DFS to persistent state on `QuotientGraph`.
  Build it once in `from_report_*`; update on every `contract`.
- `merge_preserves_invariants` becomes a cached lookup that walks only
  the affected reachability cone (Δ) around the candidate endpoints.
- `contract` runs incremental SCC maintenance (Pearce-Kelly: order
  classes by topological position, update affected positions only on
  cycle-creating merges; here merges only _destroy_ SCCs, which is the
  easier direction).

Tests (algorithm):

- `greedy_extends_existing_module_with_only_consumer` — anon orphan.
- `greedy_absorbs_tiny_named_helper_into_unique_consumer` — named.
  Initially RED (today's gate rejects this); GREEN after.
- `greedy_terminates_at_convergence` — class count strictly
  decreasing.
- `greedy_never_splits_existing_spec_module` — fixture with spec
  module containing multiple bindings; assert greedy doesn't
  propose splitting it no matter what merges happen elsewhere.
- `greedy_never_merges_into_residual` — assert no proposal contracts
  a class into residual.

Tests (performance + incremental invariants):

- `incremental_state_matches_rebuild_on_synthetic_specs` — property
  test: for a corpus of fixture chunks, after each greedy contraction,
  assert the cached SCC + post-order state byte-equals what a full
  rebuild would produce. Load-bearing correctness guard.
- `greedy_on_gaffer_chunk_completes_under_one_minute` — benchmark with
  the real owner_graph.json from a recent gaffer cache; assert
  wall-time bound. RED today (greedy doesn't exist); GREEN after
  commit 2 lands with both halves.

### Commit 3 — Enable full mergeability + merge output shape

- Drop the restriction on which classes can be operands; allow merges
  between two pre-existing module classes (with or without absorbing
  residual classes).
- Add `merge_into: Option<Vec<String>>` field to `FactorizeProposal`
  (or equivalent) so downstream consumers can see "this proposal
  merges modules A and B."
- Tests:
  - `greedy_merges_three_clusters_under_cap` — user's example: clusters
    of 20+20+10 lines mutually coupled, cap=150. Assert all three
    merged.
  - `greedy_stops_at_cap` — same fixture, cap=40. Assert exactly two
    merged.
  - `greedy_resolves_realizability_cycle_by_merging` — `mod_a ↔ mod_b`
    asymmetric cycle. Assert greedy merges them and post-merge
    quotient is realizable.
  - `merge_two_existing_modules_with_mutual_eager_reads` — assert
    proposal output carries `merge_into: Some(["mod_a", "mod_b"])`.
  - `merge_absorbs_residual_owner_with_only_intra_deps`.
- **Out of scope for this commit**: lane-worker rule for "edit two
  module yamls together → merge them." That's a gaffer-side ticket
  consuming the new output shape. The ducktape kernel just emits the
  proposal.

### Commit 4 — Unify cell discovery into seeding (Path A)

**Intentional non-byte-identical change** — the destination Path A
that commit 1b deferred. Delete
`proposal_cells_from_atomic_graph` and its associated cell IR.
Extend `build_seed_quotient` with a third gated contraction pass:
for each atomic-DAG edge whose target is in a residual atomic
unit, contract the source class with the target class through
the same gated protocol. Iterate to fixed point if needed for
overlap-coalesce equivalence. Closures that today would have
been formed despite cyclicity now appear as
`SeedContractionRejected::AtomicReachability` diagnostics
instead.

- Today's `Vec<CellClassRecord>` parallel state goes away; the
  quotient is the only representation of "which owners are in
  which proposed class."
- `promote_anonymous_only_cell_to_extension` is fully subsumed
  by the greedy's "extend single consumer" merges; the post-pass
  is deleted.
- The golden snapshots from commit 1b are **invalidated** for
  fixtures with unrealizable inputs (those gain a rejection
  diagnostic where they used to have a proposal). Snapshots for
  well-formed fixtures stay byte-identical (no behavior change
  there).
- Tests:
  - `unification_byte_identical_on_well_formed_inputs` — golden
    suite still passes for fixtures that produce zero
    rejections.
  - `unification_rejects_cyclic_atomic_reachability_with_diagnostic` —
    fixture whose atomic-DAG reachability closure would form a
    cycle; assert (a) no proposal emitted for that closure, (b)
    `SeedContractionRejected::AtomicReachability` entry pinpoints
    the rejected pair.
  - `unification_eliminates_cell_pipeline` — static check (no
    `Cell` struct, no `proposal_cells_from_atomic_graph`
    symbol).

## Out of scope

- **Naming**. Minified names stay minified through the proposer; the
  rename queue resolves them later. The proposer outputs whatever
  names the spec or atomic-unit closure assigns.
- **Per-module taxonomy decisions**. The proposer doesn't decide
  module paths for merged-existing-modules; that's a human choice
  surfaced to the lane worker.
- **Loosening the gate**. The realizability gate's correctness invariants
  are unchanged; this plan is about how the proposer _uses_ the gate,
  not what the gate computes.
- **Replacing the FAS heuristic in cycle cut reports**. The simulator
  already narrows; the user-facing cut report is a separate surface.

## Open follow-ups for downstream consumers

After commits 1-3 land in ducktape, gaffer's plan-work and lane-worker
tooling need updates:

- **plan-work consumer** needs to know how to write spec yaml for the
  new "extend existing module with named binding" output shape (today
  it only writes for anon).
- **Lane workers** need a rule for "merge two existing modules":
  combine their `members:` lists into one yaml file, delete the other,
  optionally add `anonymous_statements:` for absorbed residual owners.
- **CLI flags**: `debundle peel plan-work` should grow flags for
  `--cap-lines` (override) and `--report-rejections-only` (skip
  greedy, just emit seed rejection diagnostics — useful when a spec
  author is debugging an unrealizable spec).
