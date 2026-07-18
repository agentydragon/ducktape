# KTransformers + DeepSeek-V4-Flash — findings (future work, RAM-gated)

**Status (2026-07-18):** not run on wyrm2 — **memory-gated**, not kernel-gated.
Revisit when wyrm2 has **≥192–256 GB RAM** (or via the disk-tiered / IQ2-GGUF
paths below). This is the concrete workload that justifies a RAM upgrade.

## Why DeepSeek-V4-Flash is the target

The model that beats our resident ceiling _and_ GLM-5.2, with a shape that can be
fast:

- **79.0 SWE-bench Verified** (> GLM-5.2's 77.8, ≫ Qwen3.5's 69.2).
- **13B active / 284B total** MoE — decode cost tracks the 13B active, not 284B.
- **Compressed Sparse Attention** — KV-frugal, native 1M; the CSA kernels **run on
  sm_120** (E8 confirmed: Marlin W4A16 + fp8_ds_mla + FlashInfer Lightning Indexer
  all init on the 5090). Not kernel-blocked, unlike Qwen2.5-1M (E3).

## The KTransformers/SGLang path (validated, fast — on enough RAM)

DSV4-Flash runs through **SGLang + KT-Kernel** (CPU/GPU heterogeneous: MXFP4 routed
experts split between CPU `cpuinfer` and GPU `kt-num-gpu-experts`). Tutorial:
<https://github.com/kvcache-ai/ktransformers/blob/main/doc/en/DeepSeek-V4-Flash.md>
(cloned for reference at `/code/github.com/kvcache-ai/ktransformers`, v0.6.3).

- **RTX 5090 (SM*120) is a \_validated* arch** — triton_kernels for MXFP4 MoE, Triton
  fallback for NSA sparse-MLA. **20+ tok/s** single-GPU.
- Build: `kt-kernel/install.sh` + SGLang `install.sh`; pinned deps
  `transformers==4.57.1`, `flashinfer{,-cubin} ≥0.6.9`, `tilelang==0.1.8`,
  `apache-tvm-ffi<0.1.12`. Env: `FLASHINFER_CUDA_ARCH_LIST=12.0a`,
  `TORCH_CUDA_ARCH_LIST=12.0+PTX`, `SGLANG_DSV4_MODE=2604`.
- MTP NextN draft head available (EAGLE spec-decode) — but **net-negative on a
  single-GPU CPU-bandwidth-bound box** (it multiplies CPU expert reads); the 1.2×
  win is a multi-GPU result. Measure, don't assume.

## The wall on wyrm2 (96 GB RAM)

- **CPU experts floor at 4-bit.** `kt-kernel/scripts/convert_cpu_weights_ds4.py`
  offers only `int4`/`int8` (+ `moe_int4/8`) — **no 2-bit CPU path**. So the ~271B
  routed experts are **~135–144 GB** in RAM → tutorial mandates **≥256 GB RAM**.
- Maxing GPU experts on the 2×5090 (64 GB, 2× the tutorial's 32) only pulls CPU
  experts down to ~93–101 GB — right at/over 96 GB before OS/KV/overhead.
- **Storage:** 340 GB native weights collide with Colibri's 358 GB on the 500 GB
  disk — can't hold both.

## The reframe: disk offload is a continuum, not on/off

Decode tok/s above the resident ceiling ≈ set by the **fraction of per-token
active-expert bytes served from VRAM+RAM (fast, ~0.03–1.8 TB/s) vs SSD (~7 GB/s)**.
Colibri was slow because ~273 GB of experts sat on SSD; it wasn't "disk on." So:

- **Every GB saved is a GB that stops faulting from SSD** — memory savings and speed
  are the same lever.
- The knob is `(VRAM + RAM capacity) ÷ (model size at chosen quant)`. Push that
  ratio toward 1 and tok/s climbs smoothly from Colibri's 0.15 toward the ~20 of a
  fully-resident run.

## Paths to actually run DSV4-Flash on wyrm2 today

1. **llama.cpp IQ2 GGUF (~91 GB), GPU offload + CPU-MoE** — _the fit-on-96GB path,
   being pursued (E9)._ Concrete recipe:
   - **Branch:** `wip/deepseek-v4-support` (llama.cpp **PR #22378**) — the loFT LLC
     writeup built it for **sm_120** (CUDA 13, `BLACKWELL_NATIVE_FP4`) and got
     **35 tok/s** on DSV4-Flash — but on 2×96 GB VRAM (whole 146 GB FP4 GGUF on GPU,
     `-ngl 999`, ~0 CPU). Proves the branch works on Blackwell.
   - **wyrm2 adaptation (64 GB VRAM + 96 GB RAM):** use the smaller
     **`unsloth/DeepSeek-V4-Flash-GGUF` UD-IQ2_XXS (~91 GB)** (quality-safe floor;
     `UD-IQ1_S` ~82 GB is the more-aggressive, more-GPU-offload option). Offload
     attention/shared/KV + as many layers as fit to the 2×5090 (`-ngl` high), keep
     overflow MoE experts on CPU/RAM (`--n-cpu-moe N`). At ~91 GB it nearly fully
     page-caches in 96 GB RAM → SSD-hit fraction small; decode is then CPU-expert
     (9950X3D AVX-512) bound — expect single-to-low-double-digit tok/s (≫ Colibri's
     0.15), the memory=speed continuum in action.
   - **Disk:** 91 GB IQ2 + Colibri's 358 GB = 449 GB on the 500 GB disk → fits
     (unlike KT's 340 GB native, which would need Colibri deleted first).
   - **Quality:** IQ2 of a 79-SWE model — spot-check it holds ~≥74; drop to IQ2_M
     if IQ2_XXS degrades, IQ1 only if speed matters more than skill.
2. **RAM upgrade to 256 GB** — unlocks the full KT/SGLang path at 20+ tok/s. The
   clean fix; ~$200–500 of DDR5. This note is the justification.
3. **Disk-tiered KT** (if it supports partial SSD residency) — faster than Colibri
   (optimized kernels, more in RAM), between #1 and full-RAM. Not confirmed KT
   supports it; llama.cpp mmap gets tiering for free.
