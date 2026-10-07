# Benchmark Results: OLLAMA_NUM_CTX=131072

**Date**: 2026-02-24 **Hardware**: 2x NVIDIA RTX 5090 (32 GB each), 28 GB system RAM, 8 CPU threads **Ollama**:
OLLAMA_KV_CACHE_TYPE=q8_0, OLLAMA_FLASH_ATTENTION=1, OLLAMA_NUM_CTX=131072 **Benchmark**: 300s/config, 10 NIAH samples
(evenly spaced depths 0.0–1.0)

## Model Info

| Model        | Parameters | Quantization | Size                 | GPU Utilization  | Layer Split                                       |
| ------------ | ---------- | ------------ | -------------------- | ---------------- | ------------------------------------------------- |
| gpt-oss:20b  | 20B        | —            | 13 GB                | 100% GPU         | Single GPU                                        |
| gpt-oss:120b | 116.8B     | MXFP4        | 65 GB (69 GB loaded) | 91% GPU / 9% CPU | GPU0: 13 layers (0..12), GPU1: 12 layers (13..24) |

## 20b Results (gpt-oss-20b-128k)

| Metric     | Value                      |
| ---------- | -------------------------- |
| output     | 227.6 ± 33.2 t/s (n=4)     |
| input 1k   | 1554.9 ± 1683.5 t/s (n=10) |
| input 4k   | 1509.1 ± 3807.4 t/s (n=7)  |
| input 16k  | 1011.5 ± 1939.3 t/s (n=7)  |
| input 32k  | 1261.7 ± 1716.3 t/s (n=7)  |
| input 64k  | 2299.4 ± 2146.6 t/s (n=7)  |
| input 128k | 2109.9 ± 590.0 t/s (n=5)   |
| input 256k | 1589.8 ± 165.4 t/s (n=4)   |

High variance in input speed is due to KV cache reallocation: each context size uses a different `num_ctx`, triggering
Ollama to reload the model (~10-40s) before the first timed sample. One slow sample pulls the mean down and inflates
stdev.

### 20b NIAH Recall

| Context | Recall | Notes                                     |
| ------- | ------ | ----------------------------------------- |
| 1k      | 10/10  | All 0.8–1.4s                              |
| 4k      | 10/10  | All 1.4–2.3s                              |
| 16k     | 10/10  | 3.4–60s (first sample slow, rest fast)    |
| 32k     | 9/10   | 5.8–70s; 1 miss at depth=0.81             |
| 64k     | 8/8    | 28–88s                                    |
| 128k    | 1/3    | Pass at depth=0.64, fail at 0.36 and 0.37 |
| 256k    | 0/1    | Fail                                      |

## 120b Results (gpt-oss-120b-128k)

| Metric     | Value                    |
| ---------- | ------------------------ |
| output     | 10.4 ± 0.5 t/s (n=3)     |
| input 1k   | 40.6 ± 65.0 t/s (n=5)    |
| input 4k   | 110.7 ± 99.7 t/s (n=6)   |
| input 16k  | 588.3 ± 798.5 t/s (n=6)  |
| input 32k  | 504.1 ± 113.3 t/s (n=5)  |
| input 64k  | 1117.4 ± 489.5 t/s (n=5) |
| input 128k | 1456.2 ± 187.0 t/s (n=4) |

### 120b NIAH Recall

| Context | Recall | Notes                                       |
| ------- | ------ | ------------------------------------------- |
| 1k      | 5/5    | 50–75s per sample (reasoning overhead)      |
| 4k      | 6/6    | 51–65s                                      |
| 16k     | 5/5    | 30–84s                                      |
| 32k     | 5/5    | 43–95s                                      |
| 64k     | 3/3    | 87–170s                                     |
| 128k    | 0/2    | Empty responses after 230–249s of reasoning |

## Analysis

### NIAH Context Limit

Both models hit a wall at 128k. The benchmark requests `num_ctx = int(128000 * 1.15 + 512) = 147712`, which exceeds
`OLLAMA_NUM_CTX=131072`. Ollama appears to cap the effective context at the server-side limit, truncating the beginning
of the prompt. Needles placed at the start (depths 0.00–0.37) are lost.

The 20b model's single pass at depth=0.64 (128k) confirms this: the needle was in the latter half of the prompt and
survived truncation.

### Speed Comparison

The 120b model is ~22x slower at output (10.4 vs 228 t/s) and runs 9% on CPU, which bottlenecks throughput. The 120b
input speed improves significantly at larger contexts (40 → 1456 t/s from 1k to 128k), likely because the KV cache
reallocation cost is amortized over more tokens.

### KV Cache Reallocation Noise

Input throughput has enormous variance (stdev > mean in several cases). This is caused by the benchmark sending
different `num_ctx` values per context size, triggering Ollama to reload the model with a new KV cache allocation. The
first sample after a reload includes the reload cost (~10-40s) while subsequent samples run at native speed.

A future improvement would be to warm up at each new `num_ctx` before starting timed measurements (this benchmark
already does this via `prewarm`, but the first timed input sample sometimes still catches a reload).

### 120b GPU Fit

The 120b model is 91% GPU / 9% CPU (69 GB total, 64 GB VRAM). The 9% CPU portion bottlenecks decode speed. Options to
fit 100% in GPU:

1. **Switch KV cache from q8_0 to q4_0**: Saves ~8 GiB at 128k context. Minimal quality impact.
2. **Reduce context**: 64k instead of 128k halves KV cache (~8 GiB saved).
3. **Both**: Would give ample headroom.

The model itself (MXFP4, 65 GB) is already aggressively quantized — there is no lower quantization available for this
specific model format.
