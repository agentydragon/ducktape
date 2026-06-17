# Perf profile: read-off minimizer (post prove-gate fix)

Where the time goes in `spec synthesize-selectors` now that the prove-gate is
cheap (#2280) and the whole chunk minimizes in ~13s (see
<selector_minimizer_dogfood.md>). This backs backlog item 12 — closing the last
~3s to the ≤10s ideal.

## Method

`perf` is unavailable in the container (not installed; `perf_event_paranoid=2`),
so this uses **callgrind** (`valgrind --tool=callgrind`), which needs no kernel
perf events and gives deterministic instruction-count (Ir) self-cost attribution.
Profiled the **`-c opt`** `debundle` binary against the real tana/re chunk
(`static/index-DI2GynTv.js`) with `--rewrite name-binding-to-source-match`, on the
unmodified (name-pinned) spec. Two scopes: `integrations` (106 members —
floor-dominated) and `app` (940 members — floor + per-member work). Ir share is a
good proxy for wall-clock here (compute-bound, flat ~242 MB RSS).

## `integrations` — index-build floor (5.1B Ir)

| Ir % | function                                       |
| ---- | ---------------------------------------------- |
| 7.00 | `_int_malloc` (libc)                           |
| 6.67 | `_int_free` (libc)                             |
| 6.29 | `__memcmp_avx2` (libc) — String-label compares |
| 6.07 | `<SelectorFeature as Ord>::cmp`                |
| 4.56 | `malloc` (libc)                                |
| 3.01 | `SelectorCandidateIndex::new`                  |
| 2.98 | `Vec::from_iter`                               |
| 2.80 | `<ShapeNode as Ord>::cmp`                      |
| 2.56 | `<ShapeFeature as Ord>::cmp`                   |
| 2.48 | `BTreeMap::IntoIter::dying_next`               |
| 2.35 | `BTreeMap::insert`                             |
| 1.99 | swc lexer `next_token` (parse)                 |
| 1.68 | `ShapeIndex::with_extractor`                   |

## `app` — floor + 940 members (23.5B Ir)

| Ir %     | function                                                                                                 |
| -------- | -------------------------------------------------------------------------------------------------------- |
| 11.20    | `BTreeMap::Iter::next`                                                                                   |
| 7.85     | `_int_free` (libc)                                                                                       |
| 5.60     | `malloc` (libc)                                                                                          |
| 4.90     | `BTreeMap::IntoIter::dying_next`                                                                         |
| 4.86     | `_int_malloc` (libc)                                                                                     |
| 3.75     | `BTreeMap::Drop::drop`                                                                                   |
| 3.68     | `free` (libc)                                                                                            |
| 3.40     | `Vec::from_iter`                                                                                         |
| 2.51     | `__memcmp_avx2` (libc)                                                                                   |
| 1.54     | `source_match::…::find_member_binding_matches` (the matcher)                                             |
| 1.33     | `<SelectorFeature as Ord>::cmp`                                                                          |
| 1.21     | `hstr::Atom::as_str` (swc)                                                                               |
| ~5 (sum) | `AstWildcardMatcher::{match_var_declarator_slice,bind_alpha_sym,snapshot,restore,match_binding_ident,…}` |

## Diagnosis

The cost is **the index and its set algebra, not parsing and not the per-member
proof.** Parsing (swc `next_token`) is ~2% of the floor; the whole prove-gate
matcher (`find_member_binding_matches` + the `AstWildcardMatcher::*` family) is
only ~6% on `app` — #2280 did its job. What dominates is:

- **`BTreeMap`/`BTreeSet` traversal and churn** — `Iter::next` (11%),
  `IntoIter::dying_next` + `Drop` + `insert` (~10% more). Posting lists are
  `BTreeSet<usize>` and candidate sets are built/intersected/dropped **per member**
  via ordered-tree iteration.
- **Allocator churn ~23%** (`malloc`/`free`/`_int_*`/`malloc_consolidate`) — the
  many small BTree nodes and per-query set allocations.
- **String-label comparison** — `memcmp` (2.5–6%) + `SelectorFeature`/`ShapeFeature`
  `Ord::cmp` driven by comparing `String` feature labels inside every map op.

