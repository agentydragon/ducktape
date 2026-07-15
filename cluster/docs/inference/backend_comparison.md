# Inference engine comparison

Evaluating options for the cluster's GPU inference path. See <README.md> for
the wider docs hub.

## Current state (2026-04-28)

| Aspect          | Value                                                                                                                 |
| --------------- | --------------------------------------------------------------------------------------------------------------------- |
| Backend         | Ollama (single Deployment, <../../k8s/ollama/app/deployment.yaml>)                                                    |
| Node            | wyrm2 (2× RTX 5090, 32 GB each, 64 GB total VRAM, **no GPU P2P** — VM passthrough)                                    |
| Model storage   | PVC `llm-models` (200 Gi, **`lvm-proxmox-hdd`** — OpenEBS LVM on wyrm2 HDD, on-node) at `/models`                     |
| Auth            | nginx sidecar bearer-token proxy                                                                                      |
| **Format**      | **GGUF** — all weights are GGUF                                                                                       |
| Models loaded   | `gpt-oss:20b` (MXFP4-in-GGUF, 13.8 GB), `gpt-oss:120b` (MXFP4-in-GGUF, 65.4 GB), `gemma4:31b-it-q8_0` (Q8_0, 33.8 GB) |
| KV cache        | `q8_0` (`OLLAMA_KV_CACHE_TYPE=q8_0`)                                                                                  |
| Context         | `OLLAMA_NUM_CTX=131072`                                                                                               |
| Flash attention | enabled (`OLLAMA_FLASH_ATTENTION=1`)                                                                                  |
| Tensor parallel | **none** (Ollama only does sequential layer split)                                                                    |

The MXFP4 weights inside the gpt-oss GGUFs are _not_ exercised as native
Blackwell FP4 — Ollama/llama.cpp dequantize them into compute kernels that
don't hit the 5090's FP4 tensor cores. Real perf gap on this hardware.

## Migration path

Add a second PVC with **Blackwell-native formats** (HF safetensors / native
MXFP4 / FP8 / AWQ) for vLLM, run alongside the existing GGUF PVC during
cutover, deprecate the GGUF PVC once vLLM proves out. See <vllm_history.md>
for the prior wyrm2-host vLLM work that informs configuration choices.

### Storage class

Stay on **`lvm-proxmox-hdd`** — these are big sequential blobs, weights
land in page cache after first load, and we have plenty of HDD vs limited
NVMe. The cold-start tax (~5 min vs ~30 s) is a one-time hit per model.

Use a **single PVC with subdirs** (`/models/gguf/`, `/models/safetensors/`,
`/models/awq/`) instead of a PVC per format. Per-format PVCs punish models
that exist in two formats (where do they live?) without delivering much
over subdirs. May resize the existing `llm-models` PVC upward as we add
non-GGUF formats.

## Requirements (in priority order)

1. **Tensor-parallel sharding** across 2× 5090 (so we can run a single large
   model with throughput, not just lay layers across GPUs sequentially).
2. **OpenAI Responses API** (`/v1/responses`), ideally **stateful** — server-
   side conversation memory, server-side tool execution, persisted reasoning.
   Most engines only ship the surface shim.
3. **Anthropic Messages API** (`/v1/messages`), native if possible.
4. **Reasoning-model support** — harmony format parsing for gpt-oss,
   `<think>` for DeepSeek-R1/QwQ/Qwen3, `reasoning_content` field on
   responses, separate streaming channel for thinking, `reasoning_effort`
   as a request param.
5. **Quantization** — GGUF, AWQ, GPTQ, FP8, MXFP4, INT4.
6. **K8s-friendly** — Helm chart or simple Deployment, health probes,
   Prometheus metrics.
7. **Day-0 model support** for new architectures.

## Headline matrix

