#!/usr/bin/env bash
# E9 — build llama.cpp (CPU or Vulkan) for DeepSeek-V4-Flash on wyrm2, and fetch the
# IQ2 GGUF. Captures the exact wiring that took many iterations to pin down — run on
# wyrm2 (NixOS, no global CUDA/Vulkan SDK; everything comes from `nix shell`).
#
#   ./build.sh download   # fetch the ~91 GB UD-IQ2_XXS GGUF
#   ./build.sh cpu        # CPU-only build (trivial, always works — the coherence floor)
#   ./build.sh vulkan     # GPU build via Vulkan (no CUDA toolchain needed)
#
# Gotchas baked in (do not "simplify" these away):
#   - Use MAINLINE ggml-org/llama.cpp. The nisparks `wip/deepseek-v4-support` branch
#     (PR #22378) FAILS with `missing tensor 'hc_head_base'` — it predates the merged
#     DSV4 support (am17an #24162 + fairydreaming #24231/#25370). The unsloth GGUF
#     targets mainline.
#   - nixpkgs splits Vulkan/SPIRV into components, and cmake's FindVulkan + ggml-vulkan
#     don't discover them from a bare `nix shell`. So every path is passed explicitly,
#     and spirv-headers' include dir must be on CPATH or the compile can't find
#     `spirv/unified1/spirv.hpp`.
set -euo pipefail

REPO=${LLAMACPP_DIR:-$HOME/llama-cpp-main}
MODEL_DIR=${DSV4_MODEL_DIR:-/var/lib/colibri/dsv4-iq2}
GGUF="$MODEL_DIR/UD-IQ2_XXS/DeepSeek-V4-Flash-UD-IQ2_XXS-00001-of-00003.gguf"
export NIXPKGS_ALLOW_UNFREE=1

cmd=${1:?usage: build.sh {download|cpu|vulkan}}

case "$cmd" in
  download)
    # HF token expected at ~/.cache/huggingface/token. hf_transfer = fast; disable Xet (stalls).
    HF_HUB_ENABLE_HF_TRANSFER=1 HF_HUB_DISABLE_XET=1 \
      uvx --from 'huggingface_hub[cli,hf_transfer]' hf download \
      unsloth/DeepSeek-V4-Flash-GGUF --include 'UD-IQ2_XXS/*' --local-dir "$MODEL_DIR"
    ;;

  cpu)
    [[ -d $REPO/.git ]] || git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$REPO"
    cd "$REPO"
    nix shell --impure nixpkgs#cmake nixpkgs#gcc nixpkgs#gnumake --command bash -c '
    cmake -B build -DGGML_CUDA=OFF -DLLAMA_CURL=OFF -DGGML_NATIVE=ON -DCMAKE_BUILD_TYPE=Release
    cmake --build build -j"$(nproc)" --target llama-cli'
    echo "built: $REPO/build/bin/llama-cli"
    ;;

  vulkan)
    [[ -d $REPO/.git ]] || git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$REPO"
    cd "$REPO"
    VKL=$(nix eval --raw nixpkgs#vulkan-loader)
    VKH=$(nix eval --raw nixpkgs#vulkan-headers)
    SHC=$(nix eval --raw nixpkgs#shaderc.bin)
    SPH=$(nix eval --raw nixpkgs#spirv-headers)
    SPT=$(nix eval --raw nixpkgs#spirv-tools)
    GLS=$(nix eval --raw nixpkgs#glslang)
    nix shell --impure nixpkgs#cmake nixpkgs#gcc nixpkgs#gnumake \
      nixpkgs#glslang nixpkgs#shaderc nixpkgs#spirv-headers nixpkgs#spirv-tools --command bash -c "
      export CPATH=$SPH/include:\${CPATH:-}
      cmake -B build-vk -DGGML_VULKAN=ON -DLLAMA_CURL=OFF -DGGML_NATIVE=ON -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_PREFIX_PATH='$SPH;$SPT;$GLS' \
        -DVulkan_LIBRARY=$VKL/lib/libvulkan.so -DVulkan_INCLUDE_DIR=$VKH/include \
        -DVulkan_GLSLC_EXECUTABLE=$SHC/bin/glslc
      cmake --build build-vk -j\"\$(nproc)\" --target llama-cli"
    echo "built: $REPO/build-vk/bin/llama-cli"
    ;;

  *)
    echo "unknown: $cmd" >&2
    exit 1
    ;;
esac

echo "model GGUF: $GGUF"
