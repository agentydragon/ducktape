# Analysis performance

Where a canonical replay still spends its time after #13313, #13314, #13315
and #13316, and the next speedups, ranked by expected gain. Edges, atomic
groups, losses and gate verdicts must stay byte-identical for every item here.

## Measured state

Local `-c opt` replays on a shared 32-CPU host. Base is devel `c032c1e5f5`,
median of three runs on Tana and five on Claude. "After" is base plus all four
PRs, median of three on Claude; for Tana it is the median of three runs of the
first three PRs stacked, since #13316 does not affect Tana.

| Stage (markers)                            | Tana base (s) | Tana after (s) | Claude base (s) | Claude after (s) |
| ------------------------------------------ | ------------: | -------------: | --------------: | ---------------: |
| callable summaries (before purity markers) |           103 |             57 |              12 |               12 |
| `prepare_cross_module_purity`              |             6 |              6 |              33 |                5 |
| `compute_chunk_analysis`, main chunk       |            53 |             40 |              27 |               23 |
| whole run                                  |           167 |            112 |              90 |               63 |

Tana's callable-summary stage is the vendor chunk's structural value flow
(`FlowProgram::solve` under `infer_callable_capabilities`). Its main-chunk
stage is 77% synchronous interpreter (`annotate_synchronous_calls`).

## Ranked follow-ups

1. **Vendor value-flow read volume** (Tana, about 50 s left). One fixpoint
   makes 53.8M fact reads over 547,800 evaluations, because member and call
   reads fan out per origin of large value sets
   (<../perf/value_flow_tana_2026_10_29.md>). After #13314 and #13315, the
   largest costs are the `FlowKey` fact-store search (12% of the stage), the
   remaining worklist bookkeeping (11%) and allocation (12%). A semi-naive
   formulation reads only the origins added since the rule's last
   evaluation. It changes rule structure, so it needs its own
   identical-output proof. Expected: most of the stage.
2. **Hashed inference storage** (Tana, 10 to 15 s). Use hash maps for the
   watcher map, scheduled map and fact store in `inference/iterative.rs`.
   Iteration order there does not affect results; only `pending` and the
   exposed facts need order. This needs `Hash` on every family's key and rule
   types, and a check that hashing the large interpreter keys
   (`RuntimeKey`) is not slower than comparing them.
3. **Repeated lazy-effect promotion** (Tana main chunk, about 10 s of 40).
   A Tana run makes 8.3M `promote_top_effects` calls, about 850 per
   statement, and 94% add nothing: every promoted read is already held for
   the same effect owner. They come from escaped and deferred callback
   promotion, which recomputes the same reached owners at each opaque call.
   Under an assumption such a call is a no-op; under observation it still
   clears tags. A per-outcome memo of promoted owners must be reset wherever
   an outcome's effects are drained (`absorb_binding_effects` takes nested
   effects). Building `referenced` once over the union of reads instead of
   per owner in `promote_raw_callbacks_transfer` was measured and gave no
   gain.
4. **Parallel callable-capability preparation** (Claude, about 4 s). The
   per-module `prepare_callable_capabilities` loop in
   `resolve_imported_callable_summaries` is independent per module, and
   running it on rayon cut Claude's stage from 11.4 to 7.4 s. It also holds
   every module's execution index at once and raised Tana's peak RSS from
   4.43 to 5.45 GiB. Bound that memory first, for example by dropping each
   index once its capabilities are extracted.
5. **Claude `cli` chunk** (22 s). The synchronous interpreter is 60% of it
   and `graph::build::promote_at_init_calls` 19%; item 3 covers part of the
   interpreter share.
6. **Startup hygiene contention** (Claude, 12 s). Chunk parse and resolve run
   in parallel, but `SyntaxContext::apply_mark` takes SWC's global hygiene
   lock and is 24% of startup samples.

## Profiling recipe

The `-c opt` binary's DWARF unwinding gives `perf` empty stacks on this host,
so build a profiling engine with
`--@rules_rust//rust/settings:extra_rustc_flag=-Cforce-frame-pointers=yes`
and record with `PERF_CALL_GRAPH=fp perf_wrapper.sh`. For inlined frames, add
`-Cdebuginfo=line-tables-only`, `-Cstrip=none` and `--strip=never`, then
resolve hot addresses with `llvm-addr2line -i`. Take timings from the
unprofiled `-c opt` binary.
