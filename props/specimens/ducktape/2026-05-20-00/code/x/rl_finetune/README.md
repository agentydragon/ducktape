# RL Fine-tuning Experiments

Fine-tune open-source LLMs with GRPO (Group Relative Policy Optimization) on
agentic tasks.

## Wordle (hello-world)

Train Qwen3-1.7B to play Wordle via multi-step tool calling. Uses TRL's
`environment_factory` with the TextArena Wordle environment.

### Two-GPU server mode (recommended)

`trl` is not on `PATH` — `wordle_train.py` is a PEP 723 inline-deps script,
so its `trl` install lives in a hash-named uv cache env. Spawn the server
through `uv run --with` so we don't have to chase that path:

```bash
# Terminal 1: vLLM inference on GPU 0
CUDA_VISIBLE_DEVICES=0 uv run --no-project \
    --with 'trl[vllm]' \
    --with 'transformers @ git+https://github.com/huggingface/transformers.git@main' \
    trl vllm-serve --model Qwen/Qwen3-1.7B

# Terminal 2: GRPO training on GPU 1
CUDA_VISIBLE_DEVICES=1 uv run wordle_train.py
```

Health checks once the server is up: `curl http://localhost:8000/health/`
returns `{"status":"ok"}`.

### Single-GPU colocate mode

```bash
uv run wordle_train.py --colocate
```

### Reasoning mode

Qwen3 thinking mode is enabled with `--think`. In TRL, `--max-completion-length`
is the total rollout budget across the multi-turn tool loop, not a per-guess
budget, so thinking mode needs much more completion budget than non-reasoning
mode.

For a one-GPU vLLM server plus one-GPU trainer setup that gives Wordle enough
room for six thinking/tool rounds:

```bash
# Terminal 1: vLLM inference on GPU 0
CUDA_VISIBLE_DEVICES=0 uv run --no-project \
    --with 'trl[vllm]' \
    --with 'transformers @ git+https://github.com/huggingface/transformers.git@main' \
    trl vllm-serve --model Qwen/Qwen3-1.7B \
    --max-model-len 16384 \
    --gpu-memory-utilization 0.75

# Terminal 2: GRPO training on GPU 1
CUDA_VISIBLE_DEVICES=1 uv run wordle_train.py \
    --think \
    --max-completion-length 8192 \
    --vllm-max-model-length 16384 \
    --batch-size 1 \
    --grad-accum 64 \
    --num-generations 8 \
    --gradient-checkpointing
```

The reasoning benchmark suite captures the fit/speed tradeoff:

```bash
python bench.py --suite thinking --report-only
python bench.py --suite thinking --probes think_8192_mem075 --max-steps 1
```

The current best measured probe is `think_8192_mem075`: 16k vLLM context,
8192-token rollout budget, vLLM peak about 27.4 GiB, trainer peak about
29.8 GiB, median six Wordle feedback rounds, and 14.1% clipped completions.
See `runs/reasoning_bench/results.md` for the measured table and caveats.

### Monitor

```bash
tensorboard --logdir wordle_grpo_output
```

## Related

- [TRL GRPO docs](https://huggingface.co/docs/trl/main/en/grpo_trainer)
- [TRL OpenEnv integration](https://huggingface.co/docs/trl/main/en/openenv)
- [DeepSeek-R1 paper](https://arxiv.org/abs/2501.12948)
- `x/cotrl/` — earlier experiment testing LLMs as RL agents at inference time
