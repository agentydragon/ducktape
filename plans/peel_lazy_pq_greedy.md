# Peel Greedy: Lazy Priority-Queue Candidate Enumeration

Spec for a future commit on the debundle peel proposer. Targets the
greedy's per-iteration full-scan cost — the dominant bottleneck on
production-scale chunks like Tana's main blob.

## Status: spec / unimplemented

## Problem

`greedy_merge_to_convergence` in `devinfra/js/debundle/peel/quotient.rs`
currently does a full `O(|E|)` scan of cross-class candidate pairs
every iteration. Each candidate evaluated calls
`merge_preserves_invariants`, which dominates inner cost. On the gaffer
Tana spec (`|V| ≈ 3·10³` classes, `|E| ≈ 2.6·10⁴` cross-class edges,
~10² merges to convergence) the current shape produces:

| Component                         | Per iteration | Per run      |
| --------------------------------- | ------------- | ------------ | -------- | --- | --- | --- | --- | ------------ |
| Candidate scan                    | `O(           | E            | ) ≈ 26k` | `O( | V   | ·   | E   | ) ≈ 2.6·10⁶` |
| Verdict calls (one per candidate) | `O(           | E            | )`       | `O( | V   | ·   | E   | )`           |
| Total verdicts                    | —             | `~10⁵ – 10⁶` |

Measured wall-clock: **~10 min** on a 9709-owner / 3017-yaml Tana
chunk. Perf commits 5–8 each shaved 10–20% off inner-loop cost but
didn't change the `O(|V| · |E|)` outer shape.

## Goal

Replace the per-iteration full scan with a priority queue that re-ranks
only the candidates whose state changed since the last merge. Reduce
verdict-call count by ~2 orders of magnitude by paying verdict cost
only on the candidates as we pop (a few per merge until one succeeds),
not every candidate every iteration.

Target: **gaffer plan-work in well under 60s.** A follow-up commit on
verdict-cost reduction (dense `Vec<…>` indexing) can push toward the
user's `<10s` goal.

## Algorithm

### State

Add to `QuotientGraph`:

- `candidate_queue: BinaryHeap<CandidateEntry>`.

```rust
struct CandidateEntry {
    /// Tuple ordered so BinaryHeap's max-of-Ord gives the right
    /// pick-best order: cycle-reduction first, then highest coupling,
    /// then smallest merged size, then lex (c1, c2) for total order.
    sort_key: (CycleReductionScore, Coupling, Reverse<Lines>, ClassId, ClassId),
    c1: ClassId,
    c2: ClassId,
    /// Snapshot of coupling at push time. Used for the
    /// lazy-recomputation staleness check.
    stored_coupling: Coupling,
    /// Snapshot of cycle-reduction score at push time.
    stored_cycle_score: CycleReductionScore,
}
```

No per-class generation counter is needed — coupling staleness is
detected by comparing `stored_coupling` to a freshly computed value on
each pop.

### Initialization

Once at the start of `greedy_merge_to_convergence`:

```text
// One entry per unordered cross-class pair (NOT per individual edge —
// coupling sums over the edges between a pair, so per-pair is the right
// granularity).
for each unordered pair (c1, c2) such that ∃ cross-class edge between c1, c2:
    coupling = compute_coupling(c1, c2)  // sums all edges in the pair
    cycle_score = compute_cycle_reduction_score(c1, c2)
    push CandidateEntry { … }
```

Cost: `O(|E| log |E|)` one-time (walk edges, group by pair, push).

### Greedy loop

