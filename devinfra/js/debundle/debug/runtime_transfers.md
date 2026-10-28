# Runtime transfer migration performance

The frozen baseline is b3697f5ee41de9fbbe1742c196f3ea594d0d10c0. Its
optimized binary, neutral controls and reproduction commands are staged in
`/tmp/debundle-runtime-baseline/README.md`. The acceptance budget is at most
25% extra median user CPU or peak RSS, using three alternating pairs on each
control. Program outputs and inference evidence are compared separately from
expected executable-identity and invocation-path provenance changes.

## Rejected parent replay

The first child-request prototype suspended a parent at its first unavailable
child, replayed the parent from its immutable input after that child settled,
and retained all request/result snapshots. It preserved the three focused CLI
contracts, but failed the scale controls:

| Control                | User CPU |      Peak RSS | Result                |
| ---------------------- | -------: | ------------: | --------------------- |
| 1,024 callbacks        |  44.52 s |   518,284 KiB | 45-second wall cutoff |
| Cached fallback fanout |  88.33 s | 5,054,016 KiB | 45-second wall cutoff |

These are bounded diagnostic runs, not accepted paired measurements. The
optimized build is [bd662a62-db35-40bf-afa7-c04aade18417](https://app.buildbuddy.io/invocation/bd662a62-db35-40bf-afa7-c04aade18417).
Raw output is under `results/request-replay-smoke/` in the staged bundle.

A real `perf record` of the callback control is under
`profiles/request-replay-callback/`. Its flat report attributes about 28% of
sampled CPU to cloning binding-ID sets and 20% to cloning source-owner sets.
The recording command is retained in that directory's `command.sh`.

## Terminal transfers

Existing execution transitions consume settled snapshots and cannot update an
earlier program point. They can execute in dependency order on the common fact
context and retire completed payloads, retaining typed proof dependencies.
This eliminates parent replay and whole-state cloning. Invocation memo outcomes
remain shared facts until their last context reference retires.

The first terminal-transfer smoke returned the callback control to 0.20 seconds
and 56,996 KiB, and cached fallback to 2.74 seconds and 224,312 KiB. This
intermediate binary still copied accumulated outcomes and is not the final
acceptance measurement. Its optimized build is
[f8bb7d81-1e94-47e3-9440-eaed2877b538](https://app.buildbuddy.io/invocation/f8bb7d81-1e94-47e3-9440-eaed2877b538).
The combined port moves those outcomes as well. Its first paired measurement
passed output and Node comparisons, but cached fallback rose from 2.27 seconds
to 5.48 seconds median user CPU (+141.4%); median peak RSS rose 0.4%. The other
three controls stayed within budget. Raw results are under
`results/paired-runtime-final/`. This candidate was rejected.

The optimized `perf` profiles in
`profiles/cached-fallback-fanout-diagnostic-20261028T210348Z/` locate 22.29% of
inclusive cycles in copying `BTreeSet<RuntimeKey>`. Invocation memo reads
accumulated completed version keys in the current producer's read set; every
ephemeral request and result copied those keys and their operation paths.
`read_final` now carries immutable memo dependencies without installing live
watchers or premise keys. Growing facts retain ordinary watched reads.

The private Claude control also aborted with a stack overflow after four graph
outputs; the baseline completed analysis and rejected the configured assignment.
Execution state is now heap-owned throughout transfer calls so suspended Rust
frames retain a compact ownership slot. A neutral two-chunk fixture with
60-function call chains reproduces the old abort and now exits successfully.
The fixture is covered by `synchronous_precision_report_test`.

## Compact transfers

The optimized build is
[a8841792-1e87-4b08-b829-457e351bfce7](https://app.buildbuddy.io/invocation/a8841792-1e87-4b08-b829-457e351bfce7),
with binary SHA-256 `eba539992087a8f7f59738747632870b3ad502cfede647ad7a671e8b05dfd27b`.
Three alternating pairs per control pass the CPU and RSS budget:

| Control                | Median user CPU change | Median peak RSS change |
| ---------------------- | ---------------------: | ---------------------: |
| Cached fallback        |                +17.22% |                 +1.86% |
| 1,024 callbacks        |                +18.75% |                 +7.38% |
| Capture DAGs           |                +11.11% |                 +0.00% |
| Property-write reentry |                 +0.00% |                 +0.49% |

All 24 CLI runs exit zero. All 12 output pairs match except executable identity
and invocation-path provenance. All 12 source and emitted-entry Node controls
exit zero and print `7`. Raw samples and comparisons are under
`results/paired-compact-transfers/`. All 210 debundle targets pass in
[85642c40-b454-4d7e-b055-22f4e242bd30](https://app.buildbuddy.io/invocation/85642c40-b454-4d7e-b055-22f4e242bd30).
Exact-head hosted CI passes production Clippy and all 183 selected tests in
[3a719cd6-739b-5fb0-a1f4-19ef548d8253](https://app.buildbuddy.io/invocation/3a719cd6-739b-5fb0-a1f4-19ef548d8253).
The private Claude replay matches all 75 baseline graphs and 11 conflict reports
apart from executable provenance. Supplemental Tana parity remains pending.
