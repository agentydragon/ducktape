# Opt-in Debundle benchmark failures

The cross-module purity benchmark still exceeds its 60-second test timeout on
`2e5e95fbbc49590990a03e03183073afc5cbfb05` (devel, November 17, 2026).
The [exact-head RBE run](https://app.buildbuddy.io/invocation/556eaeb0-89af-4264-b5e4-65e94a3851ba)
timed out after 60 seconds; its TestRunner used 62.8 CPU seconds. This is
active computation, rather than evidence of an idle wait. Earlier, an
[unchanged-baseline RBE run with a diagnostic 300-second timeout](https://app.buildbuddy.io/invocation/76b896da-ca2a-47ef-a5e9-9affa283ce4c)
also timed out, using 305.3 CPU seconds. Neither run establishes how long the
full benchmark needs to finish. Keep the fixture size and emitted-JavaScript
assertion while investigating; a larger timeout alone would not explain or
bound the cost.

The fixture has four namespace-imported peer modules. Each peer generates
4,096 object declarations and 4,096 return statements in `choose`, 2,048
property writes in `assign`, and 512 calls to `assign`. An eightfold smaller
fixture passed on [RBE](https://app.buildbuddy.io/invocation/95ae2ef5-4b45-4a54-8ac7-7c6b17393d6e)
in 8.37 seconds; that is a diagnostic, not a replacement for the full case.
Full-size progress stopped before the `compute_chunk_analysis` marker, during
`materialize_logical_chunk`. The source path from
`collect_program_summary` through `resolve_imported_callable_summaries`
enters `program_value_flows`, which constructs and solves a program-wide flow
store. A historical 30-second host `perf record` of the full fixture collected
889 samples with no loss and attributed roughly 57% of samples to Roaring
bitmap union internals (array-store iteration, copy, insertion and union).
That profile is from an older devel head and does **not** establish the same
hotspot on the exact-head RBE run. A temporary singleton-insertion fast path
still failed to reach `compute_chunk_analysis` in a 90-second host run; it was
discarded. The current [analysis performance plan](../plans/analysis_performance.md)
owns related PF7/PF8 work. The next useful experiment is an exact-head profile
with a reliable caller stack and a bounded candidate whose full-fixture output
and CPU/RSS can be compared against the same head.

The separate `propose_commutations_budget_test` generates a 4,096-step,
96-reader fixture and checks a 30-second budget around the CLI proposal phase,
not the whole test. An [earlier unchanged-baseline RBE run](https://app.buildbuddy.io/invocation/4c32d035-cb80-4e4c-b9b5-d580fd49d405)
failed at 33.821600269 seconds in that phase; a retry passed. On the same
current devel head as the purity check, a
[fresh RBE rerun with uncaptured test output](https://app.buildbuddy.io/invocation/3786f8bf-9f57-4652-8ab4-b378c4deb92f)
passed with a 19.935303025-second proposal phase and 108.9-second whole test.
These two timed observations do not establish a failure rate or a code
regression. A standalone copy of the fixture must include `cycles.json` beside
the dependency graph: without it, the CLI returns zero proposals and its fast
run is invalid. With the sidecar, the copied fixture produced one SCC and
8,190 proposals, verifying that the diagnostic CLI exercised the intended
path. Investigate any recurring budget failure using that validity check and
matched RBE timings before changing the budget or algorithm.
