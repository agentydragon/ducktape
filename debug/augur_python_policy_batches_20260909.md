# Python spending-policy prototype: profiling evidence

Investigation of authoring, monthly transfer and capture costs; no policy API or
executor-language decision is implied. Reproduce with
<../finance/augur/x/bounded_spending/README.md#profiling-the-concrete-prototype>.

## Workload and measurement

Source: `45372383603f8da80b2f1b8ecb8ad7d17ee2aba5`. Each row below is one fresh
Linux RBE process, optimized Rust (`-c opt`), seven reported logical CPUs and four
Rayon threads. Sixty months, 1,000 or 100,000 paths; no warm-repeat distribution or
tail latency was measured. The first batch/1,000 run used the same source as an
uncommitted RBE patch; subsequent rows used the published commit.

Paths are three repeated stipulated equity-price paths: 100 to 200, 50 or 130 at
month 12; CPI changes from 1 to 1.25. No sampler, fit window or probability estimate.
Equity-only portfolio, no taxes, zero cash band, cashflow-only funding, purchases
disabled; no bond construction. Annual spending is 400 basis points of cash plus
public holdings, bounded by a 1,000-basis-point cut and 500-basis-point raise from
the CPI-adjusted prior decision. The retained input files pin all other values.

cProfile covers fresh policy/session creation through decoded final output.
Compilation and input-file creation are outside its timing. Both Python modes
execute Rust financial mechanics. The native CLI mode includes process startup,
execution, output-file I/O and final Python JSON decoding. cProfile changes the
cost of Python calls: these are instrumented workflow comparisons, not isolated
language or unprofiled engine benchmarks.

## Observed totals

Compact capture requests the same consumption arrays and product metrics in every
authoring mode. All paths reach the horizon, so each row observes `N × 60` months.

| Authoring      |   Paths | Capture  | Profiled seconds | Path-months/s | Self RSS KiB | Child RSS KiB |
| -------------- | ------: | -------- | ---------------: | ------------: | -----------: | ------------: |
| Native CLI     |   1,000 | Compact  |            0.091 |       658,549 |      235,812 |       235,608 |
| Scalar adapter |   1,000 | Compact  |            0.311 |       193,159 |      243,796 |         2,384 |
| Batch policy   |   1,000 | Compact  |            0.179 |       334,945 |      244,668 |         2,488 |
| Native CLI     | 100,000 | Compact  |            6.910 |       868,285 |    1,260,204 |     1,260,204 |
| Scalar adapter | 100,000 | Compact  |           38.278 |       156,749 |    1,962,864 |         2,380 |
| Batch policy   | 100,000 | Compact  |           30.377 |       197,521 |    1,908,304 |         2,416 |
| Batch policy   |   1,000 | Forensic |            2.022 |        29,668 |      801,984 |         2,416 |

RSS is a process high-water mark including preparation, not an isolated evaluator
allocation measurement. Child RSS can include the fork/pre-exec inherited parent
image. Self and child maxima are not additive simultaneous memory; in particular,
the equal native/100,000 values do not establish Rust's isolated memory demand.

### Where cProfile attributes the time

At 100,000 paths, batch mode spends 17.837 seconds in 60 native `advance` calls,
4.915 in 61 native `observe` calls, 0.820 constructing Python observation columns,
0.432 in the batch-authored rule, 1.724 in native `finish_json`, and 3.039 decoding
the final JSON. These are cumulative call times, not an additive partition of
nested functions. `advance` includes extraction, scheduling and financial work;
cProfile does not separate those components.

Scalar mode spends 10.228 seconds in the adapter, including 3.408 constructing
six million scalar observations and 2.217 in six million scalar policy calls.
Native `advance` is 16.676 seconds, `observe` 4.342, `finish_json` 1.634 and final
JSON decoding 3.017. This control shows a material row-dispatch cost which batch
authoring avoids; it does not establish that object arrays are the final fast path.

The 100,000-path native CLI profile attributes 3.871 seconds to subprocess wait
(startup, execution and output writing), 2.879 to JSON decoding, and 0.150 to
reading the output file. The 3.871 seconds is not pure financial-compute time.
Its full-horizon traversal differs from retaining every path and advancing all of
them month by month, despite sharing financial kernels.

