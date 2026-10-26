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
