# Indexed prefix reads

The synchronous interpreter's `unsettle` walk repeatedly projects callable reads
onto the current module's prefix values. A cycles profile after flat `Values`
storage found 14.55% inclusive cost in this walk and 6.32% in borrowed binding-ID
hash insertion. These regions overlap. The profile contains 3,325 samples with
zero lost samples; incomplete optimized call stacks limit inclusive attribution.

Precompute each callable's top-level read ordinals against the existing shared
`TopBindings` table. Preserve binding order, visit each prefix binding once with
a fresh boolean vector per walk, and use those ordinals for prefix-value lookup.
Every distinct closure still expands its own retained captures and lexical cells.
No state or value expansion is reused between walks.

## Fresh pipeline comparison

Source tested at `3d1f1a9cf8193829212dcaea72ddd9d8aa7cb865`, parent
`935ccd0d48fefd8932f95a7e4bd782e8fd283dfe`. The baseline executable trio was
built at `4a9fd02c57076ef153aa0cd449c889a1020a5082`; its entire Debundle tree and
Rust dependencies/configuration match the parent. Both trios use RBE Linux opt
exec configuration. The intervening source differences concern Agentplane,
cluster configuration and Nix artifact pins.

Use the frozen workload recorded in
[the pipeline investigation](2026_11_14_excalidraw_pipeline_performance.md):
79 chunks, 118 emitted JS files, source/spec manifest SHA-256
`8154a3c4ce085b8cd0279f9473a56fa6ca84c622749bdf810781daa3220abfc3`.
Three alternating pairs each run fresh projection, analysis and full application;
no saved analysis is reused between runs. `perf stat` records user instructions
and cycles; GNU time records wall, CPU and peak RSS. Hardware, thread settings,
input files and report configuration are unchanged.

| Median                 |          Parent |   Indexed reads |
| ---------------------- | --------------: | --------------: |
| Projection wall        |          0.57 s |          0.63 s |
| Analysis wall          |         14.82 s |         11.82 s |
| Analysis CPU           |         22.47 s |         19.44 s |
| Analysis instructions  | 183.152 billion | 158.495 billion |
| Analysis cycles        | 106.322 billion |  91.341 billion |
| Analysis peak RSS      |     850,712 KiB |     850,960 KiB |
| Application wall       |          3.60 s |          3.71 s |
| Complete pipeline wall |         19.24 s |         16.18 s |

Analysis instructions decrease 13.41-13.49% in every pair. Analysis cycles, CPU
time and wall time decrease in every pair. Complete pipeline ranges are
18.09-23.24 s and 15.95-17.65 s. Host contention affects the measurements: the
first baseline projection takes 1.93 s wall versus 1.07 s CPU, and application
takes 6.49 s wall versus 4.51 s CPU. These are observed comparisons, not a
guaranteed latency or a completed 3x improvement.

All six runs preserve projection bytes, all 118 emitted JS hashes and every
report JSON after replacing only the output-root path. The frozen manifest
remains unchanged. Eleven suites pass in exact-source RBE invocation
[dd9fa856-989d-4416-af1f-db65c6804d66](https://app.buildbuddy.io/invocation/dd9fa856-989d-4416-af1f-db65c6804d66):
eight execute; three Excalidraw suites hit the cache after fresh candidate
analysis/application produce identical test-input artifacts. New real CLI tests
cover distinct captures behind one callable, accepted output under Node, rejected
TDZ-changing splits, and a callback installed between two opaque calls.

A preceding per-callable hash-set deduplication experiment is held: it preserves
all outputs but increases analysis instructions about 0.65-0.69%. It is absent
from this change.

Raw profiles, commands, counters and parity checks are under
`/tmp/agents/2026_11_14/excalidraw-speed/{flat-values-analysis-profile,indexed-prefix-reads,indexed-prefix-pairs}`.
