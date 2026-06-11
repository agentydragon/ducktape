#!/bin/bash
# Start vLLM server with DeepSeek R1 Distill Qwen 32B AWQ
#
# Hardware: 2x RTX 5090 (64GB total VRAM)
# Model: casperhansen/deepseek-r1-distill-qwen-32b-awq
#
# Key features:
#   - Thinking/reasoning preserved through distillation from DeepSeek-R1
#   - Tool calling supported (Qwen base model)
#   - AWQ 4-bit quantization: ~17 GB total, can run TP=2 for better throughput
#   - 128K native context length
#
# Benchmarks: LiveCodeBench 57.2%, CodeForces 1691
#
# vLLM serves OpenAI-compatible API at http://localhost:8000/v1
#
# Usage:
#   ./start-vllm-deepseek-r1.sh                    # Use defaults
#   MAX_MODEL_LEN=65536 ./start-vllm-deepseek-r1.sh  # Custom context

set -euo pipefail

# Model to serve (AWQ 4-bit quantized DeepSeek R1 distillation)
MODEL="${MODEL:-casperhansen/deepseek-r1-distill-qwen-32b-awq}"

# Context length (128K native)
MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"

# KV cache dtype (fp8 doubles context capacity vs fp16)
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"

# GPU memory utilization
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"

# Max concurrent sequences (default 256 causes OOM during warmup with large context)
MAX_NUM_SEQS="${MAX_NUM_SEQS:-32}"

# Port (different from Ollama's 11434)
PORT="${PORT:-8000}"

# HuggingFace cache directory
HF_CACHE="${HF_CACHE:-/wyrmhdd/huggingface}"

echo "Starting vLLM server (DeepSeek R1 Distill, thinking + tools)..."
echo "  Model: $MODEL"
echo "  Context: $MAX_MODEL_LEN tokens"
echo "  KV cache: $KV_CACHE_DTYPE"
echo "  Tensor parallel: 2 GPUs"
echo "  GPU memory util: $GPU_MEM_UTIL"
echo "  Max sequences: $MAX_NUM_SEQS"
echo "  API endpoint: http://localhost:$PORT/v1"
echo ""

# Remove any existing container
podman rm -f vllm 2>/dev/null || true

exec podman run --rm --name vllm \
  --device nvidia.com/gpu=all \
  -v "$HF_CACHE:/root/.cache/huggingface" \
  -p "$PORT:8000" \
  docker.io/vllm/vllm-openai:latest \
  --model "$MODEL" \
  --served-model-name deepseek-r1-32b \
  --tensor-parallel-size 2 \
  --max-model-len "$MAX_MODEL_LEN" \
  --kv-cache-dtype "$KV_CACHE_DTYPE" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --max-num-seqs "$MAX_NUM_SEQS" \
  --enable-auto-tool-choice \
  --tool-call-parser hermes \
  "$@"