Forensic capture at 1,000 paths spends 1.246 seconds decoding JSON, 0.406 in
`finish_json`, and 0.264 in `advance`. Capture/serialization dominates this small
trace workload; comparing it with native compact capture would confound the work.

## Exact input/output agreement and retained artifacts

Inputs are byte-identical across authoring modes at each size. Decoded compact
outputs, serialized with sorted keys, have identical hashes across all three modes.

| Paths   | Input bytes | Input SHA-256                                                      | Compact output SHA-256                                             |
| ------- | ----------: | ------------------------------------------------------------------ | ------------------------------------------------------------------ |
| 1,000   |   1,144,375 | `93b03920d1235329594bb18cbc9d21ca9317ac96ad0ad8db6f334eedaa526297` | `ac94459cf93354b2029e6719c6eacbad4e793213a8e9e856c72d1981bad49b3a` |
| 100,000 | 114,268,377 | `2434094f72db16276d59010d7f2d1f53cf6a3153211b62f75b5dca8343a27ce7` | `e66e25866cd369ffd3213367e42260a371c5e2e8a5d3b31e70b533d914c8495a` |

Forensic output has a different schema and its own hash:
`11cb1c53bc88fbb372919d206a65930244a05080c43d4464a6193aa1f7536a1f`.
Full-output parity tests separately compare native and Python forensic paths.

Each runner retains `input.json`, `execution.prof` and `report.json` below its
`command-0/` artifact directory; native rows also retain `native.json`. Runner
logs include the complete report and cumulative profile. These links identify the
runner (program output), not just its Bazel build child:

| Workload                   | Runner evidence                                                                                 | Artifact subdirectory        |
| -------------------------- | ----------------------------------------------------------------------------------------------- | ---------------------------- |
| Batch / 1,000 / compact    | [Report and profile](https://app.buildbuddy.io/invocation/7fe6bc0a-4544-48b0-9187-eee0c7b8b72a) | `p6-batch-1000x60-r1`        |
| Scalar / 1,000 / compact   | [Report and profile](https://app.buildbuddy.io/invocation/22583000-157d-458d-b599-0b79a1162a51) | `p6-scalar-1000x60-r1`       |
| Native / 1,000 / compact   | [Report and profile](https://app.buildbuddy.io/invocation/ef880310-9739-4a63-872a-49f39404a786) | `p6-native-1000x60-r1`       |
| Batch / 100,000 / compact  | [Report and profile](https://app.buildbuddy.io/invocation/c4245a77-e4a8-49c7-b186-1261f067aeaa) | `p6-batch-100000x60-r1`      |
| Scalar / 100,000 / compact | [Report and profile](https://app.buildbuddy.io/invocation/624888e1-6e89-4f7b-9fbe-23616f1082bd) | `p6-scalar-100000x60-r1`     |
| Native / 100,000 / compact | [Report and profile](https://app.buildbuddy.io/invocation/5888062a-65e9-40f7-8b14-8d8618bf799e) | `p6-native-cli-100000x60-r1` |
| Batch / 1,000 / forensic   | [Report and profile](https://app.buildbuddy.io/invocation/916f9e13-a47f-47bb-96e1-a2b57b69be9e) | `p6-forensic-1000x60-r1`     |

## What remains to decide

The notebook-editable scalar and batch forms both reproduce this control. The
batch implementation reduces Python row dispatch here; native full-horizon
execution remains faster in this measurement. Neither establishes executor
language as the cause: traversal, retained working set, transfer, capture and
profiler overhead also differ.

Before selecting a supported authoring/transfer surface, inspect native hot spots
inside monthly advance, test less-copying observation/output representations, and
repeat matched workloads with realistic taxable multi-asset and variable-event
shapes. Notebook ergonomics, repeated-run variation and callback tails remain
unmeasured. No acceptable throughput or memory budget has been agreed, and this
tax-free five-year control cannot set one for the user's retirement studies.
