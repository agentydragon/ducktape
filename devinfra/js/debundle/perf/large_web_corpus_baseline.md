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

## Combined refactor control

Public validation revision `c1270ac48858e2e80a4f02d73fe920e0ef0ce7f5` has the
same Git tree as PR #12095 head `ce18b232fef566b9b2fb82b17abe740ae602b400`.
It includes the merged identity, assignment, entry-destination, reconstruction,
and authoring changes. Its optimized build passed in invocation
`f3c5f64d-35e8-4b7d-8736-0a0325a4813b`; all 175 public tests, including packaged
skill checks, passed in `eca81922-0918-4a16-9c6b-7479797f15c6`.

The same frozen baseline executable and staged inputs were run again immediately
before three candidate extractions:

| Executable         | Wall time |      Peak RSS |
| ------------------ | --------: | ------------: |
| Baseline reference |  121.37 s | 3,635,220 KiB |
| Candidate 1        |  120.82 s | 3,896,684 KiB |
| Candidate 2        |  119.59 s | 3,900,412 KiB |
| Candidate 3        |  119.32 s | 3,899,680 KiB |

Candidate median wall time is 119.59 s. The baseline's fresh 121.37 s reference
also differs from its earlier 106.25 s median, so comparisons across these
batches cannot establish a causal speed change. No wall-time slowdown appears
against the contemporaneous reference. Candidate peak RSS is below the earlier
baseline measurements; the single fresh baseline reference used less memory.

Every extraction succeeded. The fresh baseline reproduces all 3,475 original
JavaScript files byte for byte. Each candidate has the same JavaScript paths;
3,389 files differ only in the exact generated provenance comment described
above. After normalizing that line, all JavaScript is byte-identical. No
JavaScript files are missing or extra.
