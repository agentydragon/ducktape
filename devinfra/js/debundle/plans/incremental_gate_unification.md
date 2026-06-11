# Incremental gate unification: an exact and fast merge gate

Design for replacing the peel kernel's hot boolean merge gate — today a
constraining-only Pearce–Kelly walk that is blind to Pass-2 — with a
tier-laddered evaluation of the realizability primitive itself: exactly
equivalent to `check_realizability`'s accept/reject on every query,
within ~2× of the current PK-gate hot-path cost, with no second
algorithm that can drift.

Maintainer decision this implements: _"i would prefer something that's
both fast and also actually correct. if PK is not correct, then it's
insufficient, except maybe as first-pass check."_ This resolves two of
the four Track A3 decisions in `plans/sanitization_program_2026_06.md`
(PK-gate vs realizability-index; the unreachable multi-target
fallback). Sequencing: **after the A2 crate split lands** — A2 moves
`realizability.rs` internals and the peel kernel into separate crates,
and every PR below touches those files.

## 1. Current state and the correctness hole

The greedy proposer's hot path
(`peel/quotient.rs::greedy_merge_to_convergence_lazy_pq`) asks a
boolean question once per candidate pop:

```text
merge_preserves_invariants
└── check_merge_boolean
    └── would_violate_cycle_gate_after_contract
        ├── cached_cycles short-circuit (advisory cache)
        └── merge_creates_new_constraining_cycle
            ├── TopoOrder::would_create_cycle   (PK, is_dag)
            └── cone_dfs_creates_new_cycle      (fallback, !is_dag)
```

This is `O(|Δ|)` per query (PK affected-region bound) and the reason
the proposer runs in 3.54 s on the tana fixture (9709 owners, 93
proposals, 59663 `rank_candidate` calls — `perf/proposer.md`). But it
computes only **Pass 1 at class granularity**. It cannot see:

1. **Pass-2 rejections.** Asymmetric I-SCCs (eager forward, lazy back)
   where the `EsmEvaluationSimulator` decides TDZ. A merge that closes
   such a cycle is accepted by the hot gate and committed. The only
   backstop is `build_seed_quotient`'s one post-seed
   `realizability_verdict()` — which **reports**
   `PostSeedUnrealizableScc` but does not undo the offending merges.
   The seed can hand the factorizer a partition the materializer's
   gate rejects, with the blame smeared across the whole run instead
   of attached to the merge that caused it.
2. **Module-granularity Pass-1.** The PK walk runs on the **class**
   graph; the realizability verdict runs on the **module** projection,
   where the residual catch-all class and every gate-residual-only
   class share `ModuleId::logical(0)`. The two graphs genuinely
   disagree (§2).

The diagnostic path (`would_be_cycles_after_contract` →
`verdict_after_moving_owners_touching`) is already exact and already
routed through the shared primitive — but it is too slow to run per
pop (it was the pre-fix bottleneck), so the hot path bypasses it.
docs/design.md §"Why not Pearce–Kelly verbatim" documents this as a
deliberate relaxation of the "no bespoke parallel walks" invariant;
`ARCHITECTURE_BACKLOG.md` carries the open decision. This design
closes it.

## 2. The predicate, made precise

Before making the gate exact we have to say exact **about what**.
Today the system computes two different predicates in different
places:

- **Module-level (P1)**: `check_realizability(owner_graph,
project_partition(...))` under the kernel's projection — residual
  class and gate-residual-only classes → `ModuleId::logical(0)`,
  everything else a distinct module. This is what the
  `RealizabilityIndex` maintains, what the post-seed backstop checks,
  and what `validate_factorization` will compute when proposals land
  (spec-module classes are always promoted to non-residual ids via
  `set_class_pre_existing_module`, so landed modules are first-class
  on both sides).
- **Class-level**: the PK/cone walk's "no new constraining cycle
  among classes", which treats every class — including unassigned
  residual-destined singletons — as its own node.

