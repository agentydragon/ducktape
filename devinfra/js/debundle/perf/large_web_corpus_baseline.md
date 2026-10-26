# Large web-corpus extraction baseline

Source: public Debundle revision `c7168beeeb92e8ada12af187b8246002f20c771d`.
The optimized (`-c opt`) source build ran three fresh direct extractions against
the same staged inputs, writing to separate scratch roots. All three exited
successfully and emitted 7,482 files.

| Run | Wall time |      Peak RSS |
| --- | --------: | ------------: |
| 1   |  109.85 s | 3,938,136 KiB |
| 2   |  106.25 s | 3,935,140 KiB |
| 3   |  105.25 s | 3,934,960 KiB |

Median wall time was 106.25 s (mean 107.12 s). Mean peak RSS was 3,936,079 KiB
(about 3.75 GiB). A separate `perf_wrapper.sh` CPU profile succeeded with
`cycles:u`, 99 Hz sampling, DWARF callchains, about 19,000 samples, and no lost
samples.

The extracted tree matched the reference after normalizing scratch/source-root
paths: all 3,475 JavaScript files were byte-identical, and the only three raw
file differences were JSON report path fields. No files were missing or extra.

## Source-unit identity candidate

Public revision `a59dbdb696e20716d851852c53033b4ae62b027c` was built with
`-c opt` and run against the same staged source, spec, and package roots as the
baseline. Three fresh extractions succeeded:

| Run | Wall time |      Peak RSS |
| --- | --------: | ------------: |
| 1   |  111.10 s | 3,903,688 KiB |
| 2   |  102.58 s | 3,815,780 KiB |
| 3   |  105.02 s | 3,719,028 KiB |

The candidate median is 105.02 s, compared with the baseline's 106.25 s. These
runs do not establish a speedup; they show no repeatable slowdown in this
control. They are an intermediate refactor control, not evidence for later
combined heads.

Each run emitted the same 3,475 JavaScript paths. Of those, 3,389 files changed
only the generated provenance comment from owner IDs to source-unit IDs. After
normalizing that exact comment line, every JavaScript file is byte-identical to
the baseline. No JavaScript files are missing or extra. Report wire changes are
outside this JavaScript comparison.
