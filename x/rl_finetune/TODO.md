# TODO

Scope is decided and the hello-world is implemented: GRPO via TRL on Qwen3-1.7B
playing the TextArena Wordle environment through multi-step tool calls (see
<README.md>). Remaining work below is infrastructure, training, and UX.

## Infrastructure

- [ ] vast.ai setup script (install deps, pull model weights, mount data)
- [ ] Training launch script
- [ ] Checkpoint upload/download (HuggingFace Hub or S3)

## Training

- [ ] SFT warmup dataset (if needed)
- [ ] GRPO training loop
- [ ] Evaluation harness for agentic tasks

## Tests

- [ ] Wire `test_wordle_env.py` into Bazel (it never runs in CI today). Blocked, not a
      gazelle run: the tree is `# gazelle:exclude`d in the root `BUILD.bazel` (lifting
      it also pulls in `wordle_train.py`, whose `torch`/`trl`/`peft` are not in the
      lockfile), `wordle_env.py` imports `nltk` (absent from `pyproject.toml` and
      `requirements_bazel.txt`), and at import it downloads NLTK corpora when they
      are missing, which needs network. Needs the `nltk` dep plus a hermetic word
      source (vendored data or an injected word list), and the test's top-level
      `from wordle_env import ...` becomes `x.rl_finetune.wordle_env`.

## Throughput

- [ ] Re-run async_grpo throughput probe once `trl.experimental.async_grpo`
      supports PEFT/LoRA and bf16 model load. Currently (trl 1.3.0 / main as
      of 2026-05-03) `AsyncGRPOTrainer.__init__` has no `peft_config` arg
      and hard-codes `dtype=torch.float32`, so it'd be an apples-to-oranges
      comparison vs the LoRA-bf16 baseline. The structural insight (overlap
      rollout with training to remove the alternation idle) is real and
      worth measuring once the trainer can match the rest of the bench's
      config.

## UX

- [ ] Override TRL's per-step rollout rendering. The default
      `print_prompt_completions_sample` puts everything in a 4-col rich
      table (Prompt | Completion | reward | Advantage). The Completion is
      the long part — full multi-turn chat with tool calls — and the
      other 3 columns eat horizontal space it could use to wrap fewer
      lines. Cleanest fix: monkey-patch
      `trl.trainer.grpo_trainer.print_prompt_completions_sample` with a
      one-rollout-per-Panel renderer (header line with reward/advantage,
      body as plain turn blocks, no nested table). See chat for sketch.
