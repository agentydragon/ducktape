#!/bin/bash
# Start vLLM server with Qwen3-Coder-30B-A3B using tensor parallelism
#
# Hardware: 2x RTX 5090 (64GB total VRAM)
# Model: Qwen3-Coder-30B-A3B (MoE, 3.3B active, ~19GB Q4)
#
# With tensor parallelism, the model is split across both GPUs, leaving
# more VRAM per GPU for KV cache = longer context.
#
# vLLM serves OpenAI-compatible API at http://localhost:8000/v1

set -euo pipefail

# Model to serve
MODEL="${MODEL:-Qwen/Qwen3-Coder-30B-A3B-Instruct}"

# Context length (32K works; 131K OOMs due to VM passthrough lacking GPU P2P)
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"

# GPU memory utilization (0.95 to maximize KV cache)
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.95}"

# Port (different from Ollama's 11434)
PORT="${PORT:-8000}"

echo "Starting vLLM server..."
echo "  Model: $MODEL"
echo "  Context: $MAX_MODEL_LEN tokens"
echo "  Tensor parallel: 2 GPUs"
echo "  API endpoint: http://localhost:$PORT/v1"
echo ""

exec python -m vllm.entrypoints.openai.api_server \
  --model "$MODEL" \
  --served-model-name qwen3-coder-tp2 \
  --tensor-parallel-size 2 \
  --max-model-len "$MAX_MODEL_LEN" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --port "$PORT" \
  "$@"
