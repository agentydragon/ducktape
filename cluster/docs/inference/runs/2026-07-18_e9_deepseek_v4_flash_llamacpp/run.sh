#!/usr/bin/env bash
# E9 — run DeepSeek-V4-Flash (IQ2) on wyrm2. Captures the exact runtime wiring.
#
#   ./run.sh cpu    [prompt]   # CPU-only (build/bin/llama-cli)      ~1.1 tok/s
#   ./run.sh vulkan [prompt]   # GPU offload (build-vk/bin/llama-cli) ~2.9 tok/s
#
# Vulkan gotchas baked in (do not drop):
#   - The llama-cli is linked against the nix vulkan-loader, so at runtime it needs
#     LD_LIBRARY_PATH to that loader AND the driver libs in /run/opengl-driver/lib.
#   - Point VK_ICD_FILENAMES at the NVIDIA ICD explicitly:
#     /run/opengl-driver/share/vulkan/icd.d/nvidia_icd.json — auto-discovery may pick
#     the wrong ICD (asahi/nouveau/etc. are also present in that dir).
#   - `--cpu-moe` keeps ALL routed experts on CPU/RAM (the ~80 GB bulk); everything
#     else (attention/shared/KV) goes on the 2×5090 via `-ngl 999`. To go faster, move
#     hot experts to spare VRAM with `--n-cpu-moe N` (N = layers kept on CPU; lower =
#     more on GPU) — bounded by VRAM.
#   - `-c 4096`: DSV4-Flash is native 1M, so the DEFAULT KV context OOMs the GPU. Cap it.
set -euo pipefail

REPO=${LLAMACPP_DIR:-$HOME/llama-cpp-main}
GGUF=${DSV4_GGUF:-/var/lib/colibri/dsv4-iq2/UD-IQ2_XXS/DeepSeek-V4-Flash-UD-IQ2_XXS-00001-of-00003.gguf}
BACKEND=${1:?usage: run.sh {cpu|vulkan} [prompt]}
PROMPT=${2:-"Write a Python function is_prime(n) with a docstring."}
COMMON=(-m "$GGUF" -p "$PROMPT" -n 128 -t 24 -no-cnv --temp 0)

case "$BACKEND" in
  cpu)
    "$REPO/build/bin/llama-cli" "${COMMON[@]}"
    ;;
  vulkan)
    VKL=$(NIXPKGS_ALLOW_UNFREE=1 nix eval --raw nixpkgs#vulkan-loader)
    LD_LIBRARY_PATH="$VKL/lib:/run/opengl-driver/lib" \
      VK_ICD_FILENAMES=/run/opengl-driver/share/vulkan/icd.d/nvidia_icd.json \
      "$REPO/build-vk/bin/llama-cli" -ngl 999 --cpu-moe -c 4096 "${COMMON[@]}"
    ;;
  *)
    echo "unknown backend: $BACKEND" >&2
    exit 1
    ;;
esac
