# E4 — current-gen generalist MoE: Qwen3.5-35B-A3B FP8, TP2

- **Status:** done — serves at 262K; standout KV efficiency (GDN linear
  attention); verbose reasoner that needs bigger probe budgets + a non-hermes
  tool parser.
- **Date:** 2026-07-17
- **Plan:** <../../PLAN.md> § "First five experiments" → E4

## Goal

"Is there a free upgrade over the 2025 coding baseline (E1)?" Serve the strongest
recent generalist MoE that plausibly fits resident and compare.

## Configuration

- **Model:** `Qwen/Qwen3.5-35B-A3B-FP8`. On load this turned out to be more than
  a text MoE: arch `Qwen3_5MoeForConditionalGeneration` — a **vision-language
  MoE** (ViT + `MMEncoderAttention`) that also uses **GDN (Gated Delta Net)
  linear attention** (`qwen_gdn_linear_attn`) in place of full attention in most
  layers. FP8 weights via CUTLASS block-scaled kernels.
- vLLM 0.25.1, TP2, FP8 KV, max-model-len 262144, **gpu-mem-util 0.85** (see
  below), hermes tool parser. Manifest: <deployment.yaml>.

## Results

### Capacity / resources (the standout)

| Metric                 | Value                                           |
| ---------------------- | ----------------------------------------------- |
| Allocated context      | 262,144 (255,671-token request completed)       |
| **GPU KV cache**       | **1,404,197 tokens** (vs qwen3-coder's 815K)    |
| Max concurrency @ 262K | **5.36×** (vs 3.11× qwen3-coder, 4.20× gpt-oss) |
| Peak VRAM              | GPU0 29.0 GB / GPU1 27.0 GB (0.85 util)         |
| Weights                | 17.4 GiB/GPU                                    |

**GDN linear attention is the story**: even at a reduced 0.85 util it holds
**1.4M tokens** of KV — linear-attention layers cost near-constant memory, so the
KV footprint per token is a fraction of a full-attention model. This is the
architecture to watch for cheap long context (cf. the 1M discussion in E3).

### Latency (cold prefill — prefix caching is disabled for this GDN model)

| Input ctx | TTFT (cold) | Decode tok/s (incl. reasoning) |
| --------- | ----------- | ------------------------------ |
| 8K        | 0.66 s      | 231                            |
| 32K       | 2.3 s       | 226                            |
| 128K      | 13.2 s      | 211                            |

### Needle / effective context — a probe artifact, then confirmed

The automated needle probe reported failure at 128K and 262K. That was **wrong**:
Qwen3.5 is a **very verbose reasoning model that emits its chain-of-thought as
`content`** (not `reasoning_content`). The probe's `max_tokens=32` is consumed
entirely by a "Thinking Process:" preamble, so the answer is truncated before
it appears. With `max_tokens=1024` a short-context needle **does** surface the
code (`ZQ7-4413-XK`) — and the model was _still reasoning_ at 1024 tokens.

So: retrieval works; the harness's small probe budget doesn't suit a verbose
reasoner. Effective long-context retrieval is **not** definitively measured here
(would need a large, reasoning-aware output budget); short-context retrieval is
confirmed. Marked `local~`/unverified in <../../results.md>.

### Tool calls

The `hermes` parser yields **no tool calls** (n_calls=0) — the model reasons in
`content` and doesn't emit hermes-formatted calls. Needs a Qwen3.5-appropriate
tool/reasoning parser; not resolved here.

### Verdict

Qwen3.5-35B is a current-gen **generalist reasoning VL-MoE**, not a drop-in
faster coder. Its GDN linear attention is genuinely exciting for KV/long-context
efficiency (1.4M-token cache, 5.36× concurrency). But its **reasoning verbosity
is a real agent-latency cost** — thousands of reasoning tokens per answer at
~210–230 tok/s means slow wall-clock turns — and its tool-calling needs parser
work. Not a free upgrade over E1 for coding-agent latency; a strong candidate to
revisit for long-context/generalist work with a proper reasoning-aware harness.

## Notes / anomalies

- **Display shares GPU0.** vLLM at 0.90 util failed the startup free-memory check
  (`Free memory 27.62/31.36 GiB < desired 28.22`) — wyrm2's desktop/display stack
  (sunshine + GDM on the 5090, ~2.7 GB) permanently occupies GPU0. Dropped to
  **0.85 util**; every GPU experiment on wyrm2 must leave the display ~3 GB.
- **Harness limitation surfaced:** verbose reasoning-in-`content` models need
  (a) larger needle/tool `max_tokens` and (b) a reasoning-aware answer extractor.
  Follow-up for the bench.
- Multimodal capability (vision) not exercised — text-only bench.