## Improvement targets (ranked)

1. **Posting lists / candidate sets as bitsets or sorted `Vec<u32>`, not
   `BTreeSet<usize>`.** Body indices are dense small integers `0..N`; intersection
   becomes a bitwise-AND or linear merge instead of ordered-tree iteration +
   per-query allocation. This directly attacks the `BTreeMap::Iter::next` (11%) +
   `dying_next`/`Drop`/`from_iter` (~12%) + a large share of the allocator churn —
   by far the biggest lever.
2. **Intern feature labels to integer IDs.** Feature comparison becomes an int
   compare; kills `memcmp` (2.5–6%) and shrinks every `Ord::cmp`. Pairs naturally
   with a hashed feature→id table built once during index construction.
3. **Hashed maps (`FxHashMap`/`FxHashSet`) where ordered iteration isn't needed.**
   The feature→postings map doesn't need ordering; hashing removes the remaining
   `Ord::cmp` and tree-rebalance cost.
4. **Reuse per-member scratch.** `candidate_set_for_query` / `minimal_anchor_set`
   allocate and drop fresh sets per member; a reused scratch buffer (cleared, not
   freed) removes a chunk of the `malloc`/`free` pairs.

Any one of (1)–(2) should recover the last ~3s to the ≤10s ideal; (1) is the
highest-leverage and most self-contained.

## Resolution (#2291): roaring-bitmap posting lists + `FxHashMap`

Landed targets **(1)**, **(3)**, and **(4)** — the self-contained data-structure
swap, no feature-label interning needed:

- **Posting lists / candidate sets are [`roaring::RoaringBitmap`]**
  (`CandidateSet` in `selector_candidate_index.rs`) — the standard inverted-index
  posting structure. Built in one pass via `push_ascending` (body indices arrive in
  order, so `RoaringBitmap::push` appends in O(1)); intersection is `&` /
  `intersection_len` over adaptive array/bitmap containers, and `intersect_into`
  reuses a scratch set (`clone_from` + `&=`). The greedy read-off ranks candidate
  features by `intersection_len` (count-only, no materialization) and builds only
  the winner. This removes the `BTreeMap::Iter::next` / `IntoIter::dying_next` /
  `Drop` / `from_iter` cost and most of the small-node allocator churn.
- **The inverted indices are `FxHashMap`** (`feature_to_body_indices`,
  `ShapeIndex::postings`, and the `ShapeInterner` hash-cons table `by_node`) —
  ordered iteration was never needed, so hashing removes the `SelectorFeature` /
  `ShapeFeature` / `ShapeNode` `Ord::cmp` (String-label `memcmp`) and the
  tree-rebalance cost from every build insert and lookup.

The feature taxonomy and posting-list **contents** are unchanged (same
`distinct_shapes` / `posting_entries`, same `OPT=1` distribution), so the read-off
results are identical — only the representation is faster. Soundness stays gated
by `shape_index_soundness_test.rs`.

**Measured.** Synthetic `shape_index_bench` (isolated index-build + read-off
lever), `-c opt`:

| N    | build before | build after | read-off before | read-off after |
| ---- | ------------ | ----------- | --------------- | -------------- |
| 200  | 0.0067 ms/it | 0.0037      | 1.61 µs/it      | 0.76           |
| 1000 | 0.0069 ms/it | 0.0043      | 3.26 µs/it      | 1.12           |
| 4000 | 0.0081 ms/it | 0.0044      | 8.82 µs/it      | 1.95           |

≈ **1.8× faster index build** and **4.5× faster read-off** at N=4000. Real-chunk
same-spec whole-chunk wall-clock and peak RSS in <selector_minimizer_dogfood.md>.

**Why roaring over a bespoke sorted `Vec<u32>`:** roaring is the vetted, standard
structure for exactly this (dense-small-int posting lists), and its read-off
intersection edged out a hand-rolled two-pointer merge on sorted vecs in the
synthetic sweep (1.95 vs 2.41 µs/it). The one tradeoff on the real chunk: most
posting lists are size 1–2 (selective features dominate), where roaring's
per-container overhead costs ~21 MB more RSS than a `Vec<u32>` would — an
acceptable price for the standard structure (footprint stays flat vs the BTree
baseline; see the dogfood note).

