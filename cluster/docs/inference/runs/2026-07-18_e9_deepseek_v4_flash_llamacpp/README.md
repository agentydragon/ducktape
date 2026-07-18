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

## Next — how to run it faster (ranked; not started, see blocker)

**Why there's headroom:** at IQ2 (2.06 bpw) with ~13B active/token, decode reads
**~3.35 GB of expert weight per token**. wyrm2 RAM (dual-channel DDR5 ≈ ~90 GB/s) caps
that at **~25–28 tok/s**; a 5090's VRAM (~1.8 TB/s) is ~20× faster per byte. We measured
**2.9 tok/s — ~10× under even the RAM ceiling** — so we are _not_ bandwidth-bound. Two
things are eating the gap: (a) the 91 GB model does not fully fit 96 GB RAM, so part
faults from SSD every token (this is the 1.1 CPU-only floor), and (b) Vulkan/CPU MoE
kernels are slow. The wins below attack both and stack.

1. **Fill VRAM with experts — `--n-cpu-moe N` (biggest, one flag).** E9 keeps _all_
   experts on CPU (`--cpu-moe`). ~55 GB of VRAM is free across the two 5090s after
   attention/KV at `-c 4096`; push that much expert weight onto the GPUs (`--n-cpu-moe`
   = layers kept on CPU; sweep it _down_ from all-on-CPU until VRAM is full). Double win:
   those experts now stream at 1.8 TB/s, **and** the CPU remainder shrinks to ~36 GB so
   it fits RAM comfortably — killing the SSD-faulting floor. Expect a multiple, not a few %.
2. **Runtime bake-off: `ik_llama.cpp` and a CUDA build.** ikawrakow's fork has much
   faster CPU/hybrid-MoE kernels for DeepSeek-class offload (commonly 2–3× decode), same
   GGUF — verify it has `deepseek4` (CSA/HCA) support merged. And build the **CUDA**
   backend instead of Vulkan (E9 only used Vulkan because the nix CUDA build was blocked
   on the split-package `cuda_runtime.h`; fix that or use a CUDA container) — CUDA's
   Blackwell MoE kernels beat Vulkan and compose with #1.
3. **Speculative decoding via DSV4 MTP — UNVERIFIED, check first.** DeepSeek-V4 ships
   MTP (multi-token-prediction) heads and the GLM-5.2 Colibri run already used INT8-MTP
   drafting, so self-speculation could give ~1.5–2.5× on this memory-bound decode.
   **Drafting-support check is not yet done:** the GGUF confirms `general.architecture =
deepseek4` with `deepseek4.{block_count,expert_count,expert_used_count}`, but a
   `strings` scan for MTP was inconclusive (matched vocab words). Do it properly: dump
   GGUF metadata (`llama-gguf` / `gguf_dump.py`) and look for `deepseek4.nextn*` /
   MTP-layer keys, and confirm mainline or ik_llama exposes DSV4 self-speculation (or pair
   a tiny `--model-draft`).

Then: a proper coding-quality spot-check + tok/s at real (non-4K) context.

## BLOCKER (2026-07-18): wyrm2 GPU lockup

Attempting the `--n-cpu-moe` sweep this session hit a **GPU lockup**: an E9 Vulkan
`llama-cli` wedged (spinning 103% CPU for ~1h45m on a 32-token generation), **GPU1 went to
NVML "Unknown Error"** while still present on the PCI bus — the FULLCHIP_RESET hang. Full
forensics from this session (kernel log signature, timeline, recovery) are in
<../../../../../debug/atlas/gpu_lockup_20260718/README.md>; background investigation in
<../../../../../debug/atlas/wyrm_gpu_lockup.md> and
<../../../../../debug/atlas/gpu_lockup_20260417/README.md>. These intermittent VFIO-
passthrough 5090 lockups block the whole optimization thread. **Before resuming: recover
the GPUs (kill the wedged process; GPU reset or VM reboot per those notes), and ideally
land a fix for the lockups themselves** — that stability work is the real prerequisite,
tracked in the debug notes above.

## Repro

```bash
# on wyrm2 (model at /var/lib/colibri/dsv4-iq2/):
./build.sh download   # ~91 GB GGUF (if not present)
./build.sh cpu        # or: ./build.sh vulkan
./run.sh vulkan       # or: ./run.sh cpu
```