These disagree exactly on the gate-residual pile, and the class-level
side appears to be **wrong** there. Concrete reading-of-the-code
anomaly: a 3-owner atomic unit whose members form a constraining cycle
`a → b → c → a` cannot seed. `contract(a, b)` is rejected because
`cone_dfs_creates_new_cycle` finds the path `b → c → a` (the class
graph is `!is_dag` from construction, so the cone fallback runs and
detects the — pre-existing, transient — cycle), while the index's
module-level verdict is realizable (all three owners are module 0), so
`realizability_cycles_after_contract` surfaces nothing and the
"defensive, shouldn't happen" evidence branch in
`would_be_cycles_after_contract` fires as the routine path. Net: an
atomic unit that exists precisely because its members **must**
co-locate is blocked from co-locating, with bogus two-class evidence.
(`seed_pre_contracts_atomic_units` doesn't catch this — its 3-member
unit has no edges. PR 1 adds the pinning test; needs confirmation on
the real corpus.)

**Decision: the gate predicate is the module-level one.** For a
speculative merge `(c1, c2)` with post-merge module `M` (from
`projected_winner_module_after_merge`) and deltas from
`compute_merge_deltas`:

> `gate(c1, c2)` accepts iff
> `verdict_after_moving_owners_touching(owners, M).is_realizable()` —
> i.e. the post-merge partition, under the index's maintained
> projection, has no clause-2 or clause-3 violation **touching `M`**.

Rationale:

- It is literally the predicate the diagnostic path and the
  materializer's primitive already compute — goal (c) by definition.
  The boolean form is the same evaluation with evidence
  materialization elided, not a parallel algorithm.
- The "touching" filter matches today's semantics on both paths:
  pre-existing violations elsewhere don't block an unrelated merge.
  When the pre-state is realizable (the invariant the gated seed
  maintains), touching-filtered and full-verdict accept/reject
  coincide, which is the differential harness's equality claim.
- It fixes the atomic-unit anomaly: merges internal to module 0 are
  delta-free no-ops and trivially accepted.

Consequence to own honestly: class-level acyclicity among unassigned
proposal cells stops being gate-enforced. Mutual constraining cycles
**between** residual cells (today rejected at seed pass 3 with
`AtomicReachability` diagnostics) become acceptable merges from the
gate's point of view, producing larger or mutually-cyclic cells whose
single-cell landing is unrealizable. That hazard is already policed
downstream by `peel/factorize.rs`'s `BlockedResidualDependency`
status, and interacts with the open `landable_today` backlog decision
— flagged as open question 2 rather than silently re-encoding the
class-level gate.

## 3. Chosen design: a three-tier ladder inside the index

The boolean gate becomes a short-circuit evaluation of the predicate
above, executed by the `RealizabilityIndex` itself. Each tier either
**decides** (provably equal to the full verdict) or **escalates**. No
kernel-side graph walk remains.

### Tier 0 — delta-free short-circuit, `O(|members|)`

`compute_merge_deltas` returns no deltas (both classes already project
to `M`, e.g. merges inside the gate-residual pile). Post-state ==
pre-state; accept iff the pre-state touching verdict for `M` is clean
— cached per committed state, so `O(1)` after the first query.

### Tier 1 — constraining condensation order (exact Pass 1 + clause 2)

A `CondensationOrder` structure (§4) maintained over the index's
**module-level constraining graph** (the `RollbackDiGraph<ModuleId>`
the `IncrementalQuotient` already owns): a union-find of SCC
membership plus a PK topological order over the condensation DAG.
Query, with the merge's edge overlay applied during traversal (the
same `±edge` deltas `overlay_for_move` computes):

- reject iff `M`'s post-merge constraining SCC is multi-module:
  `multi(find(c1's module)) || multi(find(c2's module))` (`O(α)`) or
  identification closes a new condensation cycle — PK
  window-restricted reachability through ≥ 1 intermediate node,
  `O(|Δ|)` on the effective (overlay-patched) adjacency.

