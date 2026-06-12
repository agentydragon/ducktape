# VRAM Analysis for 2x RTX 5090

For the model search log and download list, see <model_download_history.md>.

## Hardware

- 2x RTX 5090: **32 GB GDDR7 each = 64 GB total VRAM**

## Qwen3-Coder Model Family

Qwen3-Coder only comes in **two sizes**, both MoE (Mixture of Experts):

| Model                     | Total Params | Active Params | Native Context          |
| ------------------------- | ------------ | ------------- | ----------------------- |
| **Qwen3-Coder-30B-A3B**   | 30.5B        | 3.3B          | 262K                    |
| **Qwen3-Coder-480B-A35B** | 480B         | 35B           | 262K (extendable to 1M) |

Both are MoE models, meaning only a fraction of parameters are active per forward pass. This affects memory for weights but **not** for KV cache.

Sources: [QwenLM/Qwen3-Coder GitHub](https://github.com/QwenLM/Qwen3-Coder), [Qwen Blog](https://qwenlm.github.io/blog/qwen3-coder/)

## Available Quantizations

### Qwen3-Coder-480B-A35B (from [Unsloth GGUF](https://huggingface.co/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF))

| Quantization | Size   | Fits 64GB? |
| ------------ | ------ | ---------- |
| BF16         | 960 GB | ❌ No      |
| Q8_0         | 510 GB | ❌ No      |
| Q6_K         | 394 GB | ❌ No      |
| Q5_K_M       | 340 GB | ❌ No      |
| Q4_K_M       | 290 GB | ❌ No      |
| Q4_K_S       | 273 GB | ❌ No      |
| Q3_K_M       | 229 GB | ❌ No      |
| Q2_K         | 175 GB | ❌ No      |
| IQ1_M        | 150 GB | ❌ No      |

**The 480B model does not fit on 2x 5090 at any quantization level.**

### Qwen3-Coder-30B-A3B (from [Unsloth GGUF](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF))

| Quantization | Size    | Fits 64GB? | Available on Ollama                    |
| ------------ | ------- | ---------- | -------------------------------------- |
| BF16         | 61.1 GB | ⚠️ Barely  | `qwen3-coder:30b-a3b-fp16`             |
| Q8_0         | 32.5 GB | ✅ Yes     | `qwen3-coder:30b-a3b-q8_0`             |
| Q8_K_XL      | 36 GB   | ✅ Yes     | —                                      |
| Q6_K         | 25.1 GB | ✅ Yes     | —                                      |
| Q6_K_XL      | 26.3 GB | ✅ Yes     | —                                      |
| Q5_K_M       | 21.7 GB | ✅ Yes     | —                                      |
| Q5_K_S       | 21.1 GB | ✅ Yes     | —                                      |
| Q4_K_M       | 18.6 GB | ✅ Yes     | `qwen3-coder:30b-a3b-q4_K_M` (default) |
| Q4_K_XL      | 17.7 GB | ✅ Yes     | —                                      |
| Q4_K_S       | 17.5 GB | ✅ Yes     | —                                      |
| Q4_0         | 17.4 GB | ✅ Yes     | —                                      |
| IQ4_XS       | 16.4 GB | ✅ Yes     | —                                      |
| Q3_K_M       | 14.7 GB | ✅ Yes     | —                                      |
| Q3_K_S       | 13.3 GB | ✅ Yes     | —                                      |
| Q2_K         | 11.3 GB | ✅ Yes     | —                                      |
| IQ2_M        | 10.8 GB | ✅ Yes     | —                                      |
| IQ1_M        | 9.63 GB | ✅ Yes     | —                                      |

**Recommendation**: Q4_K_M or higher. Below Q4, quality degrades noticeably for coding.

Source: [Ollama qwen3-coder tags](https://ollama.com/library/qwen3-coder/tags)

## Architecture (for KV Cache Calculation)

### Qwen3-Coder-30B-A3B

| Parameter       | Value          |
| --------------- | -------------- |
| Layers          | 48             |
| Attention Heads | 32             |
| KV Heads (GQA)  | 8              |
| Head Dimension  | 128            |
| Hidden Size     | 2048           |
| Experts         | 128 (8 active) |

### Qwen3-Coder-480B-A35B

| Parameter       | Value          |
| --------------- | -------------- |
| Layers          | 62             |
| Attention Heads | 96             |
| KV Heads (GQA)  | 8              |
| Head Dimension  | 128            |
| Hidden Size     | 6144           |
| Experts         | 160 (8 active) |

Source: [NVIDIA NIM modelcard](https://build.nvidia.com/qwen/qwen3-coder-480b-a35b-instruct/modelcard)

## KV Cache Memory Math

**Formula**:

```text
KV cache per token = 2 × num_layers × num_kv_heads × head_dim × bytes_per_element
```

### Qwen3-Coder-30B-A3B (KV cache memory)

**FP16 KV cache**:

```text
= 2 × 48 × 8 × 128 × 2 bytes
= 196,608 bytes
= 192 KB per token
```

**FP8 KV cache**:

```text
= 2 × 48 × 8 × 128 × 1 byte
= 98,304 bytes
= 96 KB per token
```

## Context Length Calculations for 2x 5090

**Available VRAM** = 64 GB total - Model weights - ~3 GB overhead

### Qwen3-Coder-30B-A3B (context length by quantization)

| Quantization | Model Size | VRAM for KV | Max Context (FP16 KV) | Max Context (FP8 KV) |
| ------------ | ---------- | ----------- | --------------------- | -------------------- |
| Q4_K_M       | 19 GB      | 42 GB       | **218K tokens**       | **437K tokens**      |
| Q5_K_M       | 22 GB      | 39 GB       | **203K tokens**       | **406K tokens**      |
| Q6_K         | 25 GB      | 36 GB       | **187K tokens**       | **375K tokens**      |
| Q8_0         | 32 GB      | 29 GB       | **151K tokens**       | **302K tokens**      |
| FP16         | 61 GB      | 0 GB        | ❌ No room            | ❌ No room           |

**Key finding**: With Q4_K_M, you can nearly max out the native 262K context with FP8 KV cache, or get 218K with FP16 KV.

## Practical Recommendations

### Best for vLLM + Tensor Parallelism: AWQ 4-bit (RECOMMENDED)

```text
Model: cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit
Weights: ~7.6 GB per GPU (vs 28.5 GB bf16)
KV cache: ~23 GB available per GPU
Max context: 131K+ tokens
```

AWQ 4-bit quantization reduces weights by ~4x while maintaining quality.
This is the **only way to run full context on 2x 5090 with vLLM**.

**⚠️ No Thinking Mode**: Qwen3-Coder does not support thinking mode. This is a base model
property — Qwen3-Coder was post-trained with Agent RL only, without thinking mode fusion.
No Qwen3-Coder variant (bf16, FP8, AWQ, etc.) has thinking. For thinking + tool use,
use the original Qwen3-30B-A3B instead.

```bash
# Start vLLM with AWQ model
~/code/ducktape/experimental/local-llm/start-vllm-awq.sh
```

### For Ollama: Q4_K_M with FP8 KV Cache

```text
Model: qwen3-coder:30b-a3b-q4_K_M (19 GB)
KV cache: FP8 (96 KB/token)
Max context: ~437K tokens (exceeds native 262K)
```

You can use the **full native 262K context** comfortably with room to spare.

### Maximum Quality (Ollama): Q8_0 with FP16 KV Cache

```text
Model: qwen3-coder:30b-a3b-q8_0 (32 GB)
KV cache: FP16 (192 KB/token)
Max context: ~151K tokens
```

Higher precision weights and KV cache. 151K is still substantial.

## Options to Increase Context Length

### 1. FP8 KV Cache Quantization

**Impact**: 2x more context for same VRAM

vLLM supports this natively:

```bash
python -m vllm.entrypoints.openai.api_server \
  --model unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF \
  --kv-cache-dtype fp8 \
  --tensor-parallel-size 2 \
  --max-model-len 262144
```

Ollama doesn't expose FP8 KV cache controls yet.

### 2. Use Lower Weight Quantization

Going from Q8 to Q4 frees ~13 GB for KV cache = ~67K more tokens (FP16) or ~135K (FP8).

Quality tradeoff is modest for Q4_K_M on coding tasks.

### 3. CPU Offloading (llama.cpp)

Offload KV cache or some layers to system RAM:

```bash
# Example: load model to GPU, but allow KV cache overflow to RAM
./llama-server -m qwen3-coder-30b-a3b-q4_k_m.gguf \
  -ngl 99 \
  -c 524288 \
  --mlock
```

**Tradeoff**: Slower when accessing CPU memory, but enables arbitrary context.

### 4. Sliding Window / Sparse Attention

Some inference engines support sliding window attention where only recent N tokens get full attention. Check if your inference engine supports this for Qwen3.

### 5. Use the 480B via API/Cloud

Ollama offers `qwen3-coder:480b-cloud` which routes to cloud inference. No local VRAM needed.

Other options:

- [Together AI](https://www.together.ai/qwen)
- [NVIDIA NIM](https://build.nvidia.com/qwen/qwen3-coder-480b-a35b-instruct)

## Real-World Testing: vLLM with VM GPU Passthrough

**Setup**: 2x RTX 5090 passed through to Proxmox VM, vLLM with tensor parallelism.

### Key Discovery: No GPU P2P in VM Passthrough

vLLM logs:

```text
WARNING: Custom allreduce is disabled because your platform lacks GPU P2P capability
```

**Impact**: Without GPU Peer-to-Peer communication, tensor parallelism requires CPU-mediated data transfer between GPUs. This adds significant memory overhead for staging buffers.

### Actual Memory Usage (vLLM, HF weights)

| Metric                    | Value                 |
| ------------------------- | --------------------- |
| Model loading             | **28.51 GB**          |
| GPU memory utilization    | 0.90 (57.6 GB usable) |
| Available KV cache @ 131K | **-1.84 GB** (OOM!)   |

The model + P2P fallback overhead consumed ~59.4 GB, leaving negative headroom for 131K context.

### Context Length Test Results

| Context        | gpu_memory_utilization | KV dtype | Result                              |
| -------------- | ---------------------- | -------- | ----------------------------------- |
| 131,072 (131K) | 0.90                   | FP16     | ❌ OOM (-1.84 GB)                   |
| 65,536 (65K)   | 0.95                   | FP16     | ❌ OOM (-0.27 GB)                   |
| 65,536 (65K)   | 0.95                   | FP8      | ❌ OOM (-0.27 GB) - FP8 didn't help |
| 65,536 (65K)   | 0.99                   | FP16     | 🧪 Testing                          |
| 32,768 (32K)   | 0.95                   | FP16     | ✅ Works                            |

### Detailed Memory Breakdown (from debug logs)

Per GPU (RTX 5090, 32GB):

```text
Total memory:           31.36 GiB
Requested (0.95 util):  29.79 GiB
Non-KV cache memory:    30.06 GiB  ← exceeds budget by 0.27 GiB
  - Weights:            28.51 GiB per GPU
  - Torch peak:         1.52 GiB
  - Non-torch overhead: ~0.03 GiB (NCCL, etc.)
```

**Root cause**: The model + overhead uses 30.06 GiB per GPU, but 0.95 utilization only allows 29.79 GiB.

**FP8 KV cache didn't help** because the bottleneck is fixed overhead (weights + activations), not KV cache size. The -0.27 GiB deficit occurs before any KV cache is allocated.

## Forensic Analysis: Why 28.51 GiB Weights Per GPU?

### Ground Truth from Safetensors

Direct analysis of the model's safetensors files provides the actual memory breakdown:

```text
======================================================================
GROUND TRUTH: Qwen3-Coder-30B-A3B Weight Breakdown (bf16)
======================================================================
Category        Total Model     Per GPU (TP=2)
----------------------------------------------------------------------
Experts          57.98 GB        28.99 GB (sharded)
Attention         1.81 GB         0.91 GB (sharded)
Embeddings        0.62 GB         0.31 GB (sharded)
LM head           0.62 GB         0.31 GB (sharded)
Router            0.03 GB         0.03 GB (replicated)
Norms             0.00 GB         0.00 GB (replicated)
----------------------------------------------------------------------
TOTAL            61.06 GB        30.54 GB
----------------------------------------------------------------------
EXPECTED/GPU                     28.45 GiB
OBSERVED/GPU                     28.51 GiB
DIFFERENCE                        0.06 GiB (padding/alignment)
```

### Key Finding: Memory Accounting is Correct

The 28.51 GiB per GPU is **exactly right**. Tensor parallelism IS working:

- All 128 experts exist on each GPU, but each expert's weights are sharded by TP=2
- Expert weights dominate: 28.99 GB of the 30.54 GB per GPU
- The 0.06 GiB difference is standard memory alignment overhead

### Why It Doesn't Fit on 32 GB GPUs

The actual problem is simple math:

```text
RTX 5090 usable memory:  31.36 GiB
Model weights (TP=2):   -28.51 GiB
Remaining:                2.85 GiB

vLLM needs for operations:
  - Activation peak:     ~1.52 GiB
  - NCCL buffers:        ~0.5+ GiB (no P2P in VM)
  - torch.compile cache: ~variable
  - KV cache:            ??? (nothing left)
```

**The bf16 model is simply too large** for 32 GB GPUs. At 0.95 utilization (29.79 GiB), we exceed the budget by 0.27 GiB before any KV cache is allocated.

### Solution: Quantized Weights

Available quantized versions:

- `cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit` (AWQ)
- `Intel/Qwen3-Coder-30B-A3B-Instruct-int4-AutoRound` (INT4)

With AWQ 4-bit quantization:

```text
Weights per GPU:  ~7.6 GB (vs 28.5 GB for bf16)
Remaining:        ~23 GB for KV cache
Max context:      100K+ tokens (FP16 KV) or 200K+ (FP8 KV)
```

```bash
docker run --rm --name vllm \
  --gpus all \
  -v /wyrmhdd/huggingface:/root/.cache/huggingface \
  -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit \
  --quantization awq \
  --tensor-parallel-size 2 \
  --max-model-len 131072 \
  --gpu-memory-utilization 0.90
```

### Why VM Passthrough Makes It Worse

Without GPU P2P:

- vLLM disables "custom allreduce" (optimized inter-GPU communication)
- Falls back to NCCL CPU-mediated transfer
- This doesn't increase weight storage, but adds communication buffer overhead
- Combined with torch.compile caching and activation memory, pushes past the 0.95 utilization limit

### Why Theoretical != Actual

The analysis above assumes **bare metal with GPU P2P**. VM passthrough disables P2P, adding:

- Communication buffers on each GPU for CPU-mediated allreduce
- Higher memory fragmentation
- ~30+ GB total overhead (model + buffers) vs ~19 GB theoretical for Q4_K_M weights

### Recommendations for VM GPU Passthrough

| Configuration    | Context | Notes                    |
| ---------------- | ------- | ------------------------ |
| **Safe**         | 32K     | Works reliably           |
| **Aggressive**   | 65K     | May work, test first     |
| **Not feasible** | 131K+   | OOMs due to P2P overhead |

For full 131K+ context, you'd need:

- Bare metal with NVLink/PCIe P2P between GPUs
- Or single-GPU inference (no TP communication overhead)
- Or FP8 KV cache (halves KV memory, may recover enough headroom)

---

## Summary

For model tables, experiment log, and download status, see <model_download_history.md>.

**Critical vLLM fixes discovered:**

1. **`--max-num-seqs 32`** — Default 256 causes OOM during warmup
2. **`--kv-cache-dtype fp8`** — Doubles context capacity vs FP16
3. **Don't use `--quantization awq`** — Model auto-detects `compressed-tensors` format

The bf16 model (61 GB) is too large for 2x 32GB GPUs even with TP=2.

**VM Passthrough note**: Without GPU P2P, vLLM disables custom allreduce (faster inter-GPU communication). This slightly increases latency but doesn't affect memory. The real issue is that bf16 weights (28.5 GB/GPU) leave no room for KV cache on 32 GB GPUs. AWQ quantization solves this by reducing weights to ~8.5 GB/GPU.

## Real-World AWQ Performance (2026-01-24)

Tested with `cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit` on vLLM:

### Memory Usage

| Metric             | Value                |
| ------------------ | -------------------- |
| Model loading      | **8.52 GiB per GPU** |
| Available KV cache | 18.28 GiB            |
| Total GPU memory   | 31.87 GiB × 2        |
| GPU utilization    | 98% (pre-allocated)  |

### Context Length Tests

| Context Tokens | Response Time | Prefill Speed | Result                        |
| -------------- | ------------- | ------------- | ----------------------------- |
| 119,403        | 1.3s (cached) | 91K tok/s     | ✓ Correct                     |
| 128,512        | 4.5s          | 28.6K tok/s   | ✓ Correct                     |
| 130,338        | 1.3s (cached) | 100K tok/s    | ✓ Correct                     |
| 130,945        | —             | —             | ✓ Near max (131,072 - output) |

### Key Findings

- **AWQ reduces weights from 28.5 GiB to 8.5 GiB per GPU** (3.4× reduction)
- **131K context works reliably** with room for 127 output tokens at max input
- Prefill speed: 28-100K tokens/sec (higher when cached)
- Needle-in-haystack recall: 100% accuracy at all tested lengths

### Test Command

```bash
./experimental/local-llm/test_long_context.py 2150  # ~130K tokens
```

## Ollama Quick Start

```bash
# Pull the model (default Q4_K_M)
ollama pull qwen3-coder:30b

# Run with extended context
ollama run qwen3-coder:30b --num-ctx 131072

# Or create a Modelfile for persistent settings
cat > Modelfile << 'EOF'
FROM qwen3-coder:30b-a3b-q4_K_M
PARAMETER num_ctx 131072
EOF
ollama create qwen3-coder-long -f Modelfile
```

## vLLM Memory Debugging Options

For detailed memory visibility when troubleshooting, vLLM provides several environment variables:

### Enable Verbose Memory Logging

```bash
# Full debug logging with memory breakdown
VLLM_LOGGING_LEVEL=DEBUG \
VLLM_LOG_MODEL_INSPECTION=1 \
docker run --rm --name vllm \
  --gpus all \
  -v /wyrmhdd/huggingface:/root/.cache/huggingface \
  -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen3-Coder-30B-A3B-Instruct \
  --tensor-parallel-size 2 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.95 \
  2>&1 | tee vllm-debug.log
```

### Key Environment Variables

| Variable                             | Purpose                                       |
| ------------------------------------ | --------------------------------------------- |
| `VLLM_LOGGING_LEVEL=DEBUG`           | Full debug logging including memory snapshots |
| `VLLM_LOG_MODEL_INSPECTION=1`        | Show model structure with layer types         |
| `VLLM_GC_DEBUG=1`                    | Track garbage collection timing               |
| `VLLM_GC_DEBUG='{"top_objects":10}'` | Show top 10 collected object types            |

### Memory Breakdown in Logs

Look for this output after model loading:

```text
Actual usage:
  - X GiB for weights
  - X GiB for peak activation
  - X GiB for non-torch memory
  - X GiB for CUDAGraph memory
Available KV cache memory: X GiB
```

### Per-Layer Profiling (PyTorch Profiler)

For detailed per-operation memory tracking:

```bash
mkdir -p /tmp/torch_profiles
VLLM_TORCH_PROFILER_DIR=/tmp/torch_profiles \
VLLM_TORCH_PROFILER_WITH_PROFILE_MEMORY=1 \
docker run ...
```

Then analyze `/tmp/torch_profiles/*.pt` with PyTorch's profiler tools.

## Verified Working Setup (2026-01-25)

### Hardware (verified setup)

- 2x RTX 5090 (32 GB each, 64 GB total)
- No GPU P2P (VM passthrough or PCIe without NVLink)

### What Works: AWQ 4-bit + FP8 KV Cache + max-num-seqs Fix

**262K context working reliably** with this exact configuration:

```bash
docker run -d --name vllm \
    --gpus all \
    -v /wyrmhdd/huggingface:/root/.cache/huggingface \
    -p 8000:8000 \
    vllm/vllm-openai:latest \
    --model cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit \
    --served-model-name qwen3-coder-awq \
    --tensor-parallel-size 2 \
    --max-model-len 262144 \
    --kv-cache-dtype fp8 \
    --gpu-memory-utilization 0.90 \
    --max-num-seqs 32 \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder
```

**Memory breakdown:**

- Model weights: **8.52 GiB per GPU** (AWQ 4-bit)
- Total usage: **~30.7 GiB per GPU** (94% of 32 GB)
- Max concurrency: **3.21x** for 262K context

### Critical Fix: --max-num-seqs 32

**The default `max-num-seqs=256` causes OOM during warmup.**

vLLM warms up the sampler with `max-num-seqs` dummy requests. With 256 sequences at 262K context, this exceeds memory. Lowering to 32 fixes it.

Error without fix:

```text
RuntimeError: CUDA out of memory occurred when warming up sampler with 256 dummy requests.
Please try lowering `max_num_seqs` or `gpu_memory_utilization` when initializing the engine.
```

### Binary Search Results (FP8 KV Cache)

| Context | Concurrency | Status   |
| ------- | ----------- | -------- |
| 134K    | 6.28x       | ✅ Works |
| 160K    | 5.14x       | ✅ Works |
| 196K    | 4.20x       | ✅ Works |
| 230K    | 3.58x       | ✅ Works |
| 250K    | 3.29x       | ✅ Works |
| 262K    | 3.21x       | ✅ Works |

### What Does NOT Work

1. **bf16 weights (28.5 GiB/GPU)** - No room for KV cache on 32 GB GPUs
2. **Default max-num-seqs (256)** - OOM during warmup at high context
3. **`--quantization awq` flag** - Model uses `compressed-tensors` format, auto-detected
4. **FP16 KV cache at 262K** - Not enough memory, use FP8

### OpenCode Configuration

Add to `~/.config/opencode/opencode.json`:

```json
{
  "provider": {
    "vllm": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "vLLM (local, tensor parallel)",
      "options": {
        "baseURL": "http://0.0.0.0:8000/v1"
      },
      "models": {
        "qwen3-coder-awq": {
          "name": "Qwen3-Coder 30B AWQ (vLLM)",
          "reasoning": true,
          "tool_call": true,
          "interleaved": {
            "field": "reasoning_content"
          },
          "limit": {
            "context": 262144,
            "output": 8192
          }
        }
      }
    }
  }
}
```

**Important:** Use `0.0.0.0` not `localhost` for baseURL when vLLM runs in Docker.

### Start Script

Use the provided script which includes all fixes:

```bash
~/code/ducktape/experimental/local-llm/start-vllm-awq.sh
```

Environment variables for customization:

- `MAX_MODEL_LEN=131072` - Lower context if needed
- `MAX_NUM_SEQS=64` - Increase concurrent sequences
- `KV_CACHE_DTYPE=fp16` - Use FP16 KV (halves max context)
- `GPU_MEM_UTIL=0.95` - Increase memory utilization (risky)

## References

- [Qwen3-Coder GitHub](https://github.com/QwenLM/Qwen3-Coder)
- [Qwen3-Coder Blog Post](https://qwenlm.github.io/blog/qwen3-coder/)
- [Unsloth Qwen3-Coder-30B GGUF](https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF)
- [Unsloth Qwen3-Coder-480B GGUF](https://huggingface.co/unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF)
- [Ollama qwen3-coder](https://ollama.com/library/qwen3-coder)
- [Unsloth Dynamic 2.0 Quantization Docs](https://unsloth.ai/docs/models/qwen3-coder-how-to-run-locally)
