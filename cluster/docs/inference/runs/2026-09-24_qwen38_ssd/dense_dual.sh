#!/usr/bin/env bash
set -euo pipefail

context_size=${CONTEXT_SIZE:-8192}
model_root=/var/lib/llm-models-ssd
model=Qwen3.8-27B-GGUF/Qwen3.8-27B-Q8_0.gguf
image=ghcr.io/ggml-org/llama.cpp@sha256:014f721265464f38ccb247c1338d07d852c4bae7509a4b4734d07a2bbadc765c

mountpoint -q "$model_root"
test "$(stat -c %s "$model_root/$model")" = 29047086048
# Run only after the download's SHA256 check passes; see checkpoints.json.
docker run --detach --name wyrm2-qwen38-dense \
  --device=nvidia.com/gpu=all \
  --env CUDA_VISIBLE_DEVICES=GPU-690929fc-97bf-9d39-d2f5-0322f1715b16,GPU-6154a49f-2cad-6b72-1e85-09b8006d08b5 \
  --user 1001:100 --cap-drop ALL --security-opt no-new-privileges \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --memory=16g --memory-swap=16g --cpus=8 \
  --mount "type=bind,source=$model_root,target=/models,readonly" \
  --publish 127.0.0.1:18080:8080 \
  --env CUDA_CACHE_PATH=/tmp/cuda-cache \
  "$image" \
  --model "/models/$model" --alias qwen3.8-27b-q8 \
  --host 0.0.0.0 --port 8080 --ctx-size "$context_size" --parallel 1 \
  --threads 8 --threads-batch 8 --split-mode layer --tensor-split 1,1 --fit-target 8192,2048 \
  --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 \
  --metrics --reasoning-format deepseek
