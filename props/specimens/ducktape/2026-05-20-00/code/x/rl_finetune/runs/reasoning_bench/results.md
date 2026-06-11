# Wordle GRPO reasoning-mode bench

Goal: run Qwen3-1.7B GRPO in thinking mode (`--think`) using the same two-GPU
server layout as the non-reasoning throughput bench.

Hardware: 2x RTX 5090, 32 GB each.

## 2026-05-04 smoke probe

Command:

```bash
python bench.py --suite thinking --probes think_1024_safe --max-steps 1
```

Probe config:

- vLLM server on GPU 0: `trl vllm-serve --model Qwen/Qwen3-1.7B --max-model-len 4096`
- trainer on GPU 1: `--think --max-completion-length 1024 --vllm-max-model-length 4096`
- GRPO shape: `batch-size=1`, `grad-accum=64`, `num-generations=8`, `gradient-checkpointing=on`

Result:

| metric                                     |        value |
| ------------------------------------------ | -----------: |
| return code                                |            0 |
| wall time including server/trainer wrapper |        153 s |
| trainer runtime                            |        106 s |
| train samples/s                            |        0.602 |
| vLLM GPU peak                              |   30,940 MiB |
| trainer GPU peak                           |   11,200 MiB |
| tool call frequency                        |        0.953 |
| clipped completions                        |        0.984 |
| mean completion length                     | 999.8 tokens |
| mean terminal reward                       |          0.0 |
| mean unique valid guesses                  |        0.922 |

Takeaways:

- Server-mode thinking runs successfully and the model emits real Wordle tool
  calls in thinking mode.
- The vLLM side is the fit bottleneck. `--max-model-len 4096` already reserves
  about 30.8 GiB at server ready and peaked at about 30.9 GiB.
- `--max-completion-length 1024` is too short for this naive thinking prompt:
  98.4% of completions clipped. The run still produced tool calls, but many
  rollouts spent too much budget on thought before finishing useful play.
- Trainer GPU headroom is large in the safe config, so next probes should tune
  vLLM context / thinking length before increasing trainer micro-batch.

Artifacts:

- `/tmp/wordle_bench/think_1024_safe.config.json`
- `/tmp/wordle_bench/think_1024_safe.metrics.json`
- `/tmp/wordle_bench/think_1024_safe.log`
- `/tmp/wordle_bench/think_1024_safe.vllm.log`

## 2026-05-04 lower vLLM reservation probe

Command:

```bash
python bench.py --suite thinking --probes think_1024_mem075 --max-steps 1
```

Same training config as `think_1024_safe`, but the vLLM server used
`--gpu-memory-utilization 0.75`:

```bash
trl vllm-serve --model Qwen/Qwen3-1.7B --max-model-len 4096 --gpu-memory-utilization 0.75
```

Result:

| metric                                     |         value |
| ------------------------------------------ | ------------: |
| return code                                |             0 |
| wall time including server/trainer wrapper |         185 s |
| trainer runtime                            |         147 s |
| train samples/s                            |         0.434 |
| vLLM GPU ready memory                      |    25,990 MiB |
| vLLM GPU peak                              |    27,399 MiB |
| trainer GPU peak                           |    11,200 MiB |
| tool call frequency                        |         0.906 |
| clipped completions                        |         0.953 |
| mean completion length                     | 1001.4 tokens |
| mean terminal reward                       |           0.0 |
| mean unique valid guesses                  |         0.859 |

Takeaways:

- `--gpu-memory-utilization 0.75` still runs the 4096-context thinking probe
  and leaves roughly 3.5 GiB more GPU 0 headroom than the default reservation.
- It is slower in this one-step probe: 147 s trainer runtime vs 106 s for the
  default-reservation run. Some of that may be sampling variance, but it is
  large enough to keep both probes in the suite.
- The behavioral signal is the same: the model calls tools, but the naive
  thinking prompt spends too much of the 1024-token completion budget on thought.

## 2026-05-05 larger rollout budget probes

Command:

```bash
python bench.py --suite thinking --probes think_4096_mem075 --max-steps 1
python bench.py --suite thinking --probes think_8192_mem075 --max-steps 1
```

Probe configs:

- `think_4096_mem075`: vLLM `--max-model-len 8192 --gpu-memory-utilization 0.75`,
  trainer `--think --max-completion-length 4096 --vllm-max-model-length 8192`
- `think_8192_mem075`: vLLM `--max-model-len 16384 --gpu-memory-utilization 0.75`,
  trainer `--think --max-completion-length 8192 --vllm-max-model-length 16384`
- Both use `batch-size=1`, `grad-accum=64`, `num-generations=8`,
  `gradient-checkpointing=on`

Result:

| metric                                     |  4096 budget |  8192 budget |
| ------------------------------------------ | -----------: | -----------: |
| return code                                |            0 |            0 |
| wall time including server/trainer wrapper |        500 s |        801 s |
| trainer runtime                            |        420 s |        742 s |
| train samples/s                            |        0.152 |        0.086 |
| vLLM GPU ready memory                      |   26,147 MiB |   26,032 MiB |
| vLLM GPU peak                              |   27,522 MiB |   27,375 MiB |
| trainer GPU peak                           |   17,136 MiB |   29,814 MiB |
| mean completion length                     | 3,732 tokens | 5,634 tokens |
| max completion length                      | 4,071 tokens | 8,125 tokens |
| mean terminated length                     | 2,619 tokens | 5,235 tokens |
| clipped completions                        |        0.781 |        0.141 |
| tool call frequency                        |        2.141 |        4.891 |
| mean terminal reward                       |        0.000 |        0.013 |
| mean unique valid guesses                  |        1.641 |        3.047 |
| invalid-word guesses / rollout             |        0.469 |        1.328 |
| invalid-length guesses / rollout           |        0.016 |        0.281 |

Transcript check from `/tmp/wordle_grpo_output/completions/completions_00001.parquet`:

| tool responses in rollout | 4096 count | 8192 count |
| ------------------------: | ---------: | ---------: |
|                         1 |         28 |          4 |
|                         2 |         13 |          1 |
|                         3 |         15 |          6 |
|                         4 |          4 |          8 |
|                         5 |          2 |         13 |
|                         6 |          2 |         32 |

Takeaways:

- `max_completion_length` is the total multi-turn rollout budget. At 1024,
  almost every thinking rollout clips before meaningful play; at 4096, most
  still clip; at 8192, clipping drops to 14.1%.
- `8192` rollout budget with `16384` vLLM context fits on the 2x32 GiB setup,
  but the trainer GPU peaked at 29.8 GiB. There is not much room to raise the
  trainer-side sequence length further without changing another knob.
- The 8192 probe is the first one that naturally gets a median rollout through
  all six Wordle feedback turns. It still does not get all rollouts there:
  32/64 reached six tool responses.
- Remaining waste is mostly behavioral, not only context length: the model
  repeats guesses, emits invalid words, and sometimes stops to answer in prose.
  The next high-leverage step is likely a more explicit concise-thinking prompt
  or reward shaping for valid unique guesses / using the available attempts,
  while keeping the measured `8192/16384` memory envelope.
