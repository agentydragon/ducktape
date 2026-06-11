# vLLM history (wyrm2 host)

Before deploying vLLM in k8s, read this. The wyrm2-host attempts in early
2026 worked but exposed several non-obvious failure modes that will recur
in the cluster.

The raw analysis lives at <qwen3_coder_vram_analysis.md> (637 lines of
memory math, debug logs, profiler output). This doc is the distilled
"what bit us, what fixed it" you need before configuring a Deployment.

## Hardware constraints that don't change

- **2× RTX 5090, 32 GB each** (64 GB total).
- **No GPU P2P** — wyrm2 is a Proxmox VM with the GPUs passed through. vLLM
  logs `WARNING: Custom allreduce is disabled because your platform lacks
GPU P2P capability`. Falls back to NCCL CPU-mediated allreduce. Latency
  hit, plus communication staging buffers eat memory. Bare metal would
  remove this; today wyrm2 is the only GPU node and it's a VM.
- **No NVLink** between the 5090s.

## What broke (Qwen3-Coder-30B-A3B)

The model that exposed every issue: `Qwen/Qwen3-Coder-30B-A3B-Instruct`.
Same gotchas will reappear with any sufficiently large dense or MoE model.

### bf16 weights don't fit, and TP=2 doesn't save you

Per-GPU breakdown at TP=2:

| Item                                          | Size                |
| --------------------------------------------- | ------------------- |
| Sharded expert weights                        | 28.99 GB            |
| Sharded attention/embed                       | 1.53 GB             |
| **Total weights/GPU**                         | **28.51 GiB**       |
| Activation peak                               | ~1.52 GiB           |
| NCCL/torch.compile/misc                       | ~0.5 GiB            |
| **Available for KV @ 0.95 util on 31.36 GiB** | **−0.27 GiB (OOM)** |

Tensor parallelism _is_ working — the math checks against the safetensors
ground truth — bf16 is just too large for 32 GB GPUs. **Lesson: budget
weights/GPU before picking a model. With no GPU P2P you also need ~0.5 GiB
of NCCL overhead per GPU.**

### AWQ 4-bit fixed it

Switching to `cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit`:

| Item                 | bf16      | AWQ 4-bit    |
| -------------------- | --------- | ------------ |
| Weights/GPU          | 28.51 GiB | **8.52 GiB** |
| Available for KV     | < 0       | **~23 GiB**  |
| Max context (FP8 KV) | n/a (OOM) | **262K**     |

That's the configuration that reliably ran 262K context with 3.21× concurrency.

### FP8 KV cache is mandatory at long context

`--kv-cache-dtype fp8` halves KV memory. At 262K context with FP16 KV the
budget doesn't fit; with FP8 it does. RTX 5090 has native FP8 tensor
cores (Hopper+), no perf penalty.

### `--max-num-seqs 32` (not the default 256)

Default `max-num-seqs=256` causes OOM during sampler warmup at long
context — vLLM allocates 256 dummy sequences at full context length.

```text
RuntimeError: CUDA out of memory occurred when warming up sampler with
256 dummy requests. Please try lowering `max_num_seqs` or
`gpu_memory_utilization` when initializing the engine.
```

32 was the empirical safe ceiling at 262K context.

### Don't pass `--quantization awq`

`cyankiwi/...-AWQ-4bit` actually ships in `compressed-tensors` format. vLLM
auto-detects it. Passing `--quantization awq` makes it fail. Let it
auto-detect; the format string in the model card is informational, not a
required flag.

## Working configuration (262K context, AWQ + FP8 KV)

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

Live host script: <../../../x/local_llm/start-vllm-awq.sh>.

## Implications for k8s deployment

When porting to `cluster/k8s/vllm/`:

1. **Pick models with native Blackwell formats** when possible — gpt-oss
   ships MXFP4 safetensors (`openai/gpt-oss-120b`); use those over the
   GGUF wrappers we currently store in the Ollama PVC. MXFP4 hits the
   5090's FP4 tensor cores; GGUF MXFP4 is dequantized.
2. **Plan VRAM with the same per-GPU formula**:
   `weights/GPU + ~1.5 GiB activations + ~0.5 GiB NCCL + KV cache budget`,
   under `gpu_memory_utilization × total_VRAM`.
3. **Always pass `--kv-cache-dtype fp8`** for long context.
4. **Always cap `--max-num-seqs`** — 32 is a safe default; raise only after
   testing warmup at the target context.
5. **Don't expect GPU P2P** until wyrm2 is bare metal. Latency penalty is
   real but tolerable; plan around the memory overhead, not around
   miraculous P2P.
6. **One Deployment per model** — vLLM is one-process-per-model. Multi-model
   serving means multiple Deployments behind one Service, or a Router
   sidecar (LiteLLM works for this).

## Known limitations of vLLM that affected our use

- **No native Anthropic Messages endpoint** — front with LiteLLM if needed.
- **Responses API still maturing** — `previous_response_id` chaining works,
  but full stateful store / file_search / computer_use tracked under
  [vLLM RFC #24603](https://github.com/vllm-project/vllm/issues/24603).
- **Qwen3-Coder has no thinking mode** — base-model property, not a
  vLLM/Ollama difference. For thinking + tool use, use plain `Qwen3-30B-A3B`
  not the Coder variant.

## See also

- <qwen3_coder_vram_analysis.md> — full memory math and debug logs
- <vllm_container_plan.md> — the home-manager systemd user-service plan
  that ran this on wyrm2
- <kv_cache_quantization.md> — KV cache dtype comparison
- <model_download_history.md> — experiment log
- <../../../x/local_llm/start-vllm-awq.sh> — live host script (262K context AWQ config)
