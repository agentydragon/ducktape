# Ollama Benchmark Report

**Date**: 2026-02-24
**Hardware**: 2x NVIDIA RTX 5090 (32 GiB each, 64 GiB total), 8 CPU cores, 28 GiB RAM
**Node**: `talos-pve-gpu-worker-0` (Proxmox VM)
**Software**: Ollama with LiteLLM proxy at `litellm.allegedly.works`
**Config**: `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`

## Models

| Model          | Parameters | Quantization | Size on disk | GPU/CPU split    | Layers (GPU/total) |
| -------------- | ---------- | ------------ | ------------ | ---------------- | ------------------ |
| `gpt-oss:20b`  | ~20B       | unknown      | 13 GB        | 100% GPU         | 25/25 (single GPU) |
| `gpt-oss:120b` | 116.8B     | MXFP4        | 65 GB        | 91% GPU / 9% CPU | 34/37              |

## GPU Memory Breakdown (120b, `num_ctx=131072`)

| Component     | GPU0         | GPU1         | CPU         | Total        |
| ------------- | ------------ | ------------ | ----------- | ------------ |
| Model weights | 27.7 GiB     | 27.7 GiB     | **5.4 GiB** | 60.8 GiB     |
| KV cache      | 1.1 GiB      | 1.2 GiB      | 0.1 GiB     | 2.4 GiB      |
| Compute graph | 1.2 GiB      | 0.5 GiB      | ~0          | 1.7 GiB      |
| **Total**     | **30.0 GiB** | **29.4 GiB** | **5.5 GiB** | **65.1 GiB** |

3 of 37 layers + the output head live on CPU. Each GPU is ~2 GiB from full.
KV cache is small (~2.4 GiB) due to GQA (8 KV heads). The bottleneck for 100%
GPU offload is model weight size, not KV cache.

## Results: `OLLAMA_NUM_CTX=131072` (correct setting)

| Metric       | 20b     | 120b    | Unit         |
| ------------ | ------- | ------- | ------------ |
| decode       | 228     | 10.4    | output tok/s |
| prefill 1k   | 1,555   | 41      | input tok/s  |
| prefill 4k   | 1,509   | 111     | input tok/s  |
| prefill 16k  | 1,012   | 588     | input tok/s  |
| prefill 32k  | 1,262   | 504     | input tok/s  |
| prefill 64k  | 2,299   | 1,117   | input tok/s  |
| prefill 128k | 2,110   | 1,456   | input tok/s  |
| NIAH 1k      | 10/10   | 5/5     |
| NIAH 4k      | 10/10   | 6/6     |
| NIAH 16k     | 10/10   | 5/5     |
| NIAH 32k     | 9/10    | 5/5     |
| NIAH 64k     | 8/8     | 3/3     |
| NIAH 128k    | **1/3** | **0/2** |
| NIAH 256k    | **0/1** | —       |

20b output is 22x faster (single GPU, fully offloaded vs 9% CPU offload bottleneck).
20b input is flat (~1-2.3k t/s); 120b starts 38x slower at 1k but narrows to ~1.5x at
128k (prefill is compute-bound, less affected by CPU-resident layers than decode).

Both models hit a NIAH wall at 128k: `num_ctx_for(128000)=147712` exceeds
`OLLAMA_NUM_CTX=131072`, so Ollama truncates from the front. The 20b's single pass at
128k (depth=0.64) survived because the needle was past the truncation point. The 120b's
128k failures produced empty responses after 230-249s of reasoning.

## Results: `OLLAMA_NUM_CTX=1048576` (broken setting)

Setting `OLLAMA_NUM_CTX` to 1M caused severe performance regression:

| Symptom      | 20b                                | 120b                |
| ------------ | ---------------------------------- | ------------------- |
| Output speed | **N/A** (no streaming tokens)      | N/A                 |
| Input 1k     | 4,003 t/s                          | 52 t/s              |
| Input 64k    | **1,055 t/s** (14x cliff from 32k) | 1,274 t/s           |
| Input 128k+  | ~1.2-1.5k t/s                      | ~1.5k t/s           |
| NIAH 32k+    | **0%** (20b)                       | passes through 256k |

The 20b model was hit hardest — a throughput cliff at 64k and complete NIAH failure
at 32k+. The 120b model handled it better (7/9 NIAH through 256k) but both converged
to ~1.5k t/s at large contexts.