```text
loop outer:
    discard_pile = Vec::new()
    while let Some(entry) = candidate_queue.pop():
        // 1. Class-existence guard. Loser ClassIds never come back to
        //    life; this discard is permanent.
        if !classes_exist(entry.c1, entry.c2):
            continue

        // 2. mergeable_commit2 check. NOT monotone in failure: orphan
        //    with two pre-existing-module neighbors fails the
        //    "unambiguous target" rule until one neighbor merges away.
        //    Stash on the discard pile so it gets re-checked after the
        //    next successful contract.
        if !mergeable_commit2(entry.c1, entry.c2):
            discard_pile.push(entry)
            continue

        // 3. Lazy-recompute and re-push if coupling drifted.
        //    coupling(c1, c2) depends on |out_edges(c1)| and
        //    |out_edges(c2)|; either can change when an unrelated
        //    merge collapses a shared neighbor. Re-push at the new
        //    value, continue popping; true max-coupling surfaces.
        current_coupling = compute_coupling(entry.c1, entry.c2)
        current_cycle_score = compute_cycle_reduction_score(entry.c1, entry.c2)
        if (current_cycle_score, current_coupling)
           != (entry.stored_cycle_score, entry.stored_coupling):
            candidate_queue.push(CandidateEntry {
                sort_key: (current_cycle_score, current_coupling, Reverse(lines), c1, c2),
                stored_coupling: current_coupling,
                stored_cycle_score: current_cycle_score,
                ..entry
            })
            continue

        // 4. Verdict check. NOT monotone: a cycle through (c1, c2)
        //    can be broken by a merge elsewhere along the cycle.
        //    Stash on the discard pile so it gets re-checked after the
        //    next successful contract.
        if !merge_preserves_invariants(entry.c1, entry.c2):
            discard_pile.push(entry)
            continue

        // 5. Commit.
        contract(entry.c1, entry.c2)
        repush_affected_neighborhood(winner)
        // State just changed; drain the discard pile so every entry
        // that previously failed gets re-evaluated in the next
        // outer-loop iteration.
        for e in discard_pile.drain(..):
            candidate_queue.push(e)
        break  // out of inner; restart outer with fresh discard_pile
    else:
        // Inner-while exited because the queue is empty without a
        // commit. No state has changed since the discarded entries
        // failed → re-checking would fail the same way. Drop the pile;
        // terminate the outer loop.
        break outer
```

`repush_affected_neighborhood(winner)`:

```text
for X in winner.cross_class_neighbors():
    push CandidateEntry for (winner, X)
```

Note: only `(winner, X)` entries are pushed. The cascading "X's other
neighbors' couplings may have drifted because X's divisor changed"
case is handled lazily at pop time by step 3 above — not via eager
re-push.

## Correctness

### Invariants

1. **Class-existence**: a `ClassId` is "alive" iff it has never been
   `contract`'s loser. `contract(loser, winner)` retires loser's
   `ClassId`; subsequent pops referencing it are discarded at step 1.

2. **No missed candidates**: every cross-class pair that exists at
   any point is in the queue or the discard pile at least once.
   - Initialization pushes every existing cross-class edge.
   - `repush_affected_neighborhood` pushes every new (winner, X) edge
     after a contract.
   - The discard pile preserves transiently-failing candidates across
     state changes by draining back to the queue on each successful
     contract.

3. **Coupling-drift safe**: when coupling drifts (due to a divisor
   change from an unrelated merge), the entry's stored coupling
   diverges from the freshly computed value. Step 3 catches this on
   pop and re-pushes at the current value. The true max-coupling
   surfaces because no entry can sit at the top with a stale-high
   stored value.

4. **Non-monotone failures retried**: `mergeable_commit2` and
   `merge_preserves_invariants` can flip from false to true after
   unrelated merges (ambiguous extension target resolved; cycle
   broken). The discard pile + drain-on-contract pattern ensures
   every failed candidate is re-evaluated after each state change.

5. **No infinite spin**: the discard pile only drains when a contract
   succeeds (state changed). If an iteration empties the queue
   without a contract, the pile is dropped and the outer loop
   terminates — re-checking would just fail the same way.

6. **Termination**: each successful `contract` strictly reduces class
   count. Bounded by initial class count. Inner-loop pop bound: each
   entry visited at most once per iteration (no re-pushes to the
   current iteration's queue from steps 2 / 4; step 3 re-pushes can
   happen but the entry's sort key strictly drops on each recompute
   if coupling decreased, OR the re-push has a higher key and is
   then popped as the true best — either way bounded).

### Determinism

Every component of `sort_key` is a deterministic function of the
current `QuotientGraph` state:

- `CycleReductionScore`: indicator from
  `would_be_cycles_after_contract` cardinality comparison.
- `Coupling`: deterministic ratio of edge weights (typed-edge walk).
- `Reverse<Lines>`: derived from class member-line counts.
- `(c1, c2)` as `(ClassId, ClassId)` lex order — the final tiebreak
  that makes the sort key a total order.

### Output equivalence to the current greedy

The PQ algorithm with the same sort key MUST produce the same merge
sequence (and therefore the same proposals) as the current full-scan
greedy on any input — modulo any nondeterminism in the current
algorithm.

This is the load-bearing correctness gate. Add property test:

