# Local LLM Model Search

Finding a model + backend + agent combo that works well on 2x RTX 5090 (64GB total, 32GB each).

Goal: **thinking + tool use + long context + coding**, running locally with vLLM (TP=2).

Data fetched: 2026-01-24 from HuggingFace and benchmark sources. Updated 2026-01-28.

## What We've Learned

- **Qwen3-Coder has NO thinking mode** — that's a base model property, not a quantization issue.
- **Qwen3-30B-A3B (original)** and **Qwen3-32B** support both thinking and tool use.
- **Qwen3-30B-A3B-Thinking-2507** supports thinking + tool use, but vLLM drops
  `reasoning_content` in multi-step tool calls (Qwen recommends Qwen-Agent client-side parsing).
- Ollama has no tensor parallelism (only layer splitting), so vLLM is the only
  option for real 2-GPU performance.
- vLLM's Responses API is mature (v0.10.0+, incl. function tools, MCP, streaming).
  Previous flakiness with gpt-oss was in early versions. LM Studio (v0.3.29+) and
  Ollama (v0.13.3+) also support Responses API now. See "gpt-oss Inference Backend
  Options" section for full comparison.
- **Qwen3-Coder-30B has almost no published benchmarks** beyond SWE-Bench (51.6%).
  No AIME, GPQA, Codeforces, or LiveCodeBench numbers. Tech report pending.
- Qwen3-30B-A3B-Thinking-2507 is the strongest local Qwen3 variant on reasoning
  benchmarks (AIME 85.0, GPQA 73.4, Codeforces 2044, LiveCodeBench 66.0).
- **gpt-oss-120b does NOT fit on 2x 5090.** Weights are ~65 GB on disk (MXFP4),
  exceeding 64 GB total VRAM. Community reports confirm: 2x 3090 (48 GB) OOMs,
  3x 3090 (72 GB) works. The cookbook's "≥60GB" claim refers to single-GPU setups
  like H100 80 GB where there's headroom for KV cache.
- **2x 5090 doesn't unlock a qualitatively better model tier for agentic coding
  (reasoning + tools).** The best agentic models at ~30B scale (gpt-oss-20b,
  Qwen3-Coder-30B, Qwen3-30B-A3B) all fit on one GPU. The 70B class unlocked by
  TP=2 is split: either tool calling (Llama 3.3 70B, Qwen2.5-72B) or reasoning
  (DeepSeek-R1-Distill-Llama-70B), not both. Qwen3-32B FP8 has both but its AWQ
  fits on one GPU anyway. Second GPU is most useful for running two models
  simultaneously or for extra KV cache / context length.

## Experiment Log

| #     | Model                         | Backend  | Client       | Thinking      | Tool Use   | Result                                |
| ----- | ----------------------------- | -------- | ------------ | ------------- | ---------- | ------------------------------------- |
| 1     | Qwen3-Coder-30B-A3B bf16      | vLLM     | —            | N/A           | —          | OOM (28.5 GiB/GPU, no room for KV)    |
| 2     | Qwen3-Coder-30B-A3B AWQ 4-bit | vLLM     | OpenCode     | ❌ No         | ✅ Yes     | Works (262K context), but no thinking |
| 3     | gpt-oss (small)               | vLLM     | custom agent | ✅ Yes        | ✅ Yes     | Responses API too flaky               |
| 4     | gpt-oss (small)               | Ollama   | custom agent | ✅ Yes        | ✅ Yes     | No tensor parallelism                 |
| **5** | **Qwen3-30B-A3B FP8**         | **vLLM** | **OpenCode** | **✅ Toggle** | **✅ Yes** | **TODO: Test next**                   |

## Models: Coding + Tool Calling

### Thinking + Tool Use (what we want)

