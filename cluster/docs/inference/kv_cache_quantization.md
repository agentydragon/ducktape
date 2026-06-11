# KV Cache Quantization Research: Performance and Quality Tradeoffs

This document summarizes research on the performance and quality implications of KV cache quantization in LLM inference.

## Executive Summary

| Precision | Memory Reduction | Accuracy Impact                            | Recommended For                                    |
| --------- | ---------------- | ------------------------------------------ | -------------------------------------------------- |
| FP8       | 2x vs FP16       | <1% perplexity loss                        | Production inference, best quality/memory tradeoff |
| INT8      | 2x vs FP16       | <0.1-0.5 perplexity loss (model-dependent) | Production, some models sensitive                  |
| INT4/Q4   | 4x vs FP16       | 0.2-0.5 perplexity loss                    | Memory-constrained setups                          |
| NVFP4     | 4x vs FP16       | <1% accuracy loss                          | Blackwell GPUs only                                |
| 3-bit     | 4.8x vs FP16     | <0.1 perplexity (KVQuant)                  | Extreme memory constraints                         |
| 2-bit     | 8x vs FP16       | Up to 2% accuracy drop                     | Not recommended for production                     |

## Detailed Benchmark Results

### FP8 vs FP16 KV Cache

**Key Finding:** FP8 KV cache shows **negligible accuracy impact** - differences are typically within measurement noise.

