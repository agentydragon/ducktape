# Local LLM Benchmarks — Claude Code Web Environment

Benchmarks of open-weight LLMs running locally via llama.cpp on the Claude Code
web (gVisor sandbox) CPU-only environment.

## Environment

- **CPU**: Intel (family 6, model 207), 16 cores, 1 thread/core
- **RAM**: 21 GiB total
- **GPU**: None (CPU-only inference)
- **OS**: Linux 4.4.0 (gVisor sandbox)
- **Runtime**: llama.cpp b7993 (commit 2cce9fd)

AMX (Advanced Matrix Extensions) instructions are unavailable in the gVisor
sandbox. Bare-metal performance would be higher.

## Summary

| Model             | Params (total / active) | Quant  | Size     | pp512 (t/s) | tg128 (t/s) |
| ----------------- | ----------------------: | ------ | -------- | ----------: | ----------: |
| gpt-oss-20b (MoE) |            20.9B / 3.6B | Q4_K_M | 10.8 GiB |      ~63-73 |      ~12-13 |
| Qwen3-8B (dense)  |                    8.2B | Q4_K_M | 4.7 GiB  |      ~79-82 |        ~8-9 |

Key observations:

- gpt-oss-20b's MoE architecture (only 3.6B active params per token) gives it
  **faster text generation** than the dense 8.2B Qwen3-8B despite having 2.5x
  more total parameters.
- Qwen3-8B has **faster prompt processing** thanks to its smaller total weight.
- Qwen3-8B fits comfortably in ~5 GiB (vs ~11 GiB), leaving more headroom for
  PostgreSQL, backend services, and other containers.

## gpt-oss-20b

- **Model**: gpt-oss-20b (OpenAI, Apache 2.0)
- **Architecture**: Mixture of Experts (MoE), 20.91B total params, 3.6B active
- **Quantization**: Q4_K_M (`unsloth/gpt-oss-20b-GGUF`)
- **Size on disk**: 10.81 GiB

### Detailed Results

| Test   |     Tokens/sec |
| ------ | -------------: |
| pp1    | 20.44 +/- 0.32 |
| pp128  | 68.15 +/- 4.02 |
| pp256  | 70.55 +/- 3.84 |
| pp512  | 63.53 +/- 0.41 |
| pp1024 | 63.18 +/- 1.91 |
| tg64   | 13.92 +/- 2.14 |
| tg128  | 11.60 +/- 1.69 |
| tg256  | 13.61 +/- 0.38 |

### Notes

- All quantizations of this model are similar in size (~11-12 GiB) because
  90%+ of parameters are MoE FFN weights that OpenAI post-trained with
  MXFP4 quantization.
- MoE architecture means only 3.6B parameters are active per token, which is
  why text generation is faster than Qwen3-8B despite the larger total size.

## Qwen3-8B

- **Model**: Qwen3-8B (Alibaba, Apache 2.0)
- **Architecture**: Dense transformer, 8.19B params
- **Quantization**: Q4_K_M (`unsloth/Qwen3-8B-GGUF`)
- **Size on disk**: 4.68 GiB

### Detailed Results

| Test   |     Tokens/sec |
| ------ | -------------: |
| pp1    |  8.04 +/- 1.63 |
| pp128  | 71.79 +/- 6.82 |
| pp256  | 82.01 +/- 1.70 |
| pp512  | 79.18 +/- 5.72 |
| pp1024 | 79.16 +/- 1.34 |
| tg64   |  8.25 +/- 1.84 |
| tg128  |  8.50 +/- 0.23 |
| tg256  |  8.79 +/- 0.50 |

### Notes

- Qwen3-8B natively supports tool calling via its chat template (works with
  `--jinja` flag in llama-server). Tested with `/v1/responses` API and
  `tools` parameter — correctly emits `function_call` output items.
- Qwen3 text-only models are instruct-tuned by default (no separate
  `-Instruct` suffix). Use `/no_think` in prompts to disable the thinking
  mode for faster responses.
- At ~8-9 t/s text generation, a 500-token critic response takes ~55-60
  seconds. Usable for evaluation but slower than gpt-oss-20b.

## Terminology

- `pp` = prompt processing (prefill speed)
- `tg` = text generation (decode speed, the user-facing output rate)
