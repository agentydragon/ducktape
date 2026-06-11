# `modules propose` performance

State and optimization plan for the proposer hot path and its
realizability gate. This is an active roadmap — it lists open or
conditional next work, not completed implementation history. Resolved
items are deleted.

## Current state

`modules propose --format json` against the tana `78d928dca7` fixture,
source-built `-c opt` binary, 2026-05-27:

| Metric                     |  Value |
| -------------------------- | -----: |
| Wall                       |  3.54s |
| Proposals                  |     93 |
| `scc_containing` calls     |     10 |
| `scc_containing` wall      | 0.041s |
| `verdict_touching` calls   |      5 |
| Overlay simulator rebuilds |      5 |
| Overlay simulator wall     | 0.148s |
| Diagnostic translations    |      6 |

The proposer-latency problem is fixed. A source `fastbuild` binary
measured 124.31s on the same workload; **never use `fastbuild` numbers
for Rust wall comparisons** — always build `-c opt`.

Gate diagnostics are **not** the active wall problem: SCC lookup,
simulator rebuild, and diagnostic translation counts are all single
digits. The next proposer optimization must start from a fresh
optimized profile, not from pre-fix gate counters.

The hot path asks a boolean question and avoids diagnostic-evidence
generation:

```text
greedy_merge_to_convergence
└── merge_preserves_invariants
    └── check_merge_boolean
        └── would_violate_cycle_gate_after_contract
            └── merge_creates_new_constraining_cycle
```

The full verdict/evidence path remains for `contract` and explicit
diagnostic queries:

```text
contract / explicit diagnostic query
└── would_be_cycles_after_contract
    └── realizability_index::verdict_after_moving_owners_touching
        └── verdict_with_overlay_touching(to, &overlay)
            ├── constraining_graph.scc_containing(to)
            ├── i_graph_view.scc_containing(to)
            └── build_simulator / translate_verdict_with_owner_modules
```

## Optimization policy

Do not implement more proposer gate machinery from old profiles. If
proposer latency becomes important again:

1. Build and run an optimized binary (`-c opt`).
2. Capture `DEBUNDLE_TIMING=1` counters on the corpus that matters.
3. Use direct counters for the suspected boundary; profile attribution
   alone is not enough for this code path.
4. Stop if the measured wall delta is inside normal run-to-run noise.

Cheap integer/shape counters stay always-on. Wall-clock timing, stderr
reports, and shadow-graph traversals stay behind `DEBUNDLE_TIMING=1`.

## Gate perf counters (reference)

Permanent diagnostic counters for the proposer's realizability gate
live in `realizability.rs::gate_perf_counters`, exposed through the
`SccTimingReporter` RAII guard. They cover the path through
`IncrementalQuotient::verdict_with_overlay_touching` and its no-overlay
cousin `verdict_touching`:

1. `OverlayGraphView::scc_containing` calls, split overlay-empty /
   overlay-non-empty.
2. `scc_containing` cumulative wall time (only under `DEBUNDLE_TIMING=1`).
3. Overlay shape histograms: `delta.len()`, additions, removals.
4. Verdict counters: `verdict_touching` calls, overlay-call subset,
   realizable/rejected split, SCC sizes, constraining-pair hits.
5. Simulator counters: requests, structural-no-op vs structural-changed,
   base rebuild count/time, overlay rebuild count/time.
6. Diagnostic translation counters: calls, active vs bypassed,
   owner-module vector size, unrealizable-SCC count.
7. Base-graph snapshot rebuilds: opt-in shadow `tarjan_scc` over each
   stale base graph to estimate snapshot+clone designs.

When `DEBUNDLE_TIMING` is unset, normal runs pay only the cheap counter
path (atomic increments + bounded integer histograms): no
`Instant::now()`, no report output, no shadow Tarjan.

Design notes: `OnceLock<bool>` enabled-check (first call resolves
`std::env::var_os`, later calls are atomic loads); the proposer is
single-threaded so the bounded-histogram mutex has no contention;
histograms are reservoir-sampled (`RESERVOIR_CAP=4096`) with percentiles
computed on report; output is stderr (stdout is reserved for proposal
JSON). Add new counters next to the existing ones when validating a new
hot-path hypothesis; keep cheap `O(1)` counters ungated and gate only
timing / report output / extra traversals behind
`gate_perf_counters::enabled()`.

How to run:

```bash
direnv exec . bash -lc 'bazelisk build //devinfra/js/debundle:debundle \
    -c opt --@rules_rust//:extra_rustc_flag=-Cdebuginfo=1 \
    --remote_download_outputs=toplevel'

GRAPH=/path/to/owner_graph.json
MODULES=/path/to/spec/modules

DEBUNDLE_TIMING=1 ./bazel-out/k8-opt/bin/devinfra/js/debundle/debundle \
    modules propose \
    --modules "$MODULES" --graph "$GRAPH" --format json \
    > /tmp/propose.json 2> /tmp/timing.txt
cat /tmp/timing.txt
```