| Model                           | Format    | Size     | Thinking     | Tool Use     | Context | Script                         | Downloaded |
| ------------------------------- | --------- | -------- | ------------ | ------------ | ------- | ------------------------------ | ---------- |
| **Qwen3-30B-A3B FP8**           | FP8       | ~16 GB   | ✅ Toggle    | ✅ Yes       | 131K+   | `start-vllm-qwen3-thinking.sh` | ✅         |
| Qwen3-30B-A3B-Thinking-2507-FP8 | FP8       | ~16 GB   | ✅ Always-on | ✅ (caveats) | 262K    | —                              | ✅         |
| Qwen3-32B AWQ                   | AWQ 4-bit | ~17 GB   | ✅ Toggle    | ✅ Yes       | 128K    | `start-vllm-qwen3-32b.sh`      | ✅         |
| Qwen3-32B FP8                   | FP8       | ~32.5 GB | ✅ Toggle    | ✅ Yes       | 128K    | —                              | TODO       |

Other Qwen3-Coder quantizations (no thinking, tool use only):

| Model                                      | Format | Size      | Context | Script              | Downloaded |
| ------------------------------------------ | ------ | --------- | ------- | ------------------- | ---------- |
| Qwen3-Coder-30B-A3B AWQ 4-bit (cyankiwi)   | AWQ    | 16.9 GB   | 262K    | `start-vllm-awq.sh` | ✅         |
| Qwen3-Coder-30B-A3B FP8 (official)         | FP8    | ~18-20 GB | 131K+   | —                   | ✅         |
| Qwen3-Coder-30B-A3B GPTQ-Int8              | GPTQ   | ~30 GB    | —       | —                   | TODO       |
| Qwen3-Coder-30B-A3B INT4 AutoRound (Intel) | INT4   | ~15-17 GB | —       | —                   | TODO       |

### Thinking Only (no tool use)

| Model                             | Size   | Thinking | Benchmark                              | Context | Slug                                             | Downloaded |
| --------------------------------- | ------ | -------- | -------------------------------------- | ------- | ------------------------------------------------ | ---------- |
| DeepSeek-R1-Distill-Qwen-32B AWQ  | ~17 GB | ✅ Yes   | LiveCodeBench: 57.2%, CodeForces: 1691 | 128K    | `casperhansen/deepseek-r1-distill-qwen-32b-awq`  | ✅         |
| DeepSeek-R1-Distill-Llama-70B AWQ | ~38 GB | ✅ Yes   | LiveCodeBench: 57.5% (best distilled)  | 128K    | `casperhansen/deepseek-r1-distill-llama-70b-awq` | ✅         |

### Smaller/Faster Options

| Model                      | Size    | Thinking | Context | Slug                                             |
| -------------------------- | ------- | -------- | ------- | ------------------------------------------------ |
| Qwen3-14B AWQ              | ~8 GB   | ✅ Yes   | 128K    | `Qwen/Qwen3-14B-AWQ`                             |
| DeepSeek-Coder-V2-Lite AWQ | ~10 GB  | ❌ No    | 128K    | `TechxGenus/DeepSeek-Coder-V2-Lite-Instruct-AWQ` |
| Qwen3-8B AWQ               | ~4.5 GB | ✅ Yes   | 128K    | `Qwen/Qwen3-8B-AWQ`                              |

### Does NOT Fit (Reference Only)

| Model                 | Size           | Why                                  |
| --------------------- | -------------- | ------------------------------------ |
| gpt-oss-120b          | ~65 GB (MXFP4) | Weights alone exceed 64GB total VRAM |
| Qwen3-235B-A22B       | ~130 GB (AWQ)  | Exceeds 64GB total VRAM              |
| Qwen3-Coder-480B-A35B | ~290 GB (Q4)   | Far too large                        |
| Llama-3.1-405B        | ~220 GB (AWQ)  | Far too large                        |

## Memory Calculation Reference

AWQ 4-bit quantization:

- **Formula**: params × 0.5 bytes + ~10% overhead
- 32B params → ~17-18 GB
- 70B params → ~38-40 GB
- 235B params → ~130 GB (DOES NOT FIT)

With TP=2, each GPU gets half the weights. Max per GPU: ~28 GB usable (leaving room for KV cache).

## Benchmark Comparison

### How local models compare to OpenAI proprietary models

