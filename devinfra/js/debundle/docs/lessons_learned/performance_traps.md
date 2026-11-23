# Performance traps: analysis cost, memory and output size

Mechanisms behind past slowdowns and memory growth, kept so the same shapes
are recognized early. Measurement discipline is in <../bazel_integration.md>
§ Profiling; open candidates are in <../../plans/analysis_performance.md>.

## Callback analysis (2026-10-24)

The callback-timing change made a controlled large web chunk exceed 60 s
(the previous pin took about 4.7 s). Each layer repeated work:

| Repeated work                                                 | Repair                                                                 |
| ------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Opaque calls invoked the same callback pool recursively       | Bound body expansions per statement, independent of cache size         |
| Recursive fallback re-promoted the same owner read closure    | Fallback callbacks become escaped roots; their closure grows monotonic |
| Capture discovery revisited shared and equal capture DAGs     | Intern capture contexts bottom-up; discover each closure once          |
| Export aliases rediscovered the same callback-bearing objects | Discover exports and arguments in batches                              |
| Cached invocations copied and re-merged the full fallback set | Accumulate fallback effects in the top-level statement; bulk-merge IDs |

Depth limits and memo-entry caps did not bound the aggregate work. The bounds
may add constraints but never drop callback reads or rebinds. Benchmark fixtures:
`e2e/bench/opaque_callback_bench_test.rs`.

## Other measured shapes

- **Diagnostics that scan the graph per conflict.** `sequenced_owner_causes`
  scanned every owner edge for each conflict (conflicts × edges, 84% of CPU at
  8,192 conflicts). Use the owner's outgoing adjacency, and pass the known edge
  to the cycle renderer.
- **Inferring unused purity.** The cross-module oracle prepared every module in
  the import closure; 64% of early time was `ChunkValueFlow::analyze` for
  providers whose exports nothing consumed. Prepare only providers with a
  consumed, unasserted export. A fast reduced chunk does not establish
  full-spec performance: the full action includes package inputs and
  transforms.
- **Per-candidate recognizers.** Callgrind on one Claude Code Web run put 5.3%
  of instructions in `is_ts_enum_iife_call_for_binding`, called once per
  candidate binding at every call expression. Deriving the candidate from the
  argument shape first is the obvious fix; it needs purity-soundness tests.
  The same profile showed no duplicated whole-AST passes: similar type names
  (`ChunkAnalysis`, `ChunkFacts`) and overlapping visits over one tree are not
  evidence of duplicate work.
- **Duplicate parses.** `propose-commutations` cloned and reparsed the verified
  source to build its selector index; reuse `PreparedSource::module()`.
- **Scoping an unknown call's callback reach.** Provider-chunk and export-region
  scoping left 0 `callback_pool_limit` events under 64, even as an offline upper
  bound. Tana: median pool 617; of 399 distinct escaped callbacks 64 come
  through vendor calls, and `new Fx` (86), `Object.*` (75) and class-array
  iteration (64) account for 225. Claude CLI 2.1.292: median 1,241; conservative
  export reentry puts every exported callable in the pool and 87% of events fit
  without them, so the
  [reentry policy](../../SPEC.md#reentry-through-imported-names) is the lever,
  not havoc reach.
- **Raising `MAX_SYNCHRONOUS_CALLBACK_POOL`.** From 64 to 512, the Tana main
  chunk's `compute_chunk_analysis` did not finish in 31 min on a `-c opt` build
  (64: 46 s). Invoking the real pools instead of summarizing them is unusable at
  this size.
- **Reachability bounds on pools.** Admitting an escaped callback only when its
  holder is reachable from the call's arguments, publications, globals or
  vendor-held objects leaves Tana's median pool at 121 (max 187), so held
  escapes alone exceed 64. Only proving that escaped callbacks are never
  reinvoked during initialization (median pool 23) is decisive for its
  1,805-module SCC. Reported reach was `bounded` for 1 of 120 Tana and 4 of 88
  Claude constructions; the rest were `borrowed_receiver`, `tainted_input` or
  `unknown_callee`.

## Memory lifetime

- Compact source does not bound fact count. An alias/property matrix with
  O(N + P) syntax produces N × P possible property cells. Storing empty proof
  dependencies inline in every judgment magnifies that relation; allocate
  dependency payloads only when nonempty, preserving proof-growth propagation.
  The CLI/Node resource regression is `e2e/bench/source_fact_memory_bench_test.rs`.
  After changes to source-value facts, run it explicitly with
  `bbr test //devinfra/js/debundle/e2e/bench:source_fact_memory_bench_test`;
  its `manual` tag excludes it from ordinary CI. The small file-backed case in
  `e2e/cases/emission/source_fact_matrix` keeps an emitted-output witness in CI
  but does not enforce the large-input memory bound.
- Measure the peak of the completed command after each repair. Boxing one
  `FlowFact` variant shrank the enum from 56 to 32 bytes without measurably
  reducing peak RSS after empty-proof compaction. The completed heap profile
  instead located the dominant retained allocations in program-flow
  projections and synchronous interpreter property state. A smaller transient
  solver record did not shrink those live tables.
- A truncated, resource-capped profile locates allocations in its captured
  prefix; it cannot identify the eventual whole-command peak. A short allocation
  trace can still be in provider preparation while a plain-speed run has reached
  value-flow fact and subscription insertion. Confirm the relevant phase before
  choosing a target. Keep profiler overhead distinct from uninstrumented RSS and
  do not treat an interrupted analysis as a completed artifact.
- Reparsed provider ASTs stayed live through parallel per-chunk analysis only
  because context ordinals were built at the end. Build `ContextOrdinals`
  (IDs only) right after cross-module analysis, then drop the ASTs.
- Decoding inflated the whole compressed analysis into a `Vec<u8>` before
  deserializing. Stream `rmp_serde` from the zstd decoder, then drain the rest
  of the stream: stopping at the end of the MessagePack value would skip the
  frame checksum and accept corruption.
- Source-premise providers were reparsed after preparation released their AST,
  through a different source representation. Retention is an explicit input to
  preparation, and analysis-only retention borrows the original source
  (`JsFileBody::SourceWithAst`).

## Output size

Default reports are bounded views with exact totals; full evidence is an
explicit sink. Measured before bounding: export-reentry JSON was 16.9 MiB over
75 chunks (614 request rows repeated in every chunk); one edge-TV report was
92.7 MiB, and compacting its conflict array still left 63 MiB. Bounded views
are serialize-only and cannot be deserialized as the semantic report;
acceptance always reads the complete counters.

Grouping outputs does not shrink computation: `split_dependency_graphs` once
moved full graphs into a second output directory of the same action, so every
default build still serialized and uploaded them. Full graphs are now a
separate on-demand `DebundleExportGraphs` action reading the same Analyze
artifact. That action exits 3 with reports-only output on a typed gate
rejection, so a rejected assignment still yields its graphs.

Console summaries are bounded too: a 13,634-character atomic-group header with
thousands of claim rows hid the failure when the full JSON was already written
and linked.
