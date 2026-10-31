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

1. **Vendor value-flow worklist cost** (Tana, about 45 s). Almost every
   one of the event stratum's 54M fact reads comes from a re-evaluation (first
   evaluations read 0.3M), and 26.3M wake checks fan changed keys out to their
   watchers. Candidate reachability now re-joins only the keys that changed.
   For events, joining only changed keys at each member and call node was
   built and measured with identical output: event re-evaluation reads fell
   from 54M to 35M, but the per-node join sets and per-wake key records cost
   more than they saved (851G user instructions per Tana run against 844G for
   candidates alone). Invocations, 21M of the remaining reads, would also need
   grown parameters re-read within one evaluation. The remaining cost is
   per-read and per-wake bookkeeping, which item 2 addresses.
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