A tier-1 reject is **definite**: it is precisely the
`MutualConstrainingCycle` clause of `verdict_with_overlay_touching`.
A tier-1 pass establishes Pass 1 is clean and escalates. Clause-2
cross-rebinds are checked here too via the existing
`cross_rebinds_touching_with_overlay` (`O(deg)`); merges only ever
convert cross-rebinds to intra-module, so on realizable pre-states
this is a formality.

Because the condensation is a DAG **by construction even when the
underlying graph is cyclic**, the kernel's `is_dag` degradation, the
`cone_dfs_creates_new_cycle` fallback, and the per-contract
`rebuild_topo_ord` recovery loop all disappear.

### Tier 2 — I-graph condensation order (Pass-2 vacuity)

A second `CondensationOrder` instance over the index's I-graph
(constraining ∪ lazy — `IncrementalQuotient::i_graph`). Same query
shape: is `M`'s post-merge I-SCC multi-module, and if so does it
contain at least one effective constraining pair
(`constraining_buckets` lookup, as `verdict_with_overlay_touching`
already does)?

- **No multi-module I-SCC, or no constraining pair inside it** ⇒
  Pass 2 is vacuous; the tier-1 answer is final. **Accept.** This is
  the load-bearing fast accept: per Lemma 2, pure-lazy I-cycles never
  TDZ, and modules outside any I-cycle can't be rejected by the
  simulator.
- Otherwise escalate to tier 3.

One exactness caveat: the merge overlay also **removes** I-edges
(edges that become intra-module, or third-module edges relabeled away
from a vacated module). The maintained union-find is stale under
removals — a removal whose endpoints sit in one pre-state SCC can
split it, making the maintained membership too coarse. Tier 2
therefore falls back to the existing exact per-query
`OverlayGraphView::scc_containing` bidirectional DFS (`O(|cone|)`)
when the overlay removes an edge internal to a multi-module SCC —
the same fallback rule `perf/proposer.md`'s appendix specifies for
the V1.5 design. This is rare: it requires the vacated module and a
third module to share an I-SCC pre-merge, and multi-module I-SCCs are
themselves rare (tana: `scc_containing` called 10× per full run).

### Tier 3 — scoped ESM simulator (exact Pass 2)

