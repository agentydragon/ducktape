#!/bin/bash
# Start vLLM server with Qwen3-30B-A3B FP8 (original, thinking + tool use)
#
# Hardware: 2x RTX 5090 (64GB total VRAM)
# Model: Qwen/Qwen3-30B-A3B-FP8
#
# FP8 weights are ~16 GB total (~8 GB/GPU with TP=2), leaving ~23 GB/GPU
# for KV cache. With FP8 KV cache, should support 200K+ context.
#
# This is the ORIGINAL Qwen3-30B-A3B (not Coder, not Thinking-2507).
# It supports BOTH thinking mode (toggle) AND tool calling.
#
# vLLM serves OpenAI-compatible API at http://localhost:8000/v1
#
# Usage:
#   ./start-vllm-qwen3-thinking.sh                       # Use defaults
#   MAX_MODEL_LEN=65536 ./start-vllm-qwen3-thinking.sh   # Custom context

set -euo pipefail

# Model to serve (official Qwen FP8)
MODEL="${MODEL:-Qwen/Qwen3-30B-A3B-FP8}"

# Context length — start conservatively, increase after testing
MAX_MODEL_LEN="${MAX_MODEL_LEN:-131072}"

# KV cache dtype (fp8 doubles context capacity vs fp16)
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"

# GPU memory utilization
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"

# Max concurrent sequences (default 256 causes OOM during warmup with large context)
MAX_NUM_SEQS="${MAX_NUM_SEQS:-32}"

# Port
PORT="${PORT:-8000}"

# HuggingFace cache directory
HF_CACHE="${HF_CACHE:-/wyrmhdd/huggingface}"

# Served model name (for API requests)
SERVED_NAME="${SERVED_NAME:-qwen3-30b-a3b}"

VLLM_START_MESSAGE="Starting vLLM server (Qwen3-30B-A3B FP8, thinking + tool use)..."
VLLM_IMAGE="vllm/vllm-openai:latest"
VLLM_SERVED_MODEL_NAME="$SERVED_NAME"
VLLM_TOOL_CALL_PARSER="hermes"
VLLM_EXTRA_FLAGS=(--enable-reasoning --reasoning-parser qwen3)

source "$(dirname "${BASH_SOURCE[0]}")/start-vllm-podman.sh"