```rust
#[test]
fn lazy_pq_greedy_matches_full_scan_greedy_on_corpus() {
    for fixture in fixture_corpus() {
        let from_scratch = run_full_scan_greedy(fixture.clone());
        let lazy_pq = run_lazy_pq_greedy(fixture);
        assert_eq!(from_scratch, lazy_pq,
                   "PQ greedy diverged from full-scan on {fixture:?}");
    }
}
```

The full-scan reference implementation stays in the codebase as the
test reference until the lazy-PQ algorithm has been load-bearing for
the production binary for a release cycle. After that, retire the
reference. Don't delete it as part of this commit.

If the test diverges, options in priority order:

1. **The PQ has a subtle bug** — most likely staleness check is too
   loose. Investigate; fix.
2. **The current greedy has unintended nondeterminism** (e.g.,
   iteration over a `HashSet`). Treat as a separate bug; fix that
   first.
3. **The sort key is incomplete** — some tiebreak isn't deterministic.
   Strengthen the sort key.

## Migration

One commit on a new branch (suggested name: `debundle-peel-lazy-pq`).

1. Introduce `CandidateEntry` + the new PQ state on `QuotientGraph`.
2. Rewrite `greedy_merge_to_convergence` using the pop-loop above.
3. Keep the old `pick_best_candidate` / `mergeable_commit2` / `coupling`
   helpers — they're called as inner primitives.
4. Add the property test
   `lazy_pq_greedy_matches_full_scan_greedy_on_corpus` with at least 5
   diverse fixtures (chains, mutual-eager cycles, asymmetric cycles,
   star topologies, fully-connected small classes).
5. Run benchmark on gaffer: capture before/after wall-clock.

## Out of scope

Verdict-call cost itself. After this commit lands, the dominant cost
shifts to the per-call simulator overlay work in `RealizabilityIndex`.
Closing from ~30-60s to <10s likely needs the dense-`Vec` ModuleId
indexing refactor that the commit-8 agent identified (replace
`BTreeMap<ModuleId, _>` / `BTreeSet<ModuleId>` with `Vec<_>` /
`Vec<bool>` indexed by `ModuleId.0` throughout `RollbackDiGraph` and
`EsmEvaluationSimulator`). That's a substantial mechanical refactor
touching several modules; a separate commit.

## Open questions

1. **Initial PQ population cost**: 26k coupling computations + pushes
   at startup. Each coupling compute is `O(|cross_edges(c1, c2)|)`,
   typically constant for sparse adjacency. Realistic: under 100ms.
   Verify in benchmark.

2. **Coupling recomputation cost**: same as a fresh coupling compute —
   constant per pair on sparse graphs. Bounded total recomputes per
   merge by the size of the affected reachability cone.

3. **Discard pile size**: bounded by `|queue|` at iteration start.
   Drains back to queue on each contract. Steady-state size is the
   count of currently-transiently-failing candidates — typically
   small.

4. **Heap growth**: every merge can push `O(|winner's new
neighborhood|)` entries plus re-pushes from coupling-drift detect.
   Across `O(|V|)` merges total: bounded by `O(|V| × |avg degree| +
coupling-drift events)`. Heap log factor stays ~20.

5. **Should we cap heap size or evict explicitly?** No — stale
   entries sift to the bottom naturally; the lazy-recomputation
   pattern is bounded regardless.

6. **Duplicate entries after drain + repush.** After a contract,
   `repush_affected_neighborhood` pushes fresh `(winner, X)` entries
   for X in winner's neighborhood. The drained discard pile may also
   contain `(winner, X)` entries (from before the contract). Both end
   up in the queue — duplicates that will both pop. Not a correctness
   issue (the first one to commit retires `winner` or `X`, after which
   step 1 discards the dup), just a small amount of wasted work.
   Optional optimization: deduplicate on drain by `(c1, c2)`. Not
   required.

## References

- Existing spec for the broader contraction model:
  `plans/peel_proposer_contraction_model.md`
- Commits 5–8 (perf history):
  - `b7326c5fa` (commit 5: kernel through RealizabilityIndex)
  - `37888abcb` (commit 6: lazy simulator cache)
  - `6a1efd219` (commit 7: incremental constraining adjacency)
  - `a1e93b2cc` (commit 8: callee_edges CSR)
- Track B literature on incremental SCC under contraction informs the
  verdict-side follow-up (separate commit).