Route the query through the existing
`verdict_after_moving_owners_touching` → `verdict_with_overlay_touching`
path: overlay-patched `EsmEvaluationSimulator` build over the shared
`EsmImportOrder` (#2071) and `tdz_pairs` over the post-merge I-SCC.
Reject iff any TDZ pair. This is the **same code** the diagnostic
path, `cycle_set()`, and (via `check_realizability`'s pure twin) the
materializer's gate execute — drift is impossible by construction,
not by differential test alone. Tiers 1–2 mean tier 3 runs only for
merges that put `M` into a multi-module I-SCC carrying a constraining
edge.

### Why this is one algorithm

The ladder is not a second decision procedure: tiers 0–2 are
short-circuits whose skip conditions are theorems about the predicate
("no multi-module constraining SCC touching `M` ⇒ no
`MutualConstrainingCycle` diagnosis touching `M`"; "no constraining
pair inside `M`'s I-SCC ⇒ `tdz_pairs` is empty"), and tier 3 **is**
the predicate. The evidence-producing diagnostic path becomes the
same ladder with evidence materialization enabled — one entry point,
two output shapes. design.md's "no bespoke parallel walks" invariant
is restored and its "Why not Pearce–Kelly verbatim" relaxation
paragraph is deleted.

## 4. `CondensationOrder`: the tier-1/2 structure

Survey of candidates for incremental SCC maintenance under
contraction:

- **Pearce–Kelly 2007** (in-tree as `peel/topo_order.rs`): online
  topological order, affected-region bounded, simple. Doesn't
  maintain SCCs — the in-tree adaptation punts to `!is_dag` + cone
  DFS when cycles exist.
- **BFGT 2015**: better asymptotics (`O(m^{1/2})` amortized per arc
  insertion), insertion-only model, substantially more complex, no
  contraction primitive. Poor fit for graphs of ~10³ nodes where the
  PK affected region is already tiny.
- **Fähndrich–Foster–Su–Aiken / Hardekopf–Lin**: bounded local-search
  cycle elimination with eager SCC collapse via union-find, from
  pointer-analysis constraint graphs. Structurally the closest match:
  "collapse cycles into representatives as they form" is exactly what
  contraction-driven quotient maintenance needs, and it scales to
  graphs orders of magnitude larger than ours.
- **Lazy invalidation keyed by incident edges**: recompute SCC
  membership on demand after invalidating regions touched by a merge.
  Rejected — hub nodes (module 0 is incident to nearly everything)
  cause invalidation storms; window-bounded search is strictly better
  parameterized.

**Choice: generalize the existing `TopoOrder` into
`CondensationOrder`** — PK order maintained over the condensation,
with a union-find for SCC membership and a `multi: bool` (or member
count) per representative:

- `would_join_multi_scc(u, v, overlay)`: the speculative query of §3.
  Identification-of-two-nodes reduces to "path through ≥ 1
  intermediate condensation node" — the same lemma
  `peel/topo_order.rs` already proves for class contraction, applied
  to condensation nodes. Window-DFS reads the **effective** adjacency
  (base edges mapped through `find`, patched by the overlay) so
  removals are handled exactly during traversal.
- `apply_contract(u, v)`: on a committed merge, identify the nodes;
  if the identification closes condensation cycles, the local
  window-Kahn (already implemented in `TopoOrder::apply_contract`)
  discovers the cyclic window — instead of degrading to `!is_dag`,
  union every node of each new cycle and re-rank the window. Cost
  `O(|window| + |E_window|)`, same bound as today.
- **Monotonicity**: vertex identification only ever coarsens the SCC
  partition — contractions merge SCCs, never split them. Within a
  committed run the union-find never needs un-merging; this is the
  structural reason a DSU (rather than full dynamic-SCC machinery) is
  sufficient. Speculative queries are read-only overlays.
- Condensation adjacency is **derived at traversal time** by mapping
  the maintained module graphs' edges through `find()` — no third
  edge store to keep in lockstep.

Two instances live inside `IncrementalQuotient`, one per maintained
graph (constraining, I), updated in the same `add_current_edge` /
`remove_current_edge` funnel that already invalidates the simulator
cache. Edge **insertions** between existing SCCs use the standard PK
insertion path (also potentially union-ing); edge **removals** mark
the affected representative's membership as stale-coarse (the tier-2
fallback trigger) rather than attempting splits — committed removals
that actually split an SCC trigger a lazy rebuild of that structure
(`O(|V| + |E|)`, amortized over a run in which removals internal to
multi-SCCs are rare).

### Journal / undo / commit interaction (#2066)

After this design, kernel-side mutation of the index is
**commit-only**: `merge_classes_unchecked` and
`set_class_pre_existing_module` push permanently and `commit()`;
speculative queries never push (single-target overlay path). With the
multi-target fallback deleted (§6), `RealizabilityIndex::undo` has
zero production callers — `scoped`/`undo` remain as the documented
planner API and test surface. `CondensationOrder` therefore handles
`undo` by **invalidate + lazy rebuild** rather than journaled
rollback: undo is off the hot path everywhere, and a journaled DSU
(rollbackable union-by-rank) can be added later behind the same API
if a profile ever shows undo-heavy callers. This follows
`perf/proposer.md`'s optimization policy: no machinery without a
profile demanding it.

## 5. Cost analysis

Per-query, against the tana profile shape (`perf/proposer.md`:
`|V| ≈ 10³`–`10⁴` owners, `|E| = O(|V|)`, sparse class/module graphs,
multi-module I-SCCs in single digits per run):

| Tier | Decides                                 | Mechanism                                   | Cost                  | Expected hit share (tana shape)                            |
| ---- | --------------------------------------- | ------------------------------------------- | --------------------- | ---------------------------------------------------------- |
| 0    | accept (no-op merge)                    | empty `compute_merge_deltas`                | `O(members)`          | high in seed passes 1/3 (residual pile); low in greedy     |
| 1    | reject (Pass 1) / escalate              | DSU find + PK window-DFS, overlay-effective | `O(α)` … `O(\|Δ_c\|)` | runs on ~every greedy pop; nearly all rejects decided here |
| 2    | accept (Pass 2 vacuous) / escalate      | DSU find + PK window-DFS on I-condensation  | `O(α)` … `O(\|Δ_I\|)` | runs on ~every tier-1 pass; decides ≈ always               |
| 2'   | exact SCC fallback (removal-inside-SCC) | `OverlayGraphView::scc_containing`          | `O(\|cone\|)`         | rare (requires pre-existing multi I-SCC at vacated module) |
| 3    | accept/reject (Pass 2 exact)            | shared `EsmEvaluationSimulator` overlay run | `O(V + E + V log V)`  | single digits per run (measured: 5 builds, 0.148 s total)  |

- **Hot-path bound (goal b)**: a typical greedy query costs tier 1 +
  tier 2 ≈ two PK window walks where today it costs one — within the
  ~2× budget. The 33-byte-key PQ machinery, `rank_candidate`, and
  adjacency maintenance around it are unchanged.
- **Maintenance per committed contraction**: two
  `CondensationOrder::apply_contract` windows instead of one
  `TopoOrder::apply_contract` — ~2× today's per-commit maintenance,
  minus the deleted `update_cycle_cache_after_merge` /
  `rebuild_class_to_cycle_indices` walk (`O(Σ cycle sizes)` per merge,
  `perf/proposer.md` #5, which dies with `cached_cycles`).
- **Worst case**: an adversarial corpus where every candidate merge
  lands the target in a constraining-edge-bearing I-SCC drives tier 3
  on every pop: `O(V)` simulator builds of `O(V + E)` ≈ the old naive
  `O(V²·E)` shape, ~30 ms per build at tana scale. Accepted: such a
  corpus is pathological (it means almost every proposal closes a
  runtime cycle through residual), and `overlay_is_simulator_noop`
  plus per-committed-state caching of negative tier-3 results are
  available mitigations if it ever materializes. Not built now (no
  profile demands it).
- **Memory**: per instance, three `O(V)` `u32` vectors (rank, inverse
  rank, visited-epoch) + `O(V)` DSU + per-rep multi flag; ×2
  instances. No new edge storage. Net change vs the deleted kernel
  `TopoOrder` + `cached_cycles` + `class_to_cycle_indices`:
  approximately zero.

## 6. What gets deleted or absorbed

- **Kernel `TopoOrder` (`peel/topo_order.rs`)**: absorbed. The PK
  core (window DFS, window Kahn, epoch visited-marks) survives as the
  heart of `CondensationOrder`, relocated to the realizability crate
  (post-A2) and re-keyed from `ClassId` to condensation nodes. The
  kernel keeps **no** order of its own; `is_dag`, `rebuild_topo_ord`,
  and `cone_dfs_creates_new_cycle` are deleted.
- \*\*`cached_cycles` + `class_to_cycle_indices` +
  `update_cycle_cache_after_merge` + `rebuild_class_to_cycle_indices`
  - `rebuild_cycle_cache`\*\*: deleted. Their three roles are subsumed:
    the two gate short-circuits by tiers 1–2, and `rank_candidate`'s
    cycle-reduction key byte 0 by an `O(α)` DSU query ("are `c1`'s and
    `c2`'s modules in the same multi-module SCC?") against the tier
    structures — same semantics ("this merge dissolves an unrealizable
    SCC"), no cache to drift, kills backlog item #5.
- **The unreachable multi-target fallback**
  (`realizability_cycles_after_contract`'s push/verdict/undo arm).
  Options per the backlog entry:
  1. **Delete + loud single-target assert** (recommended).
     `compute_merge_deltas` emits at most two `MoveOwners` deltas,
     both targeting the single post-merge module, by construction;
     the assert (`debug_assert!` + release-mode panic with the delta
     shape in the message) turns any future violation into an
     immediate, attributable failure instead of silently exercising
     untested code. Deleting it also makes kernel-side index mutation
     commit-only, which is what licenses the non-rollbackable DSU
     (§4) — the fallback's continued existence has a real design
     cost now, not just dead-code smell.
  2. Absorb into the new dispatch: keep a generic "push batch, read
     ladder, undo" arm for hypothetical future delta shapes. Rejected:
     no producer of such shapes exists or is planned, and it forces
     rollback support into the tier structures for a path nothing
     takes.
- **design.md updates**: "Cost and the upgrade path" rewritten for
  the ladder; "Why not Pearce–Kelly verbatim" loses the
  relaxed-invariant paragraph (the kernel no longer maintains
  decision-making derived state); the `ARCHITECTURE_BACKLOG.md`
  PK-gate-vs-index and multi-target-fallback entries are deleted by
  the PRs that resolve them.
- **Unchanged**: `EsmImportOrder` and the simulator (shared with the
  emitter, #2071); the journal/commit machinery (#2066);
  `check_realizability` as the pure correctness reference; the
  greedy driver and `rank_candidate` sort-key layout (modulo byte
  0's data source).

## 7. Validation plan

Acceptance is differential, not review-only:

1. **Randomized differential harness (Track F1, the acceptance
   criterion).** Proptest-generated `OwnerGraphReport`s (mixed
   `DepKind`s including lazy back-edges, residual-destined owners,
   atomic units, spec modules, rebinds) + random merge sequences
   driving the real seed/greedy entry points. After **every** boolean
   gate query, assert
   `gate(c1, c2) == reference(owner_graph, post_partition, M)` where
   `reference` is a ~20-line pure function: `check_realizability` on
   the post-merge projection, filtered to diagnoses touching `M`.
   Additionally assert, per query, tier-skip soundness (tier-1 reject
   ⇒ reference has a `MutualConstrainingCycle` touching `M`; tier-2
   vacuity ⇒ reference's Pass 2 produced nothing touching `M`) so a
   ladder bug localizes to its tier. Include the gate-residual
   promotion transition and unrealizable-seed (partition-driven)
   starting states. An independent reference partition builder, per
   the Track F note — not the kernel's own `project_partition`.
2. **Oracle mode on the corpus.** `DEBUNDLE_GATE_ORACLE=1` runs the
   reference per query during `modules propose` on the tana fixture
   and on the gaffer-private snapshot; zero divergences required.
   Off by default (it is `O(V·(V+E))`).
3. **Pinning tests for the semantic fixes**: the 3-member-cycle
   atomic-unit seed (currently rejected; must co-locate cleanly), a
   merge that closes an asymmetric I-cycle (currently accepted by the
   hot gate, only reported post-seed; must be rejected at the merge
   with `EsmEvaluationTdz`-backed evidence), and preservation of
   `seed_skips_unrealizable_spec_module_contraction_and_reports`.
4. **Corpus output gate.** `modules propose --format json` against
   tana, diffed against head. Byte-identical is **not** expected —
   the predicate change intentionally alters seed rejections and
   therefore cell shapes. Every diff hunk must trace to one of the
   cataloged semantic fixes (Pass-2 blindness, atomic-unit anomaly,
   pass-3 class-cycle rejections); unexplained diffs block.
5. **Perf gate** (per `perf/proposer.md` policy): `-c opt` builds,
   interleaved before/after runs of `modules propose` on tana. Ship
   criteria: wall within run-to-run noise of 3.54 s; new per-tier
   `DEBUNDLE_TIMING` counters show tier-3 builds in single digits and
   tier-1+2 cumulative wall ≤ 2× the pre-change PK-gate cumulative.

## 8. Staged PRs

Each independently green; sequenced after the A2 crate split.

1. **PR 1 (S) — predicate pinning + reference.** The pure
   touching-filtered `reference` function; pinning tests from §7.3
   asserting **current** behavior where it's wrong, marked
   `#[ignore]` with reasons naming PR 4 (per the backlog's
   ignored-test standard); differential harness skeleton running
   against the current gate with the known-divergence catalog encoded
   as expected failures.
2. **PR 2 (M) — `CondensationOrder`.** Generalize
   `peel/topo_order.rs` into the realizability crate: DSU + PK over
   condensation, overlay-aware `would_join_multi_scc`, contraction
   with cycle-union, removal staleness marking, invalidate-on-undo.
   Property tests vs `tarjan_scc` on random graph + mutation
   sequences. No call-site changes.
3. **PR 3 (M) — ladder in the index.** `IncrementalQuotient` gains
   the two maintained instances and a boolean
   `would_remain_realizable_after_moving_owners_touching` implementing
   tiers 0–3 (this completes `perf/proposer.md` backlog #2's
   short-circuit sketch); `DEBUNDLE_GATE_ORACLE` cross-check; per-tier
   counters wired into `gate_perf_counters`. Existing callers
   untouched.
4. **PR 4 (M/L) — cutover + deletions.** `check_merge_boolean` routes
   through the ladder; delete kernel `TopoOrder` usage, `cone_dfs`,
   `cached_cycles` family, and the multi-target fallback (+
   single-target assert); `rank_candidate` byte 0 re-sourced; seed
   diagnostics re-derived from ladder evidence; un-`#[ignore]` PR 1's
   tests; e2e/corpus diffs reviewed against the catalog.
5. **PR 5 (S) — perf validation + doc sync.** §7.5 measurements;
   rewrite design.md's two sections; delete the resolved
   `ARCHITECTURE_BACKLOG.md` and `perf/proposer.md` entries (#2, #5,
   appendix V1.5/V2 where subsumed); update this plan to tombstone.

## 9. Open questions

1. **Atomic-unit anomaly confirmation.** The ≥3-member-cycle seeding
   failure is a reading of the code, not yet a reproduced failure on
   a real corpus. PR 1's pinning test settles it; if real corpora
   never produce such units, the fix still stands but the urgency
   framing changes.
2. **Inter-cell cycle policing after the predicate change.** With
   class-level acyclicity no longer gate-enforced for the residual
   pile, is `BlockedResidualDependency` (plus the open
   `landable_today` decision) sufficient to keep mutually-cyclic
   proposal cells from reaching `bindings assign`? If not, the
   factorizer needs an explicit cell-DAG check at render time — which
   is the right home for a proposal-shape (not realizability)
   invariant.
3. **Diff envelope for the corpus gate.** How large a proposal-shape
   change on gaffer-scale corpora is acceptable in PR 4? Maintainer
   call once the diffs exist.
4. **Tier-3 worst case.** Accept the pathological `O(V·(V+E))` bound,
   or pre-approve the per-committed-state negative-result memo? This
   design says accept and measure (§5); revisit only with a profile.
5. **`rank_candidate` byte 0.** The DSU-backed replacement is
   semantically cleaner than the stale `cached_cycles` probe but not
   bit-identical on unrealizable seeds (the cache was deliberately
   left stale across promotions). Decide in PR 4 whether to chase
   heuristic-order equivalence or accept ranked-order drift on
   already-unrealizable inputs.
