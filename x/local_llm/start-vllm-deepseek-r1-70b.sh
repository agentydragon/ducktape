#!/bin/bash
# Start vLLM server with DeepSeek R1 Distill Llama 70B AWQ
#
# Hardware: 2x RTX 5090 (64GB total VRAM)
# Model: casperhansen/deepseek-r1-distill-llama-70b-awq
#
# Key features:
#   - Best quality distillation from DeepSeek-R1
#   - Thinking/reasoning preserved through distillation
#   - Tool calling supported (Llama 3.1 base model)
#   - AWQ 4-bit quantization: ~38 GB total, requires TP=2
#   - 128K native context length
#
# Benchmarks: LiveCodeBench 57.5% (best distilled)
#
# vLLM serves OpenAI-compatible API at http://localhost:8000/v1
#
# Usage:
#   ./start-vllm-deepseek-r1-70b.sh                    # Use defaults
#   MAX_MODEL_LEN=65536 ./start-vllm-deepseek-r1-70b.sh  # Custom context

set -euo pipefail

# Model to serve (AWQ 4-bit quantized DeepSeek R1 distillation of Llama 70B)
MODEL="${MODEL:-casperhansen/deepseek-r1-distill-llama-70b-awq}"

# Context length (reduced to fit 64GB with 38GB weights)
MAX_MODEL_LEN="${MAX_MODEL_LEN:-65536}"

# KV cache dtype (fp8 doubles context capacity vs fp16)
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8}"

# GPU memory utilization
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"

# Max concurrent sequences (default 256 causes OOM during warmup with large context)
MAX_NUM_SEQS="${MAX_NUM_SEQS:-16}"

# Port (different from Ollama's 11434)
PORT="${PORT:-8000}"

# HuggingFace cache directory
HF_CACHE="${HF_CACHE:-/wyrmhdd/huggingface}"

VLLM_START_MESSAGE="Starting vLLM server (DeepSeek R1 Distill Llama 70B, thinking + tools)..."
VLLM_IMAGE="docker.io/vllm/vllm-openai:latest"
VLLM_SERVED_MODEL_NAME="deepseek-r1-70b"
VLLM_TOOL_CALL_PARSER="hermes"
VLLM_EXTRA_FLAGS=()

source "$(dirname "${BASH_SOURCE[0]}")/start-vllm-podman.sh"
