# `modules propose` Optimization Roadmap

Working list of unshipped optimizations for the proposer hot loop, in
priority order. Companion to [`2026_05_27_propose.md`](2026_05_27_propose.md),
which has the perf measurements and post-mortems on landed changes.

Items move to the perf doc as they ship. This file is the queue.

## P0: Suspected order-of-magnitude wins

### #0 — Released `debundle` binary is built in `fastbuild`, not `opt`

`.github/workflows/release.yml` line 76 routes the debundle artifact
through `bb-out/bazel-out/k8-fastbuild/bin/devinfra/js/debundle/debundle`.
The `k8-fastbuild` segment is bazel's compilation_mode signature —
this is fastbuild, the default mode with no LLVM optimizations and
`debug_assertions = on`. There is no `-c opt` or `--compilation_mode=opt`
on the bazel invocation.

The Pearce-Kelly agent's measurement showed:

| Build mode | Wall (tana fixture) |
| ---------- | ------------------: |
| fastbuild  |               320 s |
| opt        |                46 s |

So the released binary that gaffer pins via `archive_override` +
`http_file` is running **~7× slower** than it could. The 8% `precondition_check`
overhead documented in `2026_05_27_propose.md` (CPU hotspots table) is
not just debug-assertions — it's the entire LLVM optimizer being off.

**Fix:** add `-c opt` (or `compilation_mode = "opt"` in the matrix
entry) to the release workflow's bazel build command for the
`debundle` artifact. One-line workflow change; the next release tag
will publish a binary that's ~7× faster on cold runs.

**Estimated impact:** 5-minute `modules propose` on the tana corpus
drops to ~45 seconds at the bazel command level. Same factor would
likely apply to the rest of the `debundle run` pipeline (other side
of the same binary).

**Caveat:** verify the release-workflow target uses the same bazel
arguments as our local `bazelisk build -c opt //devinfra/js/debundle:debundle`
benchmarks. A `compilation_mode=opt` config option exists in some
Bazel workflows; if `release.yml` already sets it elsewhere, the path
just isn't being exercised on this matrix entry.

## P1: Per-pop hot loop (after EdgeState refactor lands)

These are what `perf record` will likely surface as the new top
costs once the EdgeState refactor (in flight as PR `…/debundle-unified-edge-state`)
collapses the 7-BTree-field churn.

### #1 — SipHash → FxHash on `owner_id_to_idx: HashMap<String, OwnerIdx>`

~6% self in the current profile (sip::d_rounds, c_rounds, Hasher::write,
RandomState). DoS resistance is irrelevant on this internal data
structure. Trivial swap once `rustc-hash` (or equivalent) is in the
workspace — the EdgeState refactor brings it in.

### #2 — Verify `-C debug-assertions=off` in opt builds

Even after #0 lands, double-check that the release binary's
compilation actually disables `debug_assertions` (rules_rust's
opt-mode config). The `precondition_check` symbols should disappear
from the profile entirely.

### #3 — Cache `rank_candidate`'s cycle-reduction byte

`rank_candidate` (in `peel/quotient.rs`, byte 0 of the 33-byte
sort key) allocates a `BTreeSet<usize>` per call to intersect two
cycle-index lists. Called once per PQ pop; each merge fires hundreds
of pops.

Options:

- Precompute a `class_pair → bool` table once at PQ init and on
  contract; lookup at rank time.
