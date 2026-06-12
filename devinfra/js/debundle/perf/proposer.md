# `modules propose` performance

State and optimization plan for the proposer hot path and its
realizability gate. This is an active roadmap — it lists open or
conditional next work, not completed implementation history. Resolved
items are deleted.

## Current state

The gate-ladder cutover (`plans/incremental_gate_unification.md`,
PRs #2087/#2090/#2095/#2102) routed the hot boolean merge gate
through the `RealizabilityIndex`'s tier ladder and deleted the
kernel-side Pearce–Kelly walk, cone-DFS fallback, and
`cached_cycles` machinery.

The historical baseline corpus (a private downstream fixture, 9709
owners, measured at 3.54s pre-cutover) is not available to public
CI. The reproducible public stand-in is the synthetic corpus from
`perf/gen_synth_corpus.py` (10k statements, seed 1; 10051 owners /
22019 edges — same scale and shape class). Post-cutover validation
numbers, `-c opt` binaries, interleaved pre/post runs on one host,
2026-06-11:

| Corpus variant                                          | Pre-cutover wall | Post-cutover wall | Proposals |
| ------------------------------------------------------- | ---------------: | ----------------: | --------: |
| fully residual (`--claim-blocks 0`)                     |         6.7–8.0s |          3.5–3.8s |      1216 |
| 62 claimed modules (`--claim-blocks 62`, 2461 bindings) |        9.4–10.7s |          2.2–2.3s |       933 |

Proposal output is byte-identical pre↔post on both variants (the
cutover's semantic fixes only bite on the cataloged corner shapes —
none occur in either corpus). Gate-ladder tier distribution
(`DEBUNDLE_TIMING=1`, post-cutover binary):

| Variant  | Queries | Tier 0 accept | Tier 1 reject | Tier 2 accept | Tier 3 | Tier 1+2 wall |
| -------- | ------: | ------------: | ------------: | ------------: | -----: | ------------: |
| residual |    8834 |          8834 |             0 |             0 |      0 |        0.000s |
| claimed  |    9944 |          6644 |           753 |          2547 |      0 |        0.265s |

This is well inside the plan's ship budget (wall within noise of the
pre-cutover number; tier-3 simulator builds in single digits;
tier-1+2 cumulative ≤ 2× the old PK-gate hot path): the post-cutover
proposer is 1.9× faster on the fully-residual shape and 4.3× faster
on the claimed shape, tier-3 never fired, and overlay simulator
rebuilds and `scc_containing` calls were both zero.

**Never use `fastbuild` numbers for Rust wall comparisons** — always
build `-c opt` (a `fastbuild` binary measured 35× slower on the
historical fixture).

The hot path asks a boolean question and avoids diagnostic-evidence
generation; the diagnostic path is the same ladder with evidence
materialization enabled:

```text
greedy_merge_to_convergence
└── merge_preserves_invariants
    └── check_merge_boolean
        └── ladder_decision_for_merge
            └── realizability_index::ladder_decision_after_moving_owners_touching
                ├── tier 0: delta-free → cached pre-state verdict
                ├── tier 1: constraining CondensationOrder (DSU + PK window-DFS)
                ├── tier 2: I-graph CondensationOrder (scc_containing fallback
                │           on removal-inside-SCC overlays)
                └── tier 3: shared EsmEvaluationSimulator over the I-SCC

contract / explicit diagnostic query
└── would_be_cycles_after_contract
    ├── ladder_decision_for_merge          (accept → no evidence)
    └── realizability_index::verdict_after_moving_owners_touching
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
8. Gate-ladder per-tier counters: decision counts per
   `LadderDecision` variant (tier-0 accept/reject, tier-1
   cycle/rebind reject, tier-2 accepts, tier-3 accept/reject) plus
   per-tier cumulative wall under `DEBUNDLE_TIMING=1` — the "gate
   ladder:" / "ladder wall:" stderr lines.

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

How to run (on the reproducible synthetic corpus; substitute your own
`GRAPH`/`MODULES` for a real corpus):

```bash
direnv exec . bash -lc 'bazelisk build //devinfra/js/debundle:debundle \
    -c opt --@rules_rust//:extra_rustc_flag=-Cdebuginfo=1 \
    --remote_download_outputs=toplevel'
BIN=./bazel-out/k8-opt/bin/devinfra/js/debundle/debundle

python3 devinfra/js/debundle/perf/gen_synth_corpus.py \
    --out /tmp/synth --statements 10000 --seed 1 --claim-blocks 62
"$BIN" run --spec /tmp/synth/spec.json

DEBUNDLE_TIMING=1 "$BIN" modules propose \
    --graph /tmp/synth/out/reports/tree/static/app/owner_graph.json \
    --modules /tmp/synth/modules --format json \
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

### #4 — Skip `build_simulator` rebuild when inputs are unchanged (conditional)

`build_simulator` has a strict-zero fast path (`overlay_is_simulator_noop`).
A looser check could reuse the base simulator when the overlay's
`i_delta` adds no new `(from, to)` pair and only references base edges
that remain positive. Verify against a fresh profile first.

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
  `apply_emission_rewrites`; avoid whole-graph clone/rescan patterns where
  a graph pass or indexed lookup can answer the same question.
- Consider changing per-chunk `file_records` from an ordered vector of
  `(file, role)` pairs into a typed map if output consumers do not depend
  on order. Keep the manifest easy to diff and read.

## Avoid

- Do not revive the base-SCC cache + overlay-short-circuit approach. The
  proposer queries the move destination `to`, and candidate overlay
  edges are incident to `to`, so the overlay touches the queried SCC in
  the representative workload. The landed `CondensationOrder` ladder
  (tiers 1–2) is the maintained-SCC design that works here — it answers
  the gate from the condensation and only falls back to
  `OverlayGraphView::scc_containing` on removal-inside-SCC overlays.
