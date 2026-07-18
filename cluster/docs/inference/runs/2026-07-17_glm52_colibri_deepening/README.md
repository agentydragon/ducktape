# 2026-07-17 — GLM-5.2 Colibri deepening: warm steady-state + 64K allocation

Follow-up to <../2026-07-14_glm52_colibri/README.md> (best result 0.28 tok/s).
Two questions: (1) what is the **warm-cache steady-state** decode over a longer
generation (64 tokens, vs the baseline's 32)? (2) does a **64K context
allocation** slow decode?

Runs the same `run_one` env contract as the baseline bundle, via <deepen.sh>, on
wyrm2 after the baseline `setup.sh` rebuilds `c/glm`. Per-run `coli` logs are left
under `/var/lib/colibri/deepen-<ts>/` on wyrm2 (`*.log` is gitignored under
`cluster/`); the numbers below are the per-run summary lines.

## Results (64-token generations, ctx as noted)

| Run         | ctx | tok/s    | expert hit | expert-disk | expert-matmul | "other" |
| ----------- | --- | -------- | ---------- | ----------- | ------------- | ------- |
| cold-4k     | 4K  | 0.09     | 54.6%      | 149.9 s     | 331.3 s       | 391.4 s |
| profiled-4k | 4K  | **0.16** | 82.5%      | 56.0 s      | 90.5 s        | 259.6 s |
| warm-4k     | 4K  | 0.15     | 76.6%      | 70.1 s      | 105.1 s       | 289.7 s |
| warm-64k    | 64K | **0.15** | 76.6%      | 69.6 s      | 105.6 s       | 293.6 s |

## Findings

### 1. 64K context allocation is decode-neutral

`warm-64k` (0.15 tok/s) is **identical to `warm-4k`** (0.15). The 64K window
reserved 24.4 GB RAM for KV (`KV 1x65536 11.9, kvb 7.5`) vs 6.1 GB at 4K, but the
VRAM expert hot tier was unchanged (**2644 resident experts, 50 GB** in both), so
decode speed did not move. Decode here is **expert-I/O bound, not KV bound** —
each token streams 731 experts/token regardless of context size. The context
window is essentially free on the decode axis; the wall is expert bandwidth.

**Caveat:** this is a 64K _allocation_ with a short prompt, not 64K of _filled_
context. Filling 64K would be prefill-dominated (streaming the whole expert set
per prefill pass) and is a separate, far slower experiment — not run here.

### 2. Warm steady-state ≈ 0.15–0.16 tok/s, and it does not climb

Over 64 tokens the rate is flat (`0.14→0.15` across `t=16…64`) and the expert hit
rate stabilizes ~77%. Warming helps most going **cold→profiled** (0.09→0.16, hit
55%→83%); the extra "refined warm" round did not improve further (it actually
dipped to 76.6% hit / 0.15). So the practical warm ceiling on this config is
~0.16 tok/s.

### 3. Regression vs the 2026-07-14 baseline (~1.8× slower)

Every stage is markedly slower than Jul-14 (cold 0.17→0.09, profiled 0.26→0.16,
warm 0.28→0.15 tok/s). The extra time is in the framework **"other"** bucket
(260–390 s of a ~400–745 s run), not disk or matmul. Between the two runs wyrm2
had a **NixOS switch** that bumped the driver to **595.71.05** (CUDA 13.2), and
the baseline's `setup.sh` `cuda-test` step now fails against the nix **stub
libcuda** (`CUDA driver version is insufficient for CUDA runtime version`) — we
built `c/glm` directly, skipping that check, and it runs, but the ~1.8×
slowdown concentrated in non-compute overhead is consistent with a driver/runtime
regression from that change. Not root-caused (hobbyist scale); flagged for anyone
re-running Colibri here.

## Verdict

GLM-5.2 on Colibri is still a **storage-bound curiosity, not a usable tier**:
~0.15 tok/s warm (below the 0.5 tok/s LiteLLM gate, and now further below it than
in July). The useful _positive_ result is architectural: **long context is nearly
free** on a disk-streamed expert model — the 64K window costs RAM but not decode
speed, because expert I/O dominates. If Colibri is ever worth revisiting, the
lever is expert bandwidth (faster SSD, bigger warm tier, better routing-cache hit
rate), not the context window.
