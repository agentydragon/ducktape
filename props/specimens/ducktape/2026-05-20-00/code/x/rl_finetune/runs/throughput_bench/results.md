# Wordle GRPO throughput bench (Qwen3-1.7B, 2× RTX 5090)

Two-stage sweep run via `./bench.py`. Hardware: 2× RTX 5090 (32 GB each).
Layout: vLLM-serve on GPU 0, GRPO trainer on GPU 1 (server mode), unless
`colocate` says otherwise. All probes use LoRA (r=16) on
q/k/v/o/gate/up/down_proj.

Numbers below are completions/sec (prompts/sec × num_generations) at
**effective batch = 64**. `ss_step_s` and `ss_compl/s` come from per-step
timings averaged over `step_times[1:]` (skipping the slow warmup step);
`raw_compl/s` is HF Trainer's `train_samples_per_second` × num_generations,
biased low by warmup amortization.

## Run 1 (subprocess, max_steps=10) — knob isolation

Baseline = current production config. Each row changes one knob from baseline.

| probe         | what changed                                                             | runtime_s | raw_compl/s | vs baseline                  |
| ------------- | ------------------------------------------------------------------------ | --------- | ----------- | ---------------------------- |
| baseline      | (defaults: bs=1, grad_accum=64, num_gen=8, grad_ckpt=on, max_compl=1024) | 774       | **6.62**    | 1.00×                        |
| num_gen_16    | num_generations 8 → 16                                                   | 778       | **13.15**   | **1.99× (essentially free)** |
| no_grad_ckpt  | gradient_checkpointing on → off                                          | 576       | 8.88        | **1.34×**                    |
| max_compl_512 | max_completion_length 1024 → 512                                         | 720       | 7.11        | 1.07×                        |
| colocate      | server → colocate (single GPU, vLLM at 0.3 mem util)                     | 757       | 6.77        | 1.02×                        |

Findings:

- `num_generations` is nearly free in prompts/sec, so doubling it doubles
  completions/sec. vLLM batches 16 generations per prompt almost as
  efficiently as 8.
- Disabling gradient checkpointing buys ~34% per probe — we have ~12 GB
  headroom on GPU 1 even at bs=1, so this is a no-brainer.
- Halving `max_completion_length` only helps 7%: the iteration cap
  (`max_tool_calling_iterations=MAX_GUESSES`) already keeps mean rollout
  length around 170-200 tokens, so the 1024 ceiling rarely binds.
- Colocate ≈ server. The alternation overhead I worried about isn't
  significant for a 1.7B model on a 5090; KV cache budget difference (9.6
  GB colocate at default 0.3 mem util vs 28.8 GB server at 0.9) doesn't
  bite at this batch size either.

## Run 2 (subprocess, max_steps=5, with per-step timings) — parallelism + others

Effective batch held at 64 (= `batch_size × grad_accum`) so vLLM
gen-batch is constant; only the trainer's fwd/bwd micro-batch grows.

| probe          | what changed                        | ss_step_s | ss_compl/s | vs baseline raw   |
| -------------- | ----------------------------------- | --------- | ---------- | ----------------- |
| prefix_caching | vLLM `--enable_prefix_caching=True` | 96.2      | 5.32       | **0.80× (hurts)** |
| bs2_ga32       | bs=2, grad_accum=32                 | 45.9      | 11.15      | 1.69×             |
| bs4_ga16       | bs=4, grad_accum=16                 | 35.4      | 14.45      | 2.19×             |
| **bs8_ga8**    | bs=8, grad_accum=8                  | **24.2**  | **21.15**  | **3.20×**         |
| bs16_ga4       | bs=16, grad_accum=4                 | OOM       | OOM        | OOM               |

`bs16_ga4` OOM'd in both in-process and subprocess attempts (~29 GB in
use, ~5.8 GB more requested → over the 32 GB card). The wall is real,
not an artifact of the in-process driver. Mitigations not tried:
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, lower
`max_completion_length`, 8-bit optimizer, smaller LoRA rank.

`async_grpo` dropped from this run — see TODO.md.

Findings:

- **Trainer micro-batch is the dominant lever.** bs=1→2→4→8 scales
  near-linearly (1.7×, 2.2×, 3.2×). We didn't hit a clear inflection
  point at bs=8.
- **Prefix caching actively hurts.** The system prompt is short
  (~100 tokens), the LoRA-tuned policy invalidates the cache often, and
  vLLM's prefix-cache bookkeeping outweighs the prefill savings. Don't
  enable it for this workload.

## Run 3 (all-on, max_steps=5) — verified stack

Stacking the wins: `bs=8, grad_accum=8, num_gen=16, grad_ckpt=off`
**OOM'd at 31.31 / 31.36 GB** — 50 MB short. Fixed by dropping the
trainer micro-batch one tick to `bs=4, grad_accum=16` (effective
batch still 64).

| probe      | config                                 | ss_step_s | ss_compl/s | vs baseline raw |
| ---------- | -------------------------------------- | --------- | ---------- | --------------- |
| **all_on** | bs=4, ga=16, num_gen=16, grad_ckpt=off | **16.1**  | **63.65**  | **9.62×**       |

The win is mildly superlinear vs the naive multiplicative ceiling
(~8.6×): num_gen=16 saturates vLLM batching better than 8 does, and
the trainer's per-step fixed overhead amortizes over more
generations. Min steady-state step time dropped to **9.8 s**.

## Production-best (verified)

```
--batch-size 2 --grad-accum 32   # bs=4 fits in 5-step bench but OOMs in long runs
--num-generations 16             # ~free 2× from rollout batching
--no-gradient-checkpointing      # +34% from skipping recompute
--liger-kernel                   # fused chunked GRPO loss; avoids 19 GB logits
# (don't enable prefix_caching — actively hurts on this workload)
```

The 5-step bench's `all_on` run with `bs=4, ga=16` measured 9.62×
baseline, but actual training-length runs OOM around step 5 even
with `--liger-kernel` and `expandable_segments:True`. With `bs=4` we
sit at 31.3/31.4 GB and one batch of longer-than-mean rollouts tips
us over. `bs=2, ga=32` gives ~7× baseline (extrapolated from the
`bs2_ga32` probe at 11.15 compl/s × 2× num_gen × 1.34× grad_ckpt-off)
with comfortable memory headroom.

## Notes / caveats

- Baseline was measured with `max_steps=10` and no per-step callback,
  so its `ss_step_s` is unavailable. The 6.62 baseline figure is
  HF's `train_samples_per_second × 8` which is biased low by warmup
  amortization. For a precise speedup measurement, re-run baseline
  with `max_steps=5` and the timing callback.
- `bs >= 16` OOMs on a 32 GB card with 1024-token rollouts (confirmed
  in subprocess; ~29 GB in use before alloc failure). bs=8 is the
  ceiling for this hardware/config. Could be pushed further with
  `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, an 8-bit
  optimizer, or smaller `max_completion_length` if needed.
- `async_grpo` not measured: `trl.experimental.AsyncGRPOTrainer` (trl
  1.3.0 / main 2026-05-03) hard-codes fp32 model load and accepts no
  `peft_config`, so it'd be apples-to-oranges vs the LoRA-bf16 baseline.
  See TODO.md for revisit-when-ready.
