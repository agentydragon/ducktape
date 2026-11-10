# Shared Podman lifecycle and vLLM flags for the model-specific host launchers.
# Each entrypoint sets these values before sourcing this file.
echo "$VLLM_START_MESSAGE"
echo "  Model: $MODEL"
echo "  Context: $MAX_MODEL_LEN tokens"
echo "  KV cache: $KV_CACHE_DTYPE"
echo "  Tensor parallel: 2 GPUs"
echo "  GPU memory util: $GPU_MEM_UTIL"
echo "  Max sequences: $MAX_NUM_SEQS"
echo "  API endpoint: http://localhost:$PORT/v1"
echo ""

# Remove any existing container.
podman rm -f vllm 2>/dev/null || true

exec podman run --rm --name vllm \
  --device nvidia.com/gpu=all \
  -v "$HF_CACHE:/root/.cache/huggingface" \
  -p "$PORT:8000" \
  "$VLLM_IMAGE" \
  --model "$MODEL" \
  --served-model-name "$VLLM_SERVED_MODEL_NAME" \
  --tensor-parallel-size 2 \
  --max-model-len "$MAX_MODEL_LEN" \
  --kv-cache-dtype "$KV_CACHE_DTYPE" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --max-num-seqs "$MAX_NUM_SEQS" \
  "${VLLM_EXTRA_FLAGS[@]}" \
  --enable-auto-tool-choice \
  --tool-call-parser "$VLLM_TOOL_CALL_PARSER" \
  "$@"
