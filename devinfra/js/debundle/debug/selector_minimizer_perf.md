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

## Resolution (#2291): sorted `Vec<u32>` posting lists + `FxHashMap`

Landed targets **(1)**, **(3)**, and **(4)** — the self-contained data-structure
swap, no feature-label interning needed:

- **Posting lists / candidate sets are now ascending-sorted `Vec<u32>`**
  (`CandidateSet` in `selector_candidate_index.rs`), built in one pass via
  `push_ascending` (body indices arrive in order, so no per-element insert/sort).
  Intersection is a two-pointer linear merge into a **reused scratch buffer**;
  the greedy read-off ranks candidate features by `intersection_len` (count-only,
  no allocation) and materializes only the winner. This removes the
  `BTreeMap::Iter::next` / `IntoIter::dying_next` / `Drop` / `from_iter` cost and
  most of the small-node allocator churn.
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
| 200  | 0.0067 ms/it | 0.0038      | 1.61 µs/it      | 0.72           |
| 1000 | 0.0069 ms/it | 0.0039      | 3.26 µs/it      | 1.06           |
| 4000 | 0.0081 ms/it | 0.0042      | 8.82 µs/it      | 2.41           |

≈ **1.9× faster index build** and **3.7× faster read-off** at N=4000. Real-chunk
same-spec whole-chunk wall-clock and peak RSS in <selector_minimizer_dogfood.md>.