## Backlog

There is no active P1 proposer-latency blocker. The items below are
conditional — gated on a fresh profile showing the relevant work hot.

### #1 — Fresh post-fix profile

If proposer wall becomes material again, collect a fresh optimized
profile and fresh `DEBUNDLE_TIMING=1` report and treat that as the new
source of truth for choosing work.

### #2 — Broaden the boolean gate into `RealizabilityIndex` (conditional)

Only if a fresh profile shows simulator/diagnostic work hot outside the
quotient cycle gate. Add a
`would_remain_realizable_after_moving_owners_touching`-style boolean
query and short-circuit in order:

- cross-gate rebind touching the post-move target rejects;
- constraining SCC containing the target with size >= 2 rejects;
- I-SCC size < 2 accepts;
- I-SCC without an effective constraining pair accepts;
- only then build/run the simulator.

Keep the full verdict/evidence path as the diagnostics path and as a
debug oracle.

### #3 — Incremental SCC maintenance on the gate view (conditional)

Only if a fresh profile shows `scc_containing` hot again. See the
"Conditional SCC gate design" appendix for the V1.5 / V2 designs. The
narrow snapshot+clone design works iff `base SCCs <= ~1000` and overlay
`delta.len()` is much smaller than 50; tana's observed shape fits.
Prefer the broader class-aware gate boundary if it stays reviewable —
it also removes projection and diagnostic overhead.

### #4 — Skip `build_simulator` rebuild when inputs are unchanged (conditional)

`build_simulator` has a strict-zero fast path (`overlay_is_simulator_noop`).
A looser check could reuse the base simulator when the overlay's
`i_delta` adds no new `(from, to)` pair and only references base edges
that remain positive. Verify against a fresh profile first.

### #5 — Incrementalize `rebuild_class_to_cycle_indices` (corner case)

`update_cycle_cache_after_merge` calls `rebuild_class_to_cycle_indices`
after every merge, which clears `class_to_cycle_indices` and re-walks
the entire `cached_cycles` vec — `O(sum of cycle sizes)` per merge. Only
matters when `cached_cycles` is non-empty. Defer until profiles show it.

### #6 — `sync_index_after_merge` to the persistent realizability index

Every merge pushes deltas to `realizability_index`. Cost depends on the
index's internal representation. Investigate only if a fresh profile
shows this material.

### P2 alternatives

- **#7 — KL/FM refinement pass after greedy.** Improves cut quality, not
  wall. Worth doing if proposal quality becomes the bottleneck.
- **#8 — Topological-sweep alternative driver.** `O(V + E)` total, no
  cycle check needed. Different output character than agglomerative
  greedy; useful as a quality/perf baseline. Removing the outer greedy
  candidate-pop factor requires this kind of different driver.
- **#9 — 32-bit ClassId / OwnerIdx.** Halves cache footprint of edge
  maps and adjacency vecs. Modest expected impact, wide touch surface.
- **#10 — Replace greedy entirely.** Louvain-with-constraints, spectral,
  etc. could change the quality/perf tradeoff but are a separate design
  project.

## `debundle run` pipeline

The `debundle run` pipeline wall is a different optimization surface
from the proposer. Current unmeasured opportunities:

- **AST-hash codegen cache**: content-address the post-lowering AST and
  reuse SWC emit output when unchanged.
- **Chunk-level incremental rebuild**: hash `(upstream_bytes, spec_slice,
ducktape_version)` per chunk and skip lowering, codegen, and reports
  for unchanged chunks.
- **Opt-in heavy reports**: add `--reports=<list>` so consumers can skip
  atoms / owner_graph / atomic_units / realizability / factorize /
  peel_candidates reports they do not need.

### Materialize-stage hot-loop optimizations

Ordered by leverage. Re-profile before implementation if the consumer
corpus or pipeline shape has changed materially.

1. **Re-profile / shrink `artifact::write_tree_reports`.** A previous
   profile showed 6.42% Children % here, dominated by serde_json
   pretty-print of `DirectoryManifestIndex` / `DirectoryBoundarySummary`.
   Generated reports now use compact JSON; re-profile before more work,
   then shrink the on-wire shape if this path remains hot.
2. **`vendor::strip::sweep_unreachable_top_level`.** 6.40% Children % in
   the prior profile. Likely amenable to indexed reachability or
   per-chunk caching.
3. **Use the overlay realizability fast path where hypothetical moves
   remain.** Candidate-style evaluation should use `RealizabilityIndex`'
   `verdict_after_moving_owners_touching` where possible instead of the
   rollbacking push/scope path, avoiding mutation of the maintained
   quotient during repeated what-if checks.
4. **Keep harness emission proportional to the work.** Most remaining
   `emit_browser_harness` cost is `materialize_artifact_scripts` →
   `write_tree_reports` (item 1), not the harness JS emission. Split
   browser-harness generation from non-browser runs where practical, and
   avoid recopying unchanged non-JS assets.
