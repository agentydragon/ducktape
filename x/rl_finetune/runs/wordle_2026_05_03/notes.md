# Training run notes

## Run: 2026-05-03 18:33 → ongoing (Qwen3-1.7B GRPO Wordle)

### Setup

- Hardware: 2× RTX 5090 (32 GB each)
- Layout: vllm-serve on GPU 0 (default args), trainer on GPU 1
- Config: defaults from `wordle_train.py` after the bs=2 fallback
  (commit `688f6e32a`):
  - `--batch-size 2 --grad-accum 32` (effective batch 64)
  - `--num-generations 16`
  - `--no-gradient-checkpointing`
  - `--liger-kernel`
  - `--max-completion-length 1024`
  - `--lr 5e-6 --epochs 1000`
  - `--n-prompts 512`
- Tensorboard run dir: `/tmp/wordle_grpo_output/runs/May03_18-33-12_wyrm2`

### Throughput

- Wall-clock: 640 steps in ~6.5 h → **~36 s/step** steady-state.
- vLLM at 30.9 GB / 32 GB; trainer at 31.8 GB / 32 GB. Flush against
  the wall but stable. No OOM in 640 steps.

### Reward trajectory (windowed mean of `train/reward`)

| steps   | mean      | max in window |
| ------- | --------- | ------------- |
| 1–50    | 0.156     | 0.375         |
| 201–250 | 0.167     | 0.341         |
| 401–450 | 0.179     | 0.306         |
| 601–640 | **0.212** | 0.416         |

+36% relative on mean reward over 640 steps. Slow but real upward
trend. No win (reward=1.0 individual rollout) seen in the windowed
maxes; the partial-credit signal (G=1.0, Y=0.5 per letter, normalized
by 5) is doing the work. `reward_std` stays around 0.20-0.21 — healthy
intra-group variance, so GRPO advantages aren't degenerating.

### Behavior observations

- **`tools/call_frequency` 6.5 → 9.3 over the run.** Started at ~6.5
  calls per rollout (just under the 6-guess cap × occasional doubles)
  and crept up to ~9.3. Two contributors:
  1. Post-game-over looping: the model wins or loses, env starts
     returning "Game already over." strings, model keeps emitting
     `guess` calls until `max_tool_calling_iterations=6` exhausts. We
     have a TODO to actually abort on `env.done`.
  2. Some turns the model emits multiple tool_calls in one assistant
     message (TRL's loop accepts that and runs them all).
- **`completions/mean_length` 150 → 214 tokens.** Model is producing
  more text per rollout as training progresses; nowhere near the 1024
  cap (`clipped_ratio` peaked briefly at 0.156, currently 0).
- **`tools/failure_frequency` is 0 throughout.** The polite-string
  fix in `WordleEnv.guess` (return string instead of raise after done)
  means TRL never wraps an exception into `{"error": str(e)}`. Logs
  are clean.
- **`grad_norm` 0.10 → 0.014.** Gradients shrinking — typical of GRPO
  early in training as advantage variance settles. Not zero-collapse;
  reward_std stays healthy.
- **`train/loss` oscillates near zero (−0.38 to +0.08).** GRPO loss is
  mean-zero by construction; magnitude isn't directly meaningful.

### What this is NOT

- Not solving Wordle. The model isn't winning games; it's getting
  partial credit by picking high-frequency letters / guessing
  plausibly-shaped 5-letter words. Expected for non-thinking 1.7B on
  this task — we'd predicted the easy wins (format adherence,
  letter-frequency openers) would come early and the hard part
  (integrating per-letter feedback across turns) probably wouldn't
  emerge without thinking enabled.
- Not finished. `--epochs 1000` would take ~1.7 days at this pace;
  the user can stop whenever the curve plateaus.

### Followups noted by this run

- Post-game-over looping (call_frequency > MAX_GUESSES) confirms
  the env-done abort TODO is not just theoretical — the model really
  does waste turns this way.
- Memory utilization at 31.8/32 GB on the trainer is uncomfortable.
  If a future change adds any per-step memory (longer rollouts,
  bigger LoRA, multi-LoRA, etc.) we'll start hitting OOM again.
  `bs=1` would give the headroom; `grad_ckpt=on` is the cheaper way
  back to safety.
