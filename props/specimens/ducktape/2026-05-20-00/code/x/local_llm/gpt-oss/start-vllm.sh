#!/bin/bash
# Start vLLM server with GPT-OSS-20B
#
# Hardware: RTX 5090 (32GB) - fits easily on single GPU
# Model: openai/gpt-oss-20b
#
# Key features:
#   - MoE: 21B total params, ~2B active per token
#   - Native thinking/reasoning (CoT)
#   - Native tool calling
#   - 128K context window
#
# vLLM serves OpenAI-compatible API at http://localhost:8000/v1
# Tool calling: --tool-call-parser openai --enable-auto-tool-choice
# See: https://docs.vllm.ai/projects/recipes/en/latest/OpenAI/GPT-OSS.html
#
# Usage:
#   ./start-vllm.sh                           # Use defaults
#   MAX_MODEL_LEN=65536 ./start-vllm.sh       # Custom context

set -euo pipefail

# Model to serve
MODEL="${MODEL:-openai/gpt-oss-20b}"

# Context length (128K native)
MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"

# Port
PORT="${PORT:-8000}"

# HuggingFace cache directory
HF_CACHE="${HF_CACHE:-/wyrmhdd/huggingface}"

echo "Starting vLLM server (GPT-OSS-20B, thinking + tools)..."
echo "  Model: $MODEL"
echo "  Context: $MAX_MODEL_LEN tokens"
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
  --served-model-name gpt-oss-20b \
  --max-model-len "$MAX_MODEL_LEN" \
  --enable-auto-tool-choice \
  --tool-call-parser openai \
  "$@"