5. **AST visit churn in `prepare_js_chunks`.** SWC parser / lexer /
   `visit_children_with` still occupy ~10–15% summed across many
   sub-2.5%-self entries. No single parser symbol is over the priority
   threshold; revisit after items 1–2.

### Graph pass performance and module boundaries

Tighten before the next large peel loop:

- Keep stage telemetry complete (index build/rebuild, fused AST
  analysis, purity, owner-graph construction, atomic-DAG construction,
  quotient construction, validation, lowering, output writing) — useful
  durations should land in the emitted reports.
- Move repeated timing helpers into one shared Rust module once a second
  pass needs them outside the current local macro sites.
- Add focused regression coverage for `ArtifactIndexes` rebuild
  boundaries as more structural artifact mutations are optimized.
- Profile the debundle action around `materialize_logical_modules` and
  `rename_vendor_exports`; avoid whole-graph clone/rescan patterns where
  a graph pass or indexed lookup can answer the same question.
- Consider changing per-chunk `file_records` from an ordered vector of
  `(file, role)` pairs into a typed map if output consumers do not depend
  on order. Keep the manifest easy to diff and read.

## Avoid

- Do not revive the base-SCC cache + overlay-short-circuit approach. The
  proposer queries the move destination `to`, and candidate overlay
  edges are incident to `to`, so the overlay touches the queried SCC in
  the representative workload.

---

## Appendix: Conditional SCC gate design

Future-plan reference for `OverlayGraphView::scc_containing` and related
proposer gate work. **Not an active implementation plan** — current
optimized tana proposer wall is 3.54s and gate diagnostics are not the
bottleneck. Do not implement incremental SCC maintenance unless a fresh
optimized profile shows `scc_containing` hot again.

The narrow SCC target replaces this per-query shape:

```text
O(reachable_forward(to) + reachable_reverse(to))
```

with an affected-region query over a maintained quotient view:

```text
O(D + R)
```

where `D` is the overlay size / moved-owner incident-edge count for one
candidate and `R` is the target-specific reachable region. Worst-case
`R` is still the whole quotient graph, so this is a better
parameterization, not a better theoretical bound.

### V1.5: targeted condensation reachability

Keeps a per-base-graph snapshot:

```rust
struct BaseSnapshot {
    sccs: Vec<Vec<ModuleId>>,
    scc_of: HashMap<ModuleId, usize>,
    condensation_mult: HashMap<(usize, usize), u32>,
    condensation_out: Vec<Vec<usize>>,
    condensation_in: Vec<Vec<usize>>,
}
```

Per query: map `to` and overlay endpoints to base SCCs (synthetic
singleton SCCs for absent endpoints); project module-edge deltas into
SCC-edge deltas; if an overlay removal zeroes an edge inside one base
SCC, fall back to the full DFS (the base SCC may split); run forward and
reverse reachability from `target_scc` over effective condensation
edges; intersect visited sets and materialize member modules. Lowest-risk
SCC-only design — keeps the module-projection model, needs only localized
query changes.

### V2: owner-SCC index plus partition view

The owner graph is constant during `modules propose`; only its
projection through the current module partition changes. V2 computes
owner SCCs once and maintains a partition view:

```rust
struct OwnerSccIndex {
    owner_sccs: Vec<Vec<OwnerId>>,
    owner_scc_of: HashMap<OwnerId, usize>,
    cross_scc_out: Vec<Vec<usize>>,
    cross_scc_in: Vec<Vec<usize>>,
}

struct PartitionView {
    member_count: Vec<HashMap<ModuleId, u32>>,
    multi_module_sccs: HashSet<usize>,
    owner_sccs_per_module: HashMap<ModuleId, HashSet<usize>>,
}
```

Every owner move updates `PartitionView` in the same push/undo lifecycle
as `RealizabilityIndex`. Per-overlay queries apply a temporary diff, run
bidirectional BFS through represented owner SCCs, and materialize the
target component's modules. Faster steady state than V1.5, but more
maintenance surface.

### Edge cases to cover

- Overlay endpoints absent from the base graph.
- Overlay removals that can split one base SCC.
- Duplicate owner moves in one candidate overlay.
- Push/undo rollback of `PartitionView` and any boolean index state.
- Multi-target / non-standard gate transitions that cannot be modeled as
  one post-merge target module — fall back to scoped push/verdict/undo
  or model explicitly.
- Diagnostic drift: boolean pass/reject must stay byte-identical to the
  full verdict path for emitted proposals and reported blockers.

### Tests and gates before shipping any SCC design

- Unit tests for empty overlays, absent endpoints, base-internal
  edge-removal fallback, duplicate moves, and rollback.
- Oracle tests comparing boolean results with the full verdict path
  across synthetic graphs and the tana fixture.
- Corpus gate: `modules propose --format json` byte-identical against
  current head.
- Benchmark gate: optimized wall improves by >= 3s averaged across
  interleaved runs, or the change does not ship as a perf fix.
