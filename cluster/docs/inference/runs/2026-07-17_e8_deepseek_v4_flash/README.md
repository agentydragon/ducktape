# E8 — DeepSeek-V4-Flash: the genuine native-1M model runs on sm_120 (vLLM)

- **Status:** done — informative result. The CSA arch **loads on sm_120**
  (unblocking E3), but the 80 GB W4A16 **does not fit** our 64 GB VRAM + 96 GB RAM
  budget: kernel-feasible, memory-infeasible.
- **Date:** 2026-07-17
- **Plan:** E3 follow-up — "can we run 1M of anything here?"

## Goal

E3 found Qwen2.5-1M **kernel-blocked** (dual-chunk attention needs flash-attn,
absent on sm_120) and named DeepSeek-V4-Flash as "the genuine 1M path": native 1M
via **Compressed Sparse Attention (CSA)**, not dual-chunk, with vLLM Day-0
support. E8 tests whether it actually runs on 2×5090.

## What runs (the E3-unblocking result)

vLLM 0.25.1 has **native `DeepseekV4ForCausalLM` support**, and every
Blackwell-relevant piece initializes on sm_120:

- `Defaulting to tokenizer_mode='deepseek_v4'` — arch recognized.
- `MarlinLinearKernel for AutoGPTQLinearMethod` + `'MARLIN' WNA16 MoE backend` —
  the **Intel W4A16 AutoRound** 4-bit quant loads via Marlin.
- `Using DeepSeek's fp8_ds_mla KV cache format` — the MLA KV layout (requires
  `--kv-cache-dtype=fp8`; asserts otherwise — a required flag, not optional).
- `Using FP8 indexer cache for Lightning Indexer` — the **Compressed Sparse
  Attention** machinery, via a FlashInfer sparse-attention path
  (`flashinfer_sparse`), which _is_ available on sm_120.

So unlike Qwen2.5-1M's dual-chunk attention (E3, kernel-blocked), DeepSeek-V4's
CSA has a working kernel path here. **The 1M-capable arch is not blocked on this
hardware.** Model choice matters: CSA ≠ DCA.

## The wall: fitting 80 GB in 64 GB VRAM + 96 GB RAM

We use the **Intel W4A16 AutoRound (~80 GB)** build — the official NVFP4 (~170 GB,
E3) far exceeds our ~140 GB usable budget. Even at 4-bit it's a knife-edge, because
the CSA Lightning Indexer + `fp8_ds_mla` KV buffers need VRAM _beyond_ the weight
shard, and UVA CPU-offload needs ~2× the offloaded size in pinned host RAM during
load:

| `--cpu-offload-gb`   | Result                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------ |
| 32 (16/GPU)          | **GPU OOM** at weight load — GPU0 28.7 GB used, ~1 GB short                                |
| 32 (16/GPU) + 8K ctx | **GPU OOM** — identical (28.7 GB); context doesn't help, the OOM is at weight-load, not KV |
| 38 (19/GPU)          | **host OOM** — container OOMKilled (exit 137)                                              |
| 48 (24/GPU)          | **host OOM** — container OOMKilled                                                         |

There is **no offload value that fits both**: the GPU needs ≥ ~36 GB offloaded to
seat the weight shard + fixed Marlin/CSA buffers (at 32 GB it's ~1 GB over, with
GPU0 physically full — display ~2.6 GB + weights 24 GB + framework ~4.7 GB ≈ the
32 GB card), but the host OOM-kills at ≥ 38 GB because UVA offload pins ~2× the
offloaded size during load. The ~36–38 GB window that might satisfy the GPU is
exactly where the host gives out.

## Verdict — kernel-feasible, memory-infeasible

DeepSeek-V4-Flash **is not kernel-blocked on 2×5090** — its Compressed Sparse
Attention has a working FlashInfer path on sm*120, unlike Qwen2.5-1M's dual-chunk
attention (E3). That resolves E3's open question: the \_arch* is fine; **model
choice matters (CSA ≠ DCA)**.

But it **doesn't fit our hardware**. The smallest vLLM-loadable quant (Intel W4A16
~80 GB) is caught between GPU OOM (needs more offload) and host OOM (can't sustain
the offload's load-time pinned staging). The official NVFP4 (~170 GB) is far
worse. So the genuine native-1M model remains **out of reach on 2×5090 + 96 GB
RAM** — now for a _memory_ reason, not a kernel one. A box with ~1×80 GB or
2×48 GB VRAM (per the community sizing) would run it; ours can't. No `results.md`
coding row (nothing served); recorded in the long-context table as arch-runs /
doesn't-fit.

## Notes

- Config: vLLM 0.25.1, TP2, `--kv-cache-dtype=fp8`, `--max-model-len=16384`,
  `gpu-mem-util 0.83`, deepseek_v3 tool parser + deepseek_r1 reasoning parser.
  Manifest: <deployment.yaml>.
- Each crash cleans the partial HF download, so every attempt re-pulls ~80 GB —
  the tuning loop is expensive.