Numbers from official model cards and announcements. gpt-oss has configurable
reasoning effort (low/medium/high); "high" is most comparable to o-series models.

**SWE-Bench Verified** (agentic coding — multi-turn, real GitHub issues):

| Model                       | SWE-Bench | Runs locally?        |
| --------------------------- | --------- | -------------------- |
| GPT-5                       | 74.9%     | No                   |
| o3                          | 69-72%    | No                   |
| o4-mini                     | 68.1%     | No                   |
| Qwen3-Coder-480B-A35B       | 69.6%     | No (too large)       |
| **gpt-oss-120b (high)**     | **62.4%** | No (needs ~80 GB)    |
| **gpt-oss-20b (high)**      | **60.7%** | **Yes (~16 GB)**     |
| Qwen3-Coder-30B-A3B         | ~51.6%    | **Yes (~18 GB FP8)** |
| o3-mini (high)              | 49.3%     | No                   |
| o1                          | 48.9%     | No                   |
| DeepSeek-R1 (full)          | 49.2%     | No (too large)       |
| GPT-4o                      | 33.2%     | No                   |
| Qwen3-30B-A3B-Instruct-2507 | ~25.7%    | Yes                  |

**AIME 2025** (math reasoning, no tools):

| Model                       | AIME 2025     | Runs locally? |
| --------------------------- | ------------- | ------------- |
| GPT-5.2 Thinking            | 100%          | No            |
| GPT-5                       | 94.6%         | No            |
| o4-mini                     | 92.7%         | No            |
| **gpt-oss-120b (high)**     | **92.5%**     | No            |
| **gpt-oss-20b (high)**      | **91.7%**     | **Yes**       |
| o3                          | 88.9%         | No            |
| Qwen3-30B-A3B-Thinking-2507 | 85.0%         | Yes           |
| Qwen3-30B-A3B-Instruct-2507 | 61.3%         | Yes           |
| Qwen3-Coder-30B-A3B         | Not published | Yes           |
| gpt-oss-120b (low)          | 50.4%         | —             |
| gpt-oss-20b (low)           | 37.1%         | —             |

**GPQA Diamond** (graduate-level science):

| Model                       | GPQA          | Runs locally? |
| --------------------------- | ------------- | ------------- |
| GPT-5.2 Thinking            | 92.4%         | No            |
| GPT-5                       | 85.7-88.4%    | No            |
| o3                          | 83.3-87.7%    | No            |
| o4-mini                     | 81.4%         | No            |
| **gpt-oss-120b**            | **80.1%**     | No            |
| o1                          | 77.3%         | No            |
| Qwen3-30B-A3B-Thinking-2507 | 73.4%         | Yes           |
| **gpt-oss-20b**             | **71.5%**     | **Yes**       |
| Qwen3-30B-A3B-Instruct-2507 | 70.4%         | Yes           |
| Qwen3-Coder-30B-A3B         | Not published | Yes           |
| GPT-4o                      | ~53.6%        | No            |

**Codeforces ELO** (competitive programming):

| Model                        | ELO           | Runs locally? |
| ---------------------------- | ------------- | ------------- |
| o3                           | 2727          | No            |
| o4-mini                      | 2719          | No            |
| **gpt-oss-120b (high)**      | **2463**      | No            |
| **gpt-oss-20b (high)**       | **2230**      | **Yes**       |
| Qwen3-30B-A3B-Thinking-2507  | 2044          | Yes           |
| DeepSeek-R1 (full)           | 2029          | No            |
| DeepSeek-R1-Distill-Qwen-32B | 1691          | Yes           |
| o1                           | 1673          | No            |
| Qwen3-Coder-30B-A3B          | Not published | Yes           |
| GPT-4o                       | 808           | No            |

**LiveCodeBench v6** (coding, single-turn):

| Model                       | LiveCodeBench | Runs locally? |
| --------------------------- | ------------- | ------------- |
| Qwen3-30B-A3B-Thinking-2507 | 66.0%         | Yes           |
| Qwen3-30B-A3B-Instruct-2507 | 43.2%         | Yes           |
| Qwen3-Coder-30B-A3B         | Not published | Yes           |