| Engine                         | TP across 2 GPUs                                                                                    | `/v1/responses` (stateful?)                                                                                                                                                               | `/v1/messages` (Anthropic)                     | Harmony / `<think>`                                                     | Quants                                        | K8s                                              | Day-0 models                      |
| ------------------------------ | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- | ----------------------------------------------------------------------- | --------------------------------------------- | ------------------------------------------------ | --------------------------------- |
| **llama.cpp** (`llama-server`) | yes — `--split-mode row --tensor-split 1,1`                                                         | shim only (rewrites to chat)                                                                                                                                                              | **native**                                     | both, via `--jinja` + `--reasoning-format deepseek`                     | GGUF only                                     | manual Deployment                                | usually first                     |
| **Ollama**                     | no — sequential layer split only                                                                    | shim, **non-stateful only**                                                                                                                                                               | no                                             | both, automatic; `"think": "low\|medium\|high"` first-class for gpt-oss | GGUF (uses llama.cpp)                         | community Helm; we run raw manifests             | trails llama.cpp                  |
| **vLLM**                       | yes — `--tensor-parallel-size 2`                                                                    | maturing; `previous_response_id` chaining, harmony, MCP/built-in tools; full store/file_search/computer_use tracked under [RFC #24603](https://github.com/vllm-project/vllm/issues/24603) | no                                             | both, `reasoning_content`, `reasoning_effort`                           | AWQ, GPTQ, FP8, INT4, MXFP4                   | official Helm chart, Ray multi-GPU, Prom metrics | yes                               |
| **SGLang**                     | yes, mature; better TTFT stability than vLLM at moderate concurrency on gpt-oss-120B (2×H100 bench) | shim, less complete than vLLM, no store                                                                                                                                                   | no                                             | both                                                                    | FP8, AWQ, GPTQ, MXFP4                         | Docker image, community Helm                     | yes                               |
| **TensorRT-LLM / NIM**         | yes, plus expert/pipeline parallel                                                                  | "OpenAI Responses Client" exists, minimal stateful                                                                                                                                        | no                                             | yes (`openai_gptoss` parser, `reasoning_effort`)                        | FP8, FP4 (Blackwell), AWQ                     | NIM Operator + Helm                              | yes (PyTorch backend)             |
| **TGI** (HuggingFace)          | yes (sharded)                                                                                       | no                                                                                                                                                                                        | no (HF "Messages API" is not Anthropic-shaped) | trails                                                                  | yes                                           | Helm exists                                      | de-emphasized in 2025             |
| **Aphrodite** (vLLM fork)      | yes (Megatron-LM)                                                                                   | no                                                                                                                                                                                        | no                                             | partial                                                                 | very broad: EXL2/3, GGUF read, AWQ, GPTQ, FP8 | container only                                   | follows vLLM                      |
| **LMDeploy** (InternLM)        | yes; ~1.8× vLLM throughput on some workloads                                                        | no                                                                                                                                                                                        | no                                             | partial                                                                 | yes                                           | Docker, no first-party Helm                      | lags on novel arches              |
| **mistral.rs** (Rust)          | yes, automatic                                                                                      | experimental                                                                                                                                                                              | no                                             | partial harmony                                                         | ISQ, GGUF, AWQ                                | thin Docker, no Helm                             | follows                           |
| **TabbyAPI + ExLlamaV3**       | experimental in exllamav3, **not yet in TabbyAPI**                                                  | no                                                                                                                                                                                        | no                                             | PR-stage                                                                | EXL3 (best accuracy/bpw curve)                | community images                                 | no                                |
| **MLC LLM** (TVM)              | yes, less battle-tested                                                                             | no                                                                                                                                                                                        | no                                             | n/a                                                                     | yes                                           | Docker                                           | niche                             |
| **KTransformers**              | expert offload, not classic TP                                                                      | no                                                                                                                                                                                        | no                                             | basic                                                                   | mixed                                         | Docker                                           | niche; only matters if VRAM short |
| **Colibri**                    | GPU/RAM hot expert tiers, not classic TP                                                            | no                                                                                                                                                                                        | no                                             | GLM-5.2 native                                                          | custom INT4 experts + INT8 MTP head           | host CLI/server, no Helm                         | purpose-built for GLM-5.2         |

## Format / quantization compatibility

Legend: ✅ first-class · 🟡 experimental or partial · ❌ no · — n/a

| Engine                   | HF safetensors (fp16/bf16) | GGUF      | AWQ        | GPTQ | EXL2 / EXL3      | FP8 (e4m3 / e5m2) | MXFP4 / FP4                      | BitsAndBytes | TRT engine (`.engine`) |
| ------------------------ | -------------------------- | --------- | ---------- | ---- | ---------------- | ----------------- | -------------------------------- | ------------ | ---------------------- |
| **llama.cpp**            | ❌ (convert to GGUF)       | ✅        | ❌         | ❌   | ❌               | ❌                | ❌                               | ❌           | ❌                     |
| **Ollama**               | ❌                         | ✅        | ❌         | ❌   | ❌               | ❌                | ✅ (gpt-oss only, via llama.cpp) | ❌           | ❌                     |
| **vLLM**                 | ✅                         | 🟡        | ✅         | ✅   | ❌               | ✅                | ✅ (gpt-oss MXFP4)               | ✅           | ❌                     |
| **SGLang**               | ✅                         | 🟡        | ✅         | ✅   | ❌               | ✅                | ✅ (gpt-oss MXFP4)               | 🟡           | ❌                     |
| **TensorRT-LLM / NIM**   | ✅ (compiled to engine)    | ❌        | ✅         | 🟡   | ❌               | ✅ (Hopper+)      | ✅ (Blackwell FP4)               | ❌           | ✅ (native)            |
| **TGI**                  | ✅                         | 🟡        | ✅         | ✅   | ❌               | ✅                | 🟡                               | ✅           | ❌                     |
| **Aphrodite**            | ✅                         | ✅ (read) | ✅         | ✅   | ✅ (EXL2/3)      | ✅                | 🟡                               | ✅           | ❌                     |
| **LMDeploy**             | ✅                         | ❌        | ✅ (W4A16) | 🟡   | ❌               | ✅                | 🟡                               | ❌           | ❌                     |
| **mistral.rs**           | ✅                         | ✅        | ✅         | ❌   | ❌               | 🟡                | ❌                               | ❌           | ❌                     |
| **TabbyAPI + ExLlamaV3** | ❌                         | ❌        | ❌         | ❌   | ✅ (EXL3 native) | ❌                | ❌                               | ❌           | ❌                     |
| **MLC LLM**              | ✅ (compiled to TVM)       | ❌        | 🟡         | 🟡   | ❌               | 🟡                | ❌                               | ❌           | ❌                     |
| **KTransformers**        | ✅                         | ✅        | ✅         | ✅   | ❌               | 🟡                | ✅ (gpt-oss MoE offload)         | ❌           | ❌                     |

### Practical implications for our 2× 5090 setup

- **Blackwell native FP4 (MXFP4)** — gpt-oss ships in MXFP4 and 5090s have
  native FP4 tensor cores. vLLM, SGLang, and TensorRT-LLM all support this;
  llama.cpp/Ollama only access it via the GGUF MXFP4 conversion (no native
  FP4 kernels). Real perf gap on this hardware.
- **FP8** — Hopper+ feature, supported on 5090. vLLM/SGLang/TRT-LLM use it
  natively; llama.cpp/Ollama do not.
- **GGUF lock-in** — our existing weights on the PVC are GGUF. Migrating to
  vLLM/SGLang means re-downloading native HF safetensors or AWQ/FP8/MXFP4
  variants. vLLM/SGLang can read GGUF experimentally but slower than native
  formats — not recommended for production.
- **EXL3** is the accuracy/bpw leader for dense models but locked to TabbyAPI;
  not relevant for our MoE-heavy reasoning targets (gpt-oss, DeepSeek-R1).

## Why our two finalists fall short

**llama.cpp** — has `--split-mode row` true tensor parallel and native
Anthropic Messages, but `reasoning_effort` has to ride in the system prompt
(no first-class request param), the harmony parser has had a steady drip of
edge-case bugs through 2026 ([#20281](https://github.com/ggml-org/llama.cpp/issues/20281),
[#20650](https://github.com/ggml-org/llama.cpp/issues/20650),
[#19814](https://github.com/ggml-org/llama.cpp/issues/19814)), no Helm
chart, one process per model, GGUF-only.

**Ollama** — turnkey for gpt-oss with `"think": "low|medium|high"` as a
real request field, but **no tensor parallel** (layers laid sequentially —
bandwidth-bound on 2× 5090), no Anthropic endpoint, Responses API non-
stateful, no `/healthz`, Ollama-flavored `/api/*` is the primary surface
and OpenAI compat is secondary.

## Other options worth considering

**vLLM** — the default for serious open-weights serving. Tensor-parallel,
Helm chart, harmony, `reasoning_content`, the most-developed `/v1/responses`
surface (still not fully stateful). Fits gpt-oss-120B-MXFP4 on 2× 5090
with TP=2. ([gpt-oss recipe](https://docs.vllm.ai/projects/recipes/en/latest/OpenAI/GPT-OSS.html))

**SGLang** — RadixAttention prefix cache; latency wins on agent workloads
with long shared system prompts. Pick over vLLM if our agent traffic is
prefix-cache-heavy. ([gpt-oss-120B benchmark](https://www.clarifai.com/blog/comparing-sglang-vllm-and-tensorrt-llm-with-gpt-oss-120b))

**TensorRT-LLM / NIM** — fastest single-stream on Blackwell, but opinionated
build step and NVIDIA's tooling. NIM containers give an OpenAI REST surface
with `reasoning_effort` via `openai_gptoss` parser. Pick if max throughput
matters more than hackability.

**Colibri** — specialized GLM-5.2 runtime that streams cold MoE experts from
disk and places hot experts in RAM and across the GPUs without requiring P2P.
Its OpenAI chat server supports authentication, streaming, queues, and tool calls,
but not the Responses or Anthropic APIs. The wyrm2 experiment reached 0.28 tok/s
at full quality and 0.37 tok/s with approximate expert top-p 0.7, so retain it as
a reproducible experiment rather than a cluster backend. See
<runs/2026-07-14_glm52_colibri/README.md>.

**LiteLLM as a gateway** — independent of the engine choice, LiteLLM in
front of vLLM/SGLang would synthesize a real Anthropic `/v1/messages`
endpoint and paper over Responses-statefulness gaps with its own store.
This is probably the cleanest way to get all three APIs without picking
based on API surface alone.

## Tier list for our cluster

1. **vLLM** + LiteLLM gateway — default recommendation. TP across both
   5090s, day-0 model support, official Helm, all three APIs (Anthropic
   and stateful Responses synthesized at the gateway).
2. **SGLang** + LiteLLM — switch in if vLLM TTFT jitter is the bottleneck
   for agent traffic.
3. **llama.cpp** — keep as a side option for Anthropic-native serving and
   GGUF-only models that haven't landed in vLLM yet.
4. **Ollama** — current setup; keep until we cut over. No good reason to
   stay long-term given the no-TP ceiling.
5. **TensorRT-LLM / NIM** — only if benchmarks justify the operator complexity.
6. **Colibri** — retain the reproducible GLM-5.2 experiment, but do not deploy
   behind LiteLLM at the measured sub-0.5 tok/s throughput.
7. **Aphrodite, LMDeploy, mistral.rs, TabbyAPI, MLC, KTransformers, TGI** — skip.

## Open questions

- Does LiteLLM's `/v1/messages` translation cover tool-use round-trips
  cleanly for gpt-oss reasoning, or does it lose the analysis-channel
  context across tool rounds?
- gpt-oss-120B MXFP4 footprint on 2× 5090 with TP=2: enough headroom
  for batch + KV cache at our typical context length?
- Do we want a single multi-model engine or one Deployment per model?
  vLLM is one-process-per-model; SGLang Router supports multi-model.

## Sources

- [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [Ollama OpenAI docs](https://docs.ollama.com/openai), [thinking docs](https://docs.ollama.com/capabilities/thinking.md)
- [vLLM Responses API + tools](https://deepwiki.com/vllm-project/vllm/6.5-responses-api-and-tool-calling), [RFC #24603](https://github.com/vllm-project/vllm/issues/24603)
- [Clarifai SGLang/vLLM/TRT-LLM gpt-oss-120B benchmark](https://www.clarifai.com/blog/comparing-sglang-vllm-and-tensorrt-llm-with-gpt-oss-120b)
- [TensorRT-LLM docs](https://nvidia.github.io/TensorRT-LLM/), [NIM configuration](https://docs.nvidia.com/nim/large-language-models/latest/configuration.html)
- [Aphrodite distributed inference](https://aphrodite.pygmalion.chat/usage/distributed/)
- [LMDeploy](https://github.com/InternLM/lmdeploy), [mistral.rs](https://github.com/EricLBuehler/mistral.rs), [exllamav3](https://github.com/turboderp-org/exllamav3)
