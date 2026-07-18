# E9 — DeepSeek-V4-Flash on wyrm2 via llama.cpp (IQ2), the resident-ceiling breaker

- **Status:** running — **CPU 1.1 tok/s, Vulkan (2×5090) 2.9 tok/s**, coherent. The
  reproducible wiring is in <build.sh> + <run.sh> (see "Repro").
- **Date:** 2026-07-18
- **Plan:** E8 follow-up — get DSV4-Flash (79 SWE, 13B active) _running_ on wyrm2,
  where vLLM couldn't fit it (E8) and KTransformers needs 256 GB RAM
  (<../ktransformers_dsv4_notes.md>).

## Goal

DeepSeek-V4-Flash is the model that beats both our resident ceiling (~69 SWE) and
GLM-5.2 (77.8): **79.0 SWE-bench Verified, 13B active / 284B MoE, 1M context** via
Compressed Sparse Attention. E8 proved the arch runs on sm_120 but the 80 GB W4A16
didn't fit vLLM. E9 gets it running via llama.cpp + a 2-bit GGUF that fits 96 GB.

## What worked

- **Model:** `unsloth/DeepSeek-V4-Flash-GGUF` **UD-IQ2_XXS** (2.0625 bpw, ~91 GB,
  3 split parts) — fits wyrm2's disk (91 GB alongside Colibri's 358 GB) and nearly
  page-caches in 96 GB RAM.
- **Runtime:** **mainline `ggml-org/llama.cpp`** (master `b10066`), built CPU-only
  via `nix shell nixpkgs#{cmake,gcc,gnumake}` + `-DGGML_CUDA=OFF`. Clean build.
- **Result:** loads with **no tensor mismatch**, generates **coherent, on-task**
  output (`[Start thinking] We need to write a Python function is_prime(n)…`).
  **CPU-only decode: 1.1 tok/s** (1.3 prompt).

  1.1 tok/s is the **slow floor** (pure CPU, model partly faulting from SSD) — yet
  it's already **~7× Colibri's 0.15** and inside the usable band, at _higher_ quality
  than anything resident. DSV4-Flash at 79 SWE / 1.1 tok/s / 1M ctx / tools is
  **strictly better than Devstral** (68 SWE) — it fills the empty
  above-Devstral gap in the 0.3–100 tok/s band of the speed×skill plot.

## The trap that cost the first attempt (branch ≠ GGUF)

DeepSeek-V4 has multiple competing llama.cpp implementations. First build used the
`nisparks/llama.cpp @ wip/deepseek-v4-support` branch (PR #22378, what loFT LLC
used) — it **failed with `missing tensor 'hc_head_base'`** because that WIP branch
predates the merged support and the unsloth GGUF expects the **Heavily Compressed
Attention** head. **Fix: use mainline**, which merged DSV4 support (am17an PR
#24162 + fairydreaming's lightning-indexer #24231, KQ-mask ops #25370). Lesson: the
GGUF and the runtime must be from the _same_ DSV4 implementation; unsloth GGUFs
target mainline.

## Vulkan (GPU) — 2.9 tok/s, no CUDA toolchain

`-DGGML_VULKAN=ON` GPU-accelerates on the 2×5090 while sidestepping the nix CUDA
fight (the CUDA build was blocked on nixpkgs' split-package `cuda_runtime.h`).
Config alone needed **every** Vulkan/SPIRV path passed explicitly (`Vulkan_LIBRARY`,
`Vulkan_INCLUDE_DIR`, `Vulkan_GLSLC_EXECUTABLE`, `CMAKE_PREFIX_PATH` for
spirv-headers/spirv-tools/glslang) and `CPATH` set to spirv-headers' include so the
compile finds `spirv/unified1/spirv.hpp` — all captured in <build.sh>.

Run (attention on GPU via `-ngl 999`, all experts on CPU via `--cpu-moe`, `-c 4096`
because the native-1M default KV OOMs the GPU): **2.9 tok/s decode**, coherent —
**~2.6× the CPU floor, ~10–19× Colibri**. Runtime wiring (NVIDIA ICD, loader
`LD_LIBRARY_PATH`) in <run.sh>.

## Next (optimization, not blocking)

- Move hot experts to the spare VRAM (`--n-cpu-moe N`, N < all layers) — decode is
  CPU-expert-bandwidth bound, so every expert layer moved to the 5090s should help.
- A proper coding-quality spot-check + tok/s at real context.

## Repro

```bash
# on wyrm2 (model at /var/lib/colibri/dsv4-iq2/):
./build.sh download   # ~91 GB GGUF (if not present)
./build.sh cpu        # or: ./build.sh vulkan
./run.sh vulkan       # or: ./run.sh cpu
```