Note: gpt-oss LiveCodeBench numbers not published in model card.

**Key takeaway**: gpt-oss-20b at high reasoning is comparable to o3-mini / early o3
on math and coding. It's the strongest model that fits on a single RTX 5090. On
SWE-Bench, it beats Qwen3-Coder-30B (60.7% vs 51.6%).

**Caveat**: gpt-oss reasoning levels matter enormously. At low reasoning, gpt-oss-20b
drops to GPT-4o territory on math (37% AIME). The "high" numbers require the model to
spend many thinking tokens, consuming context and latency.

Sources:

- [gpt-oss model card (arXiv 2508.10925)](https://arxiv.org/html/2508.10925v1)
- [Introducing o3 and o4-mini](https://openai.com/index/introducing-o3-and-o4-mini/)
- [Introducing GPT-5](https://openai.com/index/introducing-gpt-5/)
- [Introducing GPT-5.2](https://openai.com/index/introducing-gpt-5-2/)
- [Learning to reason with LLMs (o1)](https://openai.com/index/learning-to-reason-with-llms/)
- [Introducing SWE-bench Verified](https://openai.com/index/introducing-swe-bench-verified/)
- [BentoML DeepSeek Guide](https://www.bentoml.com/blog/the-complete-guide-to-deepseek-models-from-v3-to-r1-and-beyond)
- [Qwen3-Coder GitHub](https://github.com/QwenLM/Qwen3-Coder)
- [Nebius SWE-bench eval](https://nebius.com/blog/posts/openhands-trajectories-with-qwen3-coder-480b)

## gpt-oss Inference Backend Options

Researched 2026-01-27. gpt-oss is OpenAI's open-weight MoE model family (Apache 2.0,
August 2025). Official repo: [github.com/openai/gpt-oss](https://github.com/openai/gpt-oss).

### Backend Comparison

| Backend                  | Responses API | Custom Function Tools | Stateful (`previous_response_id`) |      Tensor Parallelism       |
| ------------------------ | :-----------: | :-------------------: | :-------------------------------: | :---------------------------: |
| **vLLM** (v0.10.0+)      |    ✅ Full    |        ✅ Yes         |     ✅ (via Semantic Router)      | ✅ `--tensor-parallel-size N` |
| **LM Studio** (v0.3.29+) |    ✅ Full    |        ✅ Yes         |              ✅ Yes               |         ❌ Single GPU         |
| **Ollama** (v0.13.3+)    |   ✅ Basic    |      ❓ Unknown       |               ❌ No               |    ❌ Layer splitting only    |
| **SGLang**               |  ⚠️ Partial   |   ❌ Built-in only    |             ⚠️ Buggy              |          ✅ `--tp N`          |
| **llama.cpp**            | ❌ PR pending |           —           |                 —                 |    ❌ CPU offloading only     |

**Proxy adapters** (add Responses API on top of any Chat Completions backend):

- [HuggingFace responses.js](https://github.com/huggingface/responses.js/) — Express.js,
  full features incl. function tools, streaming, MCP
- [LiteLLM](https://docs.litellm.ai/docs/response_api) — bridges `/responses` ↔
  `/chat/completions`, works with any provider

**Verdict**: vLLM is the only backend with full Responses API + tensor parallelism.
For single-GPU models like gpt-oss-20b (~14 GB), LM Studio is also a strong option
(full Responses API, stateful, function tools, MCP, reasoning effort control).

### What OpenAI / Codex Recommends

- **Server/production**: vLLM (explicitly recommended). Install with `pip install vllm==0.10.1+gptoss`
  or later, serve with `vllm serve openai/gpt-oss-20b`.
- **Consumer/local**: Ollama (`ollama run gpt-oss-20b`) or LM Studio.
- **Codex CLI**: Defaults to Ollama (`oss_provider = "ollama"` in `~/.codex/config.toml`),
  but can point at any OpenAI-compatible endpoint.

### Practical Setup for 2x RTX 5090

gpt-oss-20b is ~14 GB quantized → fits easily on a **single** RTX 5090 (32 GB) with
plenty of room for KV cache. TP=2 is unnecessary for this model.

#### Option A: Single-GPU vLLM (simplest, Responses API)

```bash
CUDA_VISIBLE_DEVICES=0 vllm serve openai/gpt-oss-20b \
  --enable-auto-tool-choice --tool-call-parser hermes
```

Use second GPU for a different model (Qwen3 for thinking, image gen, etc.).

#### Option B: Data parallelism (2x throughput, Responses API)

```bash
# GPU 0
CUDA_VISIBLE_DEVICES=0 vllm serve openai/gpt-oss-20b --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes
# GPU 1
CUDA_VISIBLE_DEVICES=1 vllm serve openai/gpt-oss-20b --port 8001 \
  --enable-auto-tool-choice --tool-call-parser hermes
```

Put a load balancer (nginx, haproxy) in front for transparent routing.

#### Option C: Ollama (simplest setup, no Responses API)

```bash
ollama run gpt-oss-20b
```

Only Chat Completions API. No tensor parallelism. Single-GPU.

Sources:

- [OpenAI gpt-oss repo](https://github.com/openai/gpt-oss)
- [OpenAI Cookbook: run with vLLM](https://cookbook.openai.com/articles/gpt-oss/run-vllm)
- [SGLang gpt-oss docs](https://docs.sglang.io/basic_usage/gpt_oss.html)
- [Codex CLI config](https://developers.openai.com/codex/config-basic/)
- [SGLang Responses API issues](https://github.com/sgl-project/sglang/issues/10038)

## Image Generation Models

Based on [BentoML's 2026 guide](https://www.bentoml.com/blog/a-guide-to-open-source-image-generation-models).

### Top Tier (2026)

| Model          | Type        | Size  | VRAM  | Slug                                      |
| -------------- | ----------- | ----- | ----- | ----------------------------------------- |
| FLUX.1-dev     | Flux        | ~24GB | 24GB+ | `black-forest-labs/FLUX.1-dev`            |
| FLUX.1-schnell | Flux (fast) | ~24GB | 24GB+ | `black-forest-labs/FLUX.1-schnell`        |
| SD 3.5 Large   | SD3         | ~10GB | 16GB+ | `stabilityai/stable-diffusion-3.5-large`  |
| SD 3.5 Medium  | SD3         | ~5GB  | 8GB+  | `stabilityai/stable-diffusion-3.5-medium` |

### Style-Specific (Civitai)

| Model                | Type          | Notes                        |
| -------------------- | ------------- | ---------------------------- |
| Pony Diffusion V6 XL | SDXL finetune | Anime/furry, trained on e621 |
| AutismMix SDXL       | SDXL          | Anime style                  |
| Anything V5          | SD 1.5        | Anime style                  |

Note: Style-specific models and LoRAs are primarily on [Civitai](https://civitai.com/).

## Download Commands

```bash
source ~/.secret_env

# Qwen3-30B-A3B FP8 (thinking + tool use, next experiment)
python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-30B-A3B-FP8')"

# Already downloaded:
# cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit
# casperhansen/deepseek-r1-distill-qwen-32b-awq
# casperhansen/deepseek-r1-distill-llama-70b-awq
# Qwen/Qwen3-32B-AWQ
```

## Notes

- AWQ preferred over GPTQ for vLLM (better inference performance)
- FP8 is higher quality than AWQ 4-bit, supported on RTX 5090 (compute 10.0)
- 70B models need ~38-40GB, fit with TP=2 on 2x32GB GPUs
- 32B models need ~17-18GB, fit on single GPU or split with TP=2
- Known NCCL bug with FP8 on 2x5090: fix with `pip install nvidia-nccl-cu12==2.27.7` inside container
- For image gen, use ComfyUI or AUTOMATIC1111 WebUI as frontend
- FLUX.1-dev requires agreement to license on HuggingFace