**Root cause**: `OLLAMA_NUM_CTX` controls KV cache preallocation. Setting it to 1M
preallocates a 1M-token KV cache regardless of the per-request `num_ctx` override,
consuming VRAM that should hold model weights. This forces more layers to CPU,
destroying throughput.

## Key Findings

### 1. `OLLAMA_NUM_CTX` controls KV cache preallocation, not just truncation

A previous claim that this env var "just controls truncation" was wrong. Setting it
to 1M caused the 20b model's output speed to go from 228 t/s to N/A. The KV cache
preallocated at 1M tokens consumed ~16 GiB of VRAM on the 20b model (which only
needs 13 GB for weights), likely forcing partial CPU offload.

### 2. Per-request `num_ctx` triggers model reloads

The benchmark passes `extra_body.options.num_ctx` sized to each prompt. Changing
`num_ctx` between requests triggers Ollama to unload and reload the model with a
new KV cache size. This takes 10-40s and shows up as extreme variance in input
speed measurements. The updated benchmark code adds explicit per-size prewarm
requests to isolate this cost.

### 3. 120b is ~22x slower than 20b

Output: 10.4 vs 228 t/s. Input at 1k: 41 vs 1,555 t/s. The 120b model has 3/37
layers on CPU due to not fitting in 64 GiB VRAM, creating a memory bandwidth
bottleneck on every forward pass.

### 4. 120b needs ~5.4 GiB more VRAM for 100% GPU

The model weights spill 5.4 GiB to CPU. Each GPU has ~2 GiB free. Options to close
the gap:

| Option                                          | Savings  | Trade-off                           |
| ----------------------------------------------- | -------- | ----------------------------------- |
| `OLLAMA_KV_CACHE_TYPE=q4_0`                     | ~1.2 GiB | Minor quality loss at long contexts |
| Reduce `OLLAMA_NUM_CTX` to 65536                | ~1.2 GiB | Halves max context                  |
| Both combined                                   | ~2.4 GiB | Might just barely fit               |
| Wait for Ollama tensor parallelism improvements | N/A      | May improve GPU utilization         |

**Note**: The model uses MXFP4 quantization (~4.5 bits/weight), which is already
aggressive. gpt-oss is a Mixture-of-Experts model (128 experts, top-4 per token) where
~90% of parameters are MoE FFN weights natively trained at MXFP4. These weights don't
compress further — the total model size ranges only 2.8 GB across all quantization
levels (62.6 GB at Q2_K to 65.4 GB at BF16). There is no "slightly smaller quant"
available.

### 5. Reasoning model overhead dominates NIAH latency

Even at 1k context (trivial prompt size), each NIAH sample takes 50-75s for the
120b model. The model generates thousands of thinking tokens before answering a
simple retrieval question. At 10 t/s output speed, 5k thinking tokens = 500s.
This means NIAH benchmarks for the 120b model are bottlenecked by decode speed,
not prefill speed.

### 6. Harbor proxy cache is broken (unrelated but discovered during benchmarking)

Image pulls during the benchmark revealed Harbor's pull-through proxy returns 401
from upstream registries. Containerd falls back to direct pulls. Root cause: missing
`overridePath = true` in Talos `hosts.toml` mirror entries, causing double `/v2/`
in request URIs. See `docs/troubleshooting/harbor-proxy-cache-401.md`.

## Benchmark Methodology

- **Output speed**: Short seed prompt + `max_tokens=128`, streaming. Rate = `(tokens - 1) / (last_chunk_ts - first_chunk_ts)`.
- **Input speed**: Filler prompt of ~N tokens + `max_tokens=1`, non-streaming. Rate = `prompt_tokens / wall_clock`.
- **NIAH**: War and Peace haystack with 8-char hex needle at evenly spaced depths. Streamed response scored by needle presence.
- **Prewarm**: One throwaway request per context size to pay KV cache reallocation cost.
- **Per-request `num_ctx`**: `int(target_tokens * 1.15 + 512)` via `extra_body.options.num_ctx`.
- **JSONL logs**: All NIAH samples logged to `hack/benchmark_ollama/benchmark_*.jsonl`.

## Raw Data

- `results_num_ctx_1048576.md` — Full tables for both models at `NUM_CTX=1048576`
- `results_num_ctx_131072.md` — Full tables for both models at `NUM_CTX=131072`
- `benchmark_*.jsonl` — Raw NIAH sample data with full response text
