# Wordle GRPO reasoning training run

Started: 2026-05-05.

Goal: run a real Qwen3-1.7B GRPO Wordle training run in thinking mode, using the
best measured two-GPU config from the reasoning-mode benchmark.

## Launch config

vLLM server on GPU 0:

```bash
cd /home/agentydragon/code/ducktape/x/rl_finetune

CUDA_VISIBLE_DEVICES=0 trl vllm-serve \
  --model Qwen/Qwen3-1.7B \
  --host 127.0.0.1 \
  --port 8000 \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.75
```

Trainer on GPU 1:

```bash
cd /home/agentydragon/code/ducktape/x/rl_finetune

CUDA_VISIBLE_DEVICES=1 uv run wordle_train.py \
  --think \
  --vllm-server-port 8000 \
  --vllm-server-timeout 300 \
  --vllm-max-model-length 16384 \
  --max-completion-length 8192 \
  --batch-size 1 \
  --grad-accum 64 \
  --num-generations 8 \
  --gradient-checkpointing \
  --temperature 1.0 \
  --top-p 1.0 \
  --top-k 0 \
  --min-p 0.0 \
  --metrics-out /tmp/wordle_thinking_train_metrics.json
```

Effective GRPO shape: `num_generations=8`, `per_device_train_batch_size=1`,
`gradient_accumulation_steps=64`, so the effective batch remains 64 rollouts per
optimizer step.

## Startup snapshot

Snapshot time: 2026-05-05 04:45:59 local.

Observed process commands:

```text
trl vllm-serve --model Qwen/Qwen3-1.7B --host 127.0.0.1 --port 8000 --max-model-len 16384 --gpu-memory-utilization 0.75
VLLM::EngineCore
uv run wordle_train.py --think --vllm-server-port 8000 --vllm-server-timeout 300 --vllm-max-model-length 16384 --max-completion-length 8192 --batch-size 1 --grad-accum 64 --num-generations 8 --gradient-checkpointing --temperature 1.0 --top-p 1.0 --top-k 0 --min-p 0.0 --metrics-out /tmp/wordle_thinking_train_metrics.json
```

GPU snapshot:

| GPU | role        | memory used | memory total | util |
| --: | ----------- | ----------: | -----------: | ---: |
|   0 | vLLM server |   5,536 MiB |   32,607 MiB |  73% |
|   1 | trainer     |   8,762 MiB |   32,607 MiB |   0% |

`/tmp/wordle_thinking_train_metrics.json` did not exist at startup snapshot
time. That file is written only after `trainer.train()` returns, so live
monitoring should use tensorboard/completion parquet files until the run exits.

Known live output paths:

- `/tmp/wordle_grpo_output/completions/completions_*.parquet`
- `/tmp/wordle_grpo_output/runs/May05_03-41-10_wyrm2/events.out.tfevents.1777977670.wyrm2.1929502.0`
- `/tmp/wordle_thinking_train_metrics.json` after training exits

## Why this config

The reasoning-mode benchmark in `runs/reasoning_bench/results.md` found this to
be the best measured fit/performance point so far:

| metric                    | `think_8192_mem075` |
| ------------------------- | ------------------: |
| vLLM max model length     |              16,384 |
| max completion length     |               8,192 |
| vLLM GPU ready memory     |          26,032 MiB |
| vLLM GPU peak             |          27,375 MiB |
| trainer GPU peak          |          29,814 MiB |
| mean completion length    |        5,634 tokens |
| clipped completions       |               14.1% |
| tool call frequency       |               4.891 |
| mean unique valid guesses |               3.047 |

The 4096-token reasoning probe still clipped 78.1% of completions, so 8192 is
the first measured budget that leaves enough room for most thinking/tool-loop
rollouts to terminate normally. Trainer-side GPU peak was close to the 32 GiB
card limit, so this run keeps the conservative `batch-size=1`,
`num-generations=8`, and gradient checkpointing settings.

## Current interpretation

`max_completion_length` is the total multi-turn rollout budget across thinking,
assistant tool calls, tool responses, and post-tool continuation. It is not a
per-guess budget.

TRL's `completions/clipped_ratio` marks completions whose final generated token
is not EOS/PAD. In the tool loop, TRL can also truncate post-tool continuation
to fit the total rollout budget and can stop adding tool responses when doing so
would exceed the configured limits.

In text samples from the 8192 reasoning probe, the remaining failures were not
only context exhaustion. Representative failures included:

- actual tool feedback being followed by hallucinated prior guesses/feedback;
- final-answer prose after one or a few real tool calls;
- invalid words or invalid-length guesses;
- repeated guesses;
- model-authored transcript-looking tags, which make raw regex counts of
  `<tool_response>` unreliable unless cross-checked with environment metrics.

The most important behavioral metric is therefore the environment-side signal
(`metric_unique_guesses`, invalid-word/length counts, win rate), not just literal
tag counts in the saved completion text.

## Useful checks

Process and GPU snapshot:

```bash
pgrep -af '[t]rl vllm-serve|[w]ordle_train.py|[V]LLM::EngineCore'
nvidia-smi --query-gpu=timestamp,index,name,memory.used,memory.total,utilization.gpu --format=csv
```

Completion stream:

```bash
find /tmp/wordle_grpo_output/completions -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n' | sort | tail -n 20
```

Final metrics, after the training process exits:

```bash
jq . /tmp/wordle_thinking_train_metrics.json
```

Early sample reading should inspect actual text, not only aggregate counters.
The benchmark parquet sample that motivated this note was:

```bash
uv run --with pandas --with pyarrow python - <<'PY'
import pandas as pd

p = "/tmp/wordle_grpo_output/completions/completions_00001.parquet"
df = pd.read_parquet(p)
for i in [23, 33, 62, 9, 12, 16, 3, 34, 5]:
    row = df.iloc[i]
    print("=" * 80)
    print(i, row[["reward_func", "metric_invalid_length", "metric_invalid_word", "metric_unique_guesses"]].to_dict())
    print(str(row.completion)[:2500])
PY
```
