#!/bin/bash
# Start vLLM server with Qwen3-Coder-30B-A3B AWQ 4-bit using tensor parallelism
#
# Hardware: 2x RTX 5090 (64GB total VRAM)
# Model: cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit
#
# AWQ 4-bit quantization reduces weights from ~30.5 GB to ~8.5 GiB per GPU,
# leaving ~22 GiB per GPU for KV cache. With FP8 KV cache: 262K context.
#
# Key fix: --max-num-seqs 32 prevents OOM during warmup (default 256 is too high).
#
# ⚠️ LIMITATION: Qwen3-Coder does NOT support thinking/reasoning mode.
#    This is a base model property, not something the AWQ quantization removed.
#    Qwen3-Coder was post-trained without thinking mode fusion (Agent RL only).
#    For thinking + tool use, use Qwen3-30B-A3B (original) instead.
#
# vLLM serves OpenAI-compatible API at http://localhost:8000/v1
#
# Usage:
#   ./start-vllm-awq.sh                    # Use defaults
#   MAX_MODEL_LEN=65536 ./start-vllm-awq.sh  # Custom context

set -euo pipefail

# Model to serve (AWQ 4-bit quantized)
MODEL="${MODEL:-cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit}"

# Context length (262K with FP8 KV cache; 131K with FP16 KV)
MAX_MODEL_LEN="${MAX_MODEL_LEN:-262144}"

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

echo "Starting vLLM server (AWQ 4-bit, FP8 KV)..."
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
  vllm/vllm-openai:latest \
  --model "$MODEL" \
  --served-model-name qwen3-coder-awq \
  --tensor-parallel-size 2 \
  --max-model-len "$MAX_MODEL_LEN" \
  --kv-cache-dtype "$KV_CACHE_DTYPE" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --max-num-seqs "$MAX_NUM_SEQS" \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  "$@"