- Replace the BTreeSet allocation with a linear merge of the two
  index lists (they're usually length ≤ 1 in practice).

The latter is the cheaper change.

### #4 — Epoch-buffer for `would_create_cycle`'s `visited` set

Currently allocates a `BTreeSet<ClassId>` per call. Replace with a
shared `Vec<u32>` on `TopoOrder` indexed by `ClassId.0`, with a
bump counter for the epoch. A node is "visited this call" if
`visited[c.0] == current_epoch`. Eliminates one allocator round-trip
per cycle check.

Flagged in the PK post-mortem. **Conflicts with the EdgeState
refactor while it's in flight** (same function signature is changing).
Land after EdgeState merges.

## P2: Per-merge sync costs (latent until per-pop work shrinks)

### #5 — Incrementalize `rebuild_class_to_cycle_indices`

`update_cycle_cache_after_merge` calls `rebuild_class_to_cycle_indices`
after every merge (line 1723). That function clears `class_to_cycle_indices`
and re-walks the **entire** `cached_cycles` vec — `O(sum of cycle sizes)`
per merge.

In practice this fires only when `cached_cycles` is non-empty (i.e.,
when the partition has known cycles). On well-formed corpora like
tana the post-seed partition is realizable, `cached_cycles` stays
near-empty, and the function returns early at line 1694. So this is
a **corner-case** fix for cyclic seeds, not a hot-path cost on
healthy specs.

**However**, the structural problem is still wrong: when it does
fire, the work scales with total cycles × cycle size, not with
affected cycles. The incremental shape:

- Step 2 (rewrite cycle classes for affected indices): when a class
  is removed from a cycle.classes vec, remove `idx` from
  `class_to_cycle_indices[c]` for that class. When a class is added,
  push `idx` to `class_to_cycle_indices[c]`.
- Step 3 (compact dropped cycles): rather than rebuilding
  `cached_cycles` as a new Vec + full rebuild, use a "tombstone"
  pattern — leave dropped cycles as `Option<CycleClassSet>` with
  `None`, skip on reads. Compact on demand (e.g., when tombstone
  ratio crosses a threshold), at which point the index needs a
  shift adjustment that can also be done in O(|tombstones|).

Per-merge work becomes `O(affected cycles · affected cycle size)`,
which is small.

**Priority:** low on healthy corpora; high on corpora with pre-existing
cycles. Defer until profiles show it firing.

### #6 — `sync_index_after_merge` to the persistent realizability index

Every merge pushes deltas to `realizability_index`. The cost depends
on the index's internal representation. If profiling after #1-#3
shows it surfacing, the gate's incremental representation can
probably be tightened.

Not measured yet. Investigate after the per-pop work flattens.

## P3: Architectural alternatives (orthogonal to the wall)

### #7 — KL/FM refinement pass after greedy

Improves cut quality 10-30% over pure greedy. Constant wall cost.
Only worth doing if reviewers complain about proposal _quality_
rather than wall time.

### #8 — Topological-sweep alternative driver

`O(V + E)` total, no cycle check needed (the topo order itself is
the safety guarantee). Different output character than agglomerative
greedy. Useful as a baseline to measure greedy quality against.

### #9 — 32-bit ClassId / OwnerIdx

Halves the cache footprint of edge maps and adjacency vecs. ~5-10%
expected on tana scale. **Premature** until the structural collapses
(EdgeState + the bucket fixes) land — wide touch surface for a
single-digit-pp win.

### #10 — Replace greedy entirely (Louvain-with-constraints, spectral)

Uncertain quality; significant project. Last resort.

## `debundle run` (different command, different bottleneck)

Tracked here for completeness; not strictly part of the proposer
roadmap.

- **AST-hash codegen cache** — SWC `emit_with` is ~30% of `debundle
run` cycles per `2026_05_26.md`. Content-address post-lowering AST;
  reuse emit if seen. Biggest cold-wall lever; architecturally
  invasive.
- **Chunk-level incremental rebuild** — hash `(upstream_bytes,
spec_slice, ducktape_version)` per chunk; skip lowering + codegen
  - reports for unchanged chunks. 10× on hot iteration cycles, zero
    on cold. Architecturally invasive.
- **Opt-in heavy reports** — pipeline always emits atoms /
  owner_graph / atomic_units / realizability / factorize /
  peel_candidates JSON. Most consumers want a subset. `--reports=<list>`
  flag, default to current set. 5-15% cold wall.

## Hold / done

- ✅ `class_neighbors` non-allocating iter (commit `641d6d9d3`).
- 🔄 Unified EdgeState (PR `debundle-unified-edge-state` in flight).
- ✅ Pearce-Kelly + reverse index (PR #1700, commit `5bcc77279`).
- ✅ `write_tree_reports` rayon (commit `7b69a8c0f`).
- ✅ `lower_chunk` rayon (commit `951957122`).
- ✅ `vendor::strip` rayon.
- ✅ `materialize_artifact_scripts` rayon.
