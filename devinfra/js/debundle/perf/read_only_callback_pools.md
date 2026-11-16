# Read-only callback pool summaries

Measured 2026-11-16 on Linux 6.18.55, AMD Ryzen 9 9950X3D. Both CLI binaries
were built with `bbr build -c opt //devinfra/js/debundle:debundle`. Baseline:
`bcdd9b4b23ad4ce635538e6622c2c2935ba2fac3`; candidate adds the read-only pool
summary described in <../docs/inference.md>. Executable SHA-256:

- Baseline: `3bbb49af57972aefaad662c980c0bea129076b36dde6cf606ae6ae691f6af01f`.
- Candidate: `0b758191a1dfb2571c4a0d5c672876a5feb05da243dc8cb4d5dc453005bc2c13`.

The generated public workload has `const state = 7`, `N` functions
`function readerI() { return state; }`, and sixteen calls to one imported
opaque host with all readers as arguments. The host sums their returns; the
entry logs the final result. Both versions run the complete CLI pipeline with
the same source and empty authored-module assignment, leaving statements inline.
Each run writes a fresh output directory. Five repetitions per size/version
alternate baseline/candidate order, with warm filesystem caches and
`RAYON_NUM_THREADS=1` for both binaries. Other host workloads were uncontrolled.

`perf stat -x, -e duration_time,task-clock,cycles,instructions` collected the
measurements. The host's `duration_time` counter returned zero and is excluded;
the table reports user task-clock milliseconds and retired user instructions,
not wall time. P90 is nearest-rank (the maximum with five samples). Sampling
profiles used `perf_wrapper.sh`; these short runs have too few samples for a
stable function-cost comparison.

| Callbacks | Baseline CPU p50 / p90 / mean (ms) | Candidate CPU p50 / p90 / mean (ms) | Baseline instructions p50 (M) | Candidate instructions p50 (M) |
| --------- | ---------------------------------- | ----------------------------------- | ----------------------------- | ------------------------------ |
| 32        | 96.70 / 143.06 / 105.38            | 77.96 / 93.07 / 79.15               | 383.77                        | 354.06                         |
| 64        | 109.93 / 120.98 / 109.01           | 92.99 / 147.14 / 102.40             | 497.72                        | 437.11                         |
| 65        | 100.01 / 155.31 / 106.43           | 94.45 / 101.63 / 94.22              | 426.67                        | 440.44                         |
| 128       | 109.07 / 120.27 / 112.19           | 95.00 / 107.96 / 97.94              | 586.37                        | 616.12                         |
| 256       | 157.90 / 161.52 / 157.18           | 146.69 / 169.11 / 147.10            | 945.03                        | 1005.27                        |
| 512       | 273.85 / 277.56 / 267.16           | 264.10 / 295.05 / 269.09            | 1789.47                       | 1932.53                        |

At 32/64 callbacks, median instruction counts fall by about 7.7%/12.2%.
Above the baseline's 64-member limit, its cheaper global fallback loses
precision; the candidate retains the read-only summary and executes 3.2–8.0%
more instructions. The noisy CPU samples do not establish a general speedup.
These small whole-CLI measurements do not isolate the callback transfer or
establish Excalidraw performance, peak memory, or asymptotic bounds. The phase
bound is proved separately in <../docs/inference.md>.

The behavioral suite uses the real CLI and Node, including 65/256-member pools,
TDZ rejections, changed prefix values, returned callables/accessors, and saved
analysis. On the baseline, the enhanced wide-pool checks report
`callback_pool_limit` at 65 members; runtime preservation controls still pass.
The candidate removes that loss for the covered family. This is no claim that
another extraction boundary or an unsupported callback family became safe.