## Re-profile after enclosing-context + interior cover (2026-06-17)

Same callgrind method (Ir-only, no cache-sim), but on the **whole-spec
`--apply`** run with the post-#2291 capabilities landed (#2315
enclosing-context anchoring, #2289 interior cover, #2306 multi-target
binding-group read-off). Reproducer:
<perf/2026_06_17_dogfood_apply_whole_spec/command.sh>. Total **99.3B Ir**;
wall **13.1 s** — a regression from the ~7 s #2291 recorded, because the new
capabilities now **convert ~1104 members (~50%)** instead of the ~200 then
convertible, and each conversion drives the prove-gate matcher (often several
times: neighbor windows, group tuples, interior covers).

**The bottleneck has moved off the shape index (#2291 fixed it) and onto the
prove-gate MATCHER.** Inclusive cost:

| Ir % (incl.) | function                                                |
| ------------ | ------------------------------------------------------- |
| 96.0         | `rewrite_name_bindings_to_source_match` (whole convert) |
| **58.4**     | `find_member_binding_matches` (the prove-gate matcher)  |
| **8.4**      | `AstWildcardMatcher::restore` (backtrack undo)          |
| **5.6**      | `AstWildcardMatcher::snapshot` (backtrack save)         |
| 3.3          | `AstWildcardMatcher::with_alpha_scope` (scope push/pop) |

`snapshot` + `restore` + `with_alpha_scope` ≈ **17% of the whole run is pure
backtracking bookkeeping**, and it is the **same `BTreeMap` pattern #2291 fixed
one layer up**: `AlphaMatchScope` (`source_match/matcher.rs`) holds two
`BTreeMap<Atom, Atom>` (forward/backward alpha-binding maps), and the matcher keeps
a `Vec<AlphaMatchScope>` stack that it `clone()`s on `snapshot` and drops/swaps on
`restore` for every backtrack. Top self-cost confirms it — allocator churn ~30%
(`_int_free` 9.3%, `malloc` 6.8%, `_int_malloc` 5.6%, `free` 4.4%, …) and
`BTreeMap`/`BTreeSet` ops ~21% (`IntoIter::dying_next` 5.6%, `Drop` 4.3%,
`from_iter` 4.1%, `Iter::next` 2.4%, `clone_subtree` 1.3%, …), almost all of it
the alpha-scope clone/drop. Parsing is ~2% (`next_token` 0.79%); the shape index /
read-off no longer registers near the top.

The new conversion paths are what call the matcher so much:
`find_member_binding_matches` callers include `synthesized_candidate` (the
prove-gate, 1075×), `minimize_var_group_selector` (1272×),
`try_var_read_off` (577×), `render_via_neighbor_context` (497×, the #2315 path),
and `resolve_member_binding_group_with_declarator_holes` (379×).

### Improvement targets (ranked)

1. **Journaled (undo-log) `AlphaMatchScope`, not clone-on-snapshot.** Record the
   keys inserted into `forward`/`backward` since the last `snapshot`; on `restore`
   pop exactly those, instead of cloning the whole map and swapping it back. Makes
   snapshot/restore O(Δbindings) rather than O(scope size) and removes the
   `clone_subtree` / `dying_next` / `Drop` churn — directly attacks the ~14%
   snapshot+restore and a large share of the ~30% allocator churn. Highest-leverage
   and self-contained, exactly mirroring the #2291 win one layer down.
2. **`FxHashMap<Atom, Atom>` for `forward`/`backward`.** Ordered iteration is never
   needed for a forward/backward lookup; hashing removes the remaining
   `Atom: Ord::cmp` and BTree rebalance on every bind.
3. **Cut redundant prove-gate calls in the neighbor / group paths.** The #2315
   window search (497×) and the keep-shallow group escalation (1272×) re-run the
   full matcher per candidate window/tuple; restricting each to the candidate-index
   posting intersection first (as the single-target read-off already does, #2280)
   would prune most failing matches before the matcher walks them.

(1)+(2) are the read-off→matcher analogue of #2291 and should recover the
regression to at or below the ≤10 s ideal even with the higher conversion count.
