# `modules propose` performance

State and optimization plan for the proposer hot path and its
realizability gate. This is an active roadmap — it lists open or
conditional next work, not completed implementation history. Resolved
items are deleted.

## Current state

The hot boolean merge gate goes through the `RealizabilityIndex`'s tier ladder
(call tree below).

The reproducible public corpus is the synthetic one from
`perf/gen_synth_corpus.py` (10k statements, seed 1; 10051 owners / 22019
edges). Baseline, `-c opt` binaries, one host, 2026-06-11:

| Corpus variant                                          |     Wall | Proposals |
| ------------------------------------------------------- | -------: | --------: |
| fully residual (`--claim-blocks 0`)                     | 3.5–3.8s |      1216 |
| 62 claimed modules (`--claim-blocks 62`, 2461 bindings) | 2.2–2.3s |       933 |

**Never use `fastbuild` numbers for Rust wall comparisons** — always
build `-c opt` (a `fastbuild` binary measured 35× slower on a proposer fixture).

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
                ├── tier 1: constraining CondensationOrder (DSU + cone DFS)
                ├── tier 2: I-graph CondensationOrder (exact_multi_scc fallback
                │           on removal-inside-SCC overlays)
                └── tier 3: shared EsmEvaluationSimulator over the I-SCC

contract / explicit diagnostic query
└── would_be_cycles_after_contract
    ├── ladder_decision_for_merge          (accept → no evidence)
    └── realizability_index::verdict_after_moving_owners_touching
        └── build_simulator / translate_verdict_with_owner_modules
```

## Optimization policy

Add no more proposer gate machinery without a fresh measurement. If
proposer latency becomes important again:

1. Build and run an optimized binary (`-c opt`, with debug info).
2. Profile the corpus that matters with `perf_wrapper.sh`
   (<../docs/bazel_integration.md> § Profiling); use Callgrind for exact call
   counts on a reduced input and heaptrack for allocation.
3. Do not act on sampled attribution alone: compare wall time of the same
   `modules propose` run before and after the change across repeated runs.
4. Stop if the measured wall delta is inside normal run-to-run noise.

## How to run

On the reproducible synthetic corpus; substitute your own `GRAPH`/`MODULES`
for a real corpus:

```bash
direnv exec . bash -lc 'bazelisk build //devinfra/js/debundle:debundle \
    -c opt --@rules_rust//:extra_rustc_flag=-Cdebuginfo=1 \
    --remote_download_outputs=toplevel'
BIN=./bazel-out/k8-opt/bin/devinfra/js/debundle/debundle

python3 devinfra/js/debundle/perf/gen_synth_corpus.py \
    --out /tmp/synth --statements 10000 --seed 1 --claim-blocks 62
"$BIN" run --spec /tmp/synth/spec.json

devinfra/js/debundle/perf_wrapper.sh --output-dir /tmp/propose-profile -- \
    "$BIN" modules propose \
    --graph /tmp/synth/out/reports/tree/static/app/owner_graph.json \
    --modules /tmp/synth/modules --format json
```

## Backlog

There is no active P1 proposer-latency blocker. The items below are
conditional — gated on a fresh profile showing the relevant work hot.

### #1 — Fresh post-fix profile

If proposer wall becomes material again, collect a fresh optimized
profile and treat it as the new source of truth for choosing work.

### #4 — Skip `build_simulator` rebuild when inputs are unchanged (conditional)

`build_simulator` has a strict-zero fast path (`overlay_is_simulator_noop`).
A looser check could reuse the base simulator when the overlay's
`i_delta` adds no new `(from, to)` pair and only references base edges
that remain positive. Verify against a fresh profile first.

### #6 — Per-merge updates to the persistent realizability index

Every `contract` pushes deltas to `realizability_index`. Cost depends on the
index's internal representation. Investigate only if a fresh profile
shows this material.

## `debundle run` pipeline

The `debundle run` pipeline wall is a different optimization surface
from the proposer. Current unmeasured opportunities:

- **AST-hash codegen cache**: content-address the post-lowering AST and
  reuse SWC emit output when unchanged.
- **Chunk-level incremental rebuild**: hash `(upstream_bytes, spec_slice,
ducktape_version)` per chunk and skip lowering, codegen, and reports
  for unchanged chunks.
- **Opt-in heavy reports**: add `--reports=<list>` so consumers can skip the
  per-chunk reports they do not need (`output_layout.rs` lists them).

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
  the gate from the condensation and only takes its exact fallback
  (`exact_multi_scc`) on removal-inside-SCC overlays.