| Source                                                                                                                         | Model       | Finding                                                                                                                                                                            |
| ------------------------------------------------------------------------------------------------------------------------------ | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Baseten](https://www.baseten.co/blog/33-faster-llm-inference-with-fp8-quantization/)                                          | Mistral 7B  | "FP8 quantization shows comparable perplexity to FP16 — in fact some benchmark runs showed FP8 at a lower perplexity which indicates that these slight differences are just noise" |
| [NVIDIA](https://developer.nvidia.com/blog/optimizing-inference-for-long-context-and-large-batch-sizes-with-nvfp4-kv-cache)    | Llama 2 70B | "No significant change in perplexity (less than 1%) compared to FP16 on WikiText"                                                                                                  |
| [NVIDIA Blog](https://developer.nvidia.com/blog/optimizing-llms-for-performance-and-accuracy-with-post-training-quantization/) | Multiple    | "For KV cache on Hopper & Ada GPUs, FP8 KV cache over INT8 because the former has a lower accuracy impact than the latter in most tested cases"                                    |

**Memory Benefits:**

- ~2x memory reduction for KV cache
- Enables 2-3x larger batch sizes on H100 machines
- [vLLM](https://docs.vllm.ai/en/latest/features/quantization/quantized_kvcache/): "Approximately double the amount of space for KV cache allocation"

### INT8 KV Cache

**Key Finding:** INT8 works well for most models but has higher variance than FP8.

| Source                                                                                | Model      | Result                                                                                           |
| ------------------------------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------ |
| [GPU-Accelerated INT8](https://arxiv.org/abs/2601.04719)                              | Multiple   | "4x memory reduction with reconstruction error below 0.004 and attention score error below 0.1"  |
| [LMDeploy](https://lmdeploy.readthedocs.io/en/latest/quantization/kv_quant.html)      | Llama2-7B  | "KV int8 quantization has almost lossless accuracy"                                              |
| [ATOM](https://homes.cs.washington.edu/~arvind/papers/atom-mlsys.pdf)                 | Various    | "Quantizing KV-cache results in a slight 0.12 perplexity increase on WikiText2"                  |
| [Baseten](https://www.baseten.co/blog/33-faster-llm-inference-with-fp8-quantization/) | Mistral 7B | "INT8 with SmoothQuant was nearly double the FP16 baseline perplexity" (unusable for this model) |

**Recommendation:** FP8 is generally preferred over INT8 for KV cache due to better dynamic range handling.

### INT4/Q4_0 KV Cache

**Key Finding:** Usable with some quality degradation; effectiveness is model-dependent.

| Source                                                                                  | Model             | Result                                                                                                     |
| --------------------------------------------------------------------------------------- | ----------------- | ---------------------------------------------------------------------------------------------------------- |
| [LMDeploy](https://lmdeploy.readthedocs.io/en/latest/quantization/kv_quant.html)        | Llama2-7B         | "KV int4 quantization accuracy is within an acceptable range. RPS improved by around 40% compared to fp16" |
| [HuggingFace](https://huggingface.co/blog/kv-cache-quantization)                        | Llama2-7b-chat    | "int4 cache performs almost the same as the original fp16 precision for both quanto and HQQ backends"      |
| [llama.cpp discussion](https://github.com/ggml-org/llama.cpp/discussions/5932)          | Various           | "Q4_0 added around 0.206-0.25~ perplexity to the model"                                                    |
| [Ollama blog](https://smcleod.net/2024/12/bringing-k/v-context-quantisation-to-ollama/) | Qwen 2.5 Coder 7B | "Q8_0: +0.0043 perplexity, Q4_0: noticeable quality reduction but usable"                                  |

### NVFP4 (NVIDIA Blackwell)

**Key Finding:** Newest 4-bit format achieves <1% accuracy loss with native hardware support.

[NVIDIA Blog](https://developer.nvidia.com/blog/optimizing-inference-for-long-context-and-large-batch-sizes-with-nvfp4-kv-cache):

- **<1% accuracy loss** on LiveCodeBench, MMLU-PRO, MBPP, and Ruler 64K
- 50% memory reduction compared to FP8
- Enables doubling of context length and batch size
- "5% higher accuracy when the KV cache is in NVFP4 versus MXFP4" for Llama 3.3 70B

### Ultra-Low Bit (3-bit and 2-bit)

#### 3-bit (KVQuant)

[KVQuant (NeurIPS 2024)](https://arxiv.org/abs/2401.18079):

| Model              | Dataset         | Perplexity Degradation                    |
| ------------------ | --------------- | ----------------------------------------- |
| LLaMA-7B           | Wikitext-2      | \<0.1 (within 0.07 of FP16 baseline)      |
| All LLaMA variants | Wikitext-2, C4  | \<0.1                                     |
| LLaMA-7B           | RULER benchmark | 14% better than KIVI at similar bit-width |

**Memory savings:** 4.8x reduction in cached activation memory footprint.

#### 2-bit (KIVI)

[KIVI (ICML 2024)](https://arxiv.org/abs/2402.02750):

- **For Llama and Mistral models:** Up to 2% accuracy drop
- Enables 2.6x less peak memory usage
- **LongBench:** KIVI-2 average 44.27 vs 16-bit 44.52 (Llama2-7B)
- **Caution:** Later research (KITTY) found KIVI-K2V2 "consistently yields significantly lower accuracy than the FP16 baseline across all evaluated tasks"

## Model/Architecture Robustness

### Model Size Effect

[NQKV Research](https://arxiv.org/html/2505.16210v1):

> "As the model size increases, the robustness of large language models also improves."

Larger models are generally more robust to KV cache quantization.

### Qwen3 vs LLaMA3

[Qwen3 Quantization Study](https://arxiv.org/html/2505.02214v1):

> "Compared to prior results on LLaMA3, Qwen3 exhibits more pronounced performance degradation under low-bit quantization (3 bits or fewer)."

**Reason:** Qwen3's advanced pre-training produces less parameter redundancy, making it more sensitive to quantization.

**Finding:** Qwen3-14B retained higher performance than smaller variants, confirming model size effect.

### GQA/MQA Impact

Models using aggressive Grouped Query Attention (GQA) may be more sensitive to KV cache quantization:

[Ollama blog](https://smcleod.net/2024/12/bringing-k/v-context-quantisation-to-ollama/):

> "Qwen2 uses 8x GQA, and a heavily quantized KV cache may hurt it more than a model with less aggressive GQA."

### Key vs Value Cache Sensitivity

**Keys are significantly more sensitive to quantization than values.**

[KVLinC](https://arxiv.org/html/2510.05373):

> "The Key cache generally exhibits higher norms than the Value cache, making it more sensitive to uniform quantization."

[Llama3.3-70B experiments](https://arxiv.org/html/2502.15075v1):

- K(4bit)V(2bit) **significantly outperforms** K(2bit)V(4bit) on GSM8K

**Recommendation:** When using mixed precision, allocate more bits to keys than values.

## Calibration Techniques

### Per-Channel vs Per-Token Quantization

[KIVI](https://arxiv.org/abs/2402.02750):

- **Keys:** Quantize per-channel (outliers concentrate in specific channels)
- **Values:** Quantize per-token (no fixed outlier pattern)

### Pre-RoPE Key Quantization

[KVQuant](https://arxiv.org/abs/2401.18079):

> "Quantizing Keys pre-RoPE further improves perplexity by 0.82 for 3-bit LLaMA-7B, as it mitigates the disruptive effect of RoPE on channel magnitudes."

**Why it works:** RoPE applies rotations that mix pairs of channels with different magnitudes, disrupting outlier patterns.

### Offline vs Online Calibration

[KVQuant](https://github.com/SqueezeAILab/KVQuant):

> "It is possible to calibrate offline for scaling factors, thereby avoiding expensive online recomputation."

Offline calibration works well for per-channel quantization of keys.

### Scale Factor Precision

[NVIDIA Blog](https://developer.nvidia.com/blog/optimizing-inference-for-long-context-and-large-batch-sizes-with-nvfp4-kv-cache):

> "NVFP4's more granular block scaling and higher precision E4M3 FP8 scaling factors together allow for lower quantization error during the dequantization step."

### Outlier Handling

[KVQuant](https://arxiv.org/abs/2401.18079):

> "Removing just 1% of numerical outliers using per-vector thresholds leads to an additional 0.19 perplexity reduction for 3-bit LLaMA-7B."

Techniques like Dense-and-Sparse quantization isolate outliers for separate handling.

### Advanced Methods

| Method                                                 | Technique                      | Benefit                                       |
| ------------------------------------------------------ | ------------------------------ | --------------------------------------------- |
| [KVQuant](https://arxiv.org/abs/2401.18079)            | Non-Uniform Quantization (NUQ) | Better represents non-uniform activations     |
| [QuaRot, SpinQuant](https://arxiv.org/html/2510.05373) | Hadamard transforms            | Improves quantization robustness              |
| [ResQ](https://arxiv.org/html/2510.05373)              | Preserve salient channels      | Higher precision for outlier channels         |
| [GEAR](https://arxiv.org/html/2510.05373)              | Low-rank error approximation   | Maintains approximation of quantization error |
| [XQuant](https://arxiv.org/html/2510.11236)            | Data-free calibration          | No calibration data needed                    |

## Throughput and Latency Impact

### Throughput Improvements

| Source                                                                           | Configuration | Improvement                              |
| -------------------------------------------------------------------------------- | ------------- | ---------------------------------------- |
| [KIVI](https://arxiv.org/abs/2402.02750)                                         | 2-bit KV      | 2.35x-3.47x throughput on real workloads |
| [ATOM](https://homes.cs.washington.edu/~arvind/papers/atom-mlsys.pdf)            | INT4 KV       | Up to 7.7x higher throughput vs FP16     |
| [ATOM](https://homes.cs.washington.edu/~arvind/papers/atom-mlsys.pdf)            | Batch 128     | 1.8x speedup over INT8, 3.5x over FP16   |
| [KVQuant](https://arxiv.org/abs/2401.18079)                                      | 4-bit         | ~1.7x speedup for LLaMA-7B               |
| [LMDeploy](https://lmdeploy.readthedocs.io/en/latest/quantization/kv_quant.html) | INT4          | 40% RPS improvement vs FP16              |

### Latency Considerations

[vLLM vs TensorRT-LLM comparison](https://blog.squeezebits.com/vllm-vs-tensorrtllm-8-kv-cache-quantization-35079):

> "For vLLM, FP8 KV cache did not improve throughput; in fact, it slightly degraded throughput in prefill-heavy scenarios."

**Note:** Some implementations lack fused dequantization + attention kernels, limiting latency benefits.

[TensorRT-LLM](https://blog.squeezebits.com/vllm-vs-tensorrtllm-8-kv-cache-quantization-35079):

> "KV cache quantization provided up to 1.09x and 1.45x throughput improvement at prefill-heavy and decode-heavy scenarios, respectively."

## Practical Recommendations

### For Production Inference

1. **Start with FP8** - best quality/memory tradeoff, nearly lossless
2. **Fall back to INT8** if FP8 not supported, but verify model quality
3. **Consider INT4** only for memory-constrained setups with acceptable quality loss

### For Long Context Inference

1. **Use KVQuant techniques** for 3-bit quantization with <0.1 perplexity loss
2. **Pre-RoPE key quantization** is essential for accuracy
3. **Asymmetric K/V precision** (more bits for keys) helps

### Model Selection

1. **Larger models** are more robust to KV quantization
2. **LLaMA3** appears more robust than **Qwen3** at ultra-low bit-widths
3. **Avoid aggressive GQA models** if using heavy KV quantization

### Backend Selection (vLLM)

[vLLM docs](https://docs.vllm.ai/en/latest/features/quantization/quantized_kvcache/):

> "FlashAttention-2 does not support quantized KV cache. Use FlashInfer backend instead."

### Calibration

1. Use [LLM Compressor](https://github.com/vllm-project/llm-compressor) for calibrated FP8 scales
2. Offline calibration works for per-channel key quantization
3. Consider representative inference data for scale tuning

## Summary Table: Method Comparison

| Method                                                                                                                     | Precision | Per-channel   | Pre-RoPE | Outlier Handling  | Best Result             |
| -------------------------------------------------------------------------------------------------------------------------- | --------- | ------------- | -------- | ----------------- | ----------------------- |
| [KVQuant](https://arxiv.org/abs/2401.18079)                                                                                | 3-bit     | Yes (Keys)    | Yes      | Dense-Sparse      | <0.1 ppl degradation    |
| [KIVI](https://arxiv.org/abs/2402.02750)                                                                                   | 2-bit     | Yes (Keys)    | No       | No                | ~2% accuracy drop       |
| [NVFP4](https://developer.nvidia.com/blog/optimizing-inference-for-long-context-and-large-batch-sizes-with-nvfp4-kv-cache) | 4-bit     | Block scaling | -        | Hardware-native   | <1% accuracy loss       |
| [ATOM](https://homes.cs.washington.edu/~arvind/papers/atom-mlsys.pdf)                                                      | INT4      | Mixed         | No       | Indirect indexing | +0.12 ppl on WikiText2  |
| [Oaken](https://dl.acm.org/doi/10.1145/3695053.3731019)                                                                    | Mixed     | Hybrid        | No       | Online-offline    | 0.87% avg accuracy loss |

## References

- [vLLM Quantized KV Cache Documentation](https://docs.vllm.ai/en/latest/features/quantization/quantized_kvcache/)
- [HuggingFace KV Cache Quantization Blog](https://huggingface.co/blog/kv-cache-quantization)
- [NVIDIA NVFP4 KV Cache Blog](https://developer.nvidia.com/blog/optimizing-inference-for-long-context-and-large-batch-sizes-with-nvfp4-kv-cache)
- [KVQuant Paper (NeurIPS 2024)](https://arxiv.org/abs/2401.18079)
- [KIVI Paper (ICML 2024)](https://arxiv.org/abs/2402.02750)
- [ATOM Paper (MLSys 2024)](https://homes.cs.washington.edu/~arvind/papers/atom-mlsys.pdf)
- [Baseten FP8 Quantization Blog](https://www.baseten.co/blog/33-faster-llm-inference-with-fp8-quantization/)
- [llama.cpp 4-bit KV Cache Discussion](https://github.com/ggml-org/llama.cpp/discussions/5932)
- [Ollama K/V Context Quantisation](https://smcleod.net/2024/12/bringing-k/v-context-quantisation-to-ollama/)
