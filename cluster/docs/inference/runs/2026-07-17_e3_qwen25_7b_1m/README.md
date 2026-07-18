# E3 — the 1M attempt: memory fits, kernels don't (Blackwell + vLLM 0.25.1)

- **Status:** done — informative negative. 1M is memory-feasible but
  **kernel-blocked** on this hardware/runtime. Practical ceiling stays ~256K.
- **Date:** 2026-07-17
- **Plan:** <../../PLAN.md> § "First five experiments" → E3

## What this run set out to do

The PLAN's E3 was "Nemotron 3 Nano, 1M context". Two corrections on contact with
reality:

1. **Nemotron-3-Nano is 256K, not 1M** (`NemotronHForCausalLM`,
   `max_position_embeddings=262144`). Its value is the hybrid-Mamba KV
   efficiency, not a 1M window. Deferred to its own run (user wants the Mamba
   arch tried) — its weights are already partly cached.
2. So E3 pivoted to the **genuine 1M question**: pick a model that is actually
   1M-position-capable and see if it runs on 2×5090.

## Which model can even do 1M here (the memory math)

From E1, qwen3-coder-30b held **815,504 tokens** of KV in its budget → ~48
KB/token (48 layers, GQA, FP8 KV). So 1M tokens ≈ 48 GB of KV — a dense/MoE GQA
model tops out just short of 1M. Candidates checked:

| Model                        | max positions | 1M KV (FP8)                    | Fits 64 GB?       |
| ---------------------------- | ------------- | ------------------------------ | ----------------- |
| Qwen2.5-**7B**-Instruct-1M   | 1,010,000     | ~28 GB (4 KV heads, 28 layers) | **yes**           |
| Qwen2.5-14B-Instruct-1M      | 1,010,000     | ~96 GB (8 KV heads, 48 layers) | no                |
| Nemotron-3-Nano-30B (hybrid) | 262,144       | tiny (Mamba)                   | yes, but 256K cap |
| MiniMax-M2.5                 | 196,608       | —                              | not 1M            |

So **Qwen2.5-7B-Instruct-1M** is the one that fits: ~28 GB KV for 1M + ~15 GB
weights, comfortably inside 64 GB at TP2. Memory is **not** the blocker.

## The actual blocker: dual-chunk attention has no Blackwell kernel

Qwen2.5-1M reaches 1M via **dual-chunk attention (DCA)**
(`dual_chunk_attention_config = {chunk_size: 262144, original_max_position_embeddings: 262144}`)
— its base window is 256K and DCA stitches chunks to extend it. On this stack it
can't run:

- vLLM auto-selects **FlashInfer** on Blackwell (sm_120); the only available
  backends are `[FLASHINFER, TRITON_ATTN]` — **flash-attn is not built for
  sm_120** in `vllm/vllm-openai:latest` (0.25.1). vLLM's DCA is implemented only
  in the flash-attn backend.
- With DCA on, FlashInfer crashes at model init:
  `TypeError: FlashInferImpl.__init__() got an unexpected keyword argument 'layer_idx'`
  (DCA passes per-layer indices FlashInfer doesn't accept).
- `VLLM_ATTENTION_BACKEND` is **unrecognized** in 0.25.1 (warns "Unknown env
  variable"), so it can't be forced.
- Disabling DCA via `--hf-overrides '{"dual_chunk_attention_config": null}'`
  fails differently: vLLM's `verify_dual_chunk_attention_config` does item
  assignment on it → `TypeError: 'NoneType' object does not support item
assignment`. So DCA can't be cleanly turned off either.

Net: **Qwen2.5-1M does not serve at all on this build**, at any context.

## Answer to "can we run 1M of anything here?"

**Not on 2×5090 with `vllm/vllm-openai:latest` (0.25.1) today** — and the reason
is _kernel support, not memory_. The 1M-capable model that fits needs dual-chunk
attention, whose kernels (flash-attn) aren't available for Blackwell in this
image. The **practical effective-context ceiling remains ~256K** (E1 reached
262K with standard attention).

## Follow-ups (to actually get 1M)

Feasibility checked 2026-07-17 (web research):

- **Qwen2.5-1M on newer vLLM — low chance, don't bother.** Its DCA is "powered by
  BladeLLM; optimizations _will be_ integrated into vLLM" — not there yet, and no
  evidence anyone got its DCA path working on sm_120. Not worth a nightly
  download+compile.
- **DeepSeek-V4-Flash — the genuine 1M path.** Native 1M via **Compressed Sparse
  Attention (not DCA)**, has vLLM **Day-0 support**, and is confirmed running on
  **sm_120** (RTX Pro 6000). KV-frugal (≈10% of V3.2's KV at 1M). Catch: 284B-total
  (13B active) ≈ 142 GB @ 4-bit → needs **offload** (won't fit 64 GB resident), so
  it's an offload-lane experiment (large download, slow), not an image-swap.
  Refs: <https://vllm.ai/blog/2026-04-24-deepseek-v4>,
  <https://recipes.vllm.ai/deepseek-ai/DeepSeek-V4-Flash>.
- **SGLang** — iffy on Blackwell (trtllm_mha SM-detection ValueError, gptq_marlin
  issues reported on RTX 5090); not obviously a better 1M path.
- **Nemotron-H / hybrid (Mamba)** — 256K cap today, but the arch's tiny KV (cf. E4
  GDN's 1.4M-token cache) is the natural home for long context if a 1M-position
  hybrid lands.

No `results.md` coding-agent row (nothing served). Recorded in the long-context
table as a blocked attempt.
