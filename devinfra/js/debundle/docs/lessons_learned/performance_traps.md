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
may add constraints but never drop callback reads or rebinds. Scale fixtures:
`e2e/opaque_callback_scale_test.rs`.

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

## Memory lifetime

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
