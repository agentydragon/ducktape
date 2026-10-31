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

1. **Vendor value-flow evaluation** (Tana, about 28 s). The engine's
   storage is hashed, and the 26.3M wake checks per Tana run now take under 1%
   of the stage's samples, so batching wakes would not pay. The largest
   remaining self costs are each rule's watched-key inserts (about 7%), value
   unions (5%) and allocation (about 17%). Joining only changed keys pays for
   candidate reachability, which does it. For events it was built and measured
   with identical output: event re-evaluation reads fell from 54M to 35M, but
   the per-node join sets and per-wake key records cost more than they saved.
2. **Repeated lazy-effect promotion** (Tana main chunk, about 10 s of 40).
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
3. **Parallel callable-capability preparation** (Claude, about 4 s). The
   per-module `prepare_callable_capabilities` loop in
   `resolve_imported_callable_summaries` is independent per module, and
   running it on rayon cut Claude's stage from 11.4 to 7.4 s. It also holds
   every module's execution index at once and raised Tana's peak RSS from
   4.43 to 5.45 GiB. Bound that memory first, for example by dropping each
   index once its capabilities are extracted.
4. **Claude `cli` chunk** (22 s). The synchronous interpreter is 60% of it
   and `graph::build::promote_at_init_calls` 19%; item 2 covers part of the
   interpreter share.
5. **Startup hygiene contention** (Claude, 12 s). Chunk parse and resolve run
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
