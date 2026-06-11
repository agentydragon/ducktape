# Benchmarks and known results

Configurations we've actually run, what we measured, what bit us. Each
row exists to inform either a model choice or an inference-config
choice — see <README.md#goal>. Update when you bring up a new config or
rerun an existing one; empty fields mean "not measured yet" — fill them
in, don't delete the row.

## Configurations

A "configuration" = backend + model variant + the flags that matter for
correctness or perf. Two configs that differ in `--max-num-seqs` count as
distinct because that flag changes whether warmup OOMs.

### Live in cluster (k8s, wyrm2)

| ID          | Backend | Model                | Inner format  | Key flags                                                                        | Status              |
| ----------- | ------- | -------------------- | ------------- | -------------------------------------------------------------------------------- | ------------------- |
| `c-gpt20`   | Ollama  | `gpt-oss:20b`        | MXFP4-in-GGUF | `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_NUM_CTX=131072` | Running, unmeasured |
| `c-gpt120`  | Ollama  | `gpt-oss:120b`       | MXFP4-in-GGUF | same as above                                                                    | Running, unmeasured |
| `c-gemma31` | Ollama  | `gemma4:31b-it-q8_0` | Q8_0 GGUF     | same as above                                                                    | Running, unmeasured |

### Validated on host (wyrm2 systemd-user, see <vllm_history.md>)

| ID             | Backend | Model                                            | Inner format                   | Key flags                                                                                                              |
| -------------- | ------- | ------------------------------------------------ | ------------------------------ | ---------------------------------------------------------------------------------------------------------------------- |
| `h-qwen3c-awq` | vLLM    | `cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit` | AWQ 4-bit (compressed-tensors) | `--tensor-parallel-size 2 --kv-cache-dtype fp8 --max-model-len 262144 --gpu-memory-utilization 0.90 --max-num-seqs 32` |
| `h-qwen3c-ol`  | Ollama  | `qwen3-coder:30b-a3b-q4_K_M` (custom 131K ctx)   | Q4_K_M GGUF                    | `num_ctx 131072` via Modelfile <../../../x/local_llm/Modelfile.qwen3-coder-long>                                       |

Configured but unverified on host: `h-r1-32b`, `h-r1-70b`, `h-qwen3-32b` —
see <../../../x/local_llm/start-vllm-deepseek-r1.sh> and siblings.

## Known measurements

### Throughput

| Config         | Input | Prefill TPS p50  | Content decode TPS p50 (single) | t_done p50 (s) | Source                                                               |
| -------------- | ----- | ---------------- | ------------------------------- | -------------- | -------------------------------------------------------------------- |
| `c-gpt20`      | 1024  | 3 800            | **174**                         | 1.96           | <runs/2026-04-28_initial/README.md>                                  |
| `c-gpt20`      | 8192  | 21 300           | **150**                         | 2.31           | <runs/2026-04-28_initial/README.md>                                  |
| `c-gpt120`     | 1024  | 119              | **1.48** (2)                    | 191.8          | <runs/2026-04-28_initial/README.md>                                  |
| `c-gpt120`     | 8192  | 1 016            | **1.56** (2)                    | 183.9          | <runs/2026-04-28_initial/README.md>                                  |
| `c-gemma31`    | 1024  | n/a (1)          | n/a (1)                         | 7.63           | <runs/2026-04-28_initial/README.md>                                  |
| `c-gemma31`    | 8192  | n/a (1)          | n/a (1)                         | 8.50           | <runs/2026-04-28_initial/README.md>                                  |
| `h-qwen3c-awq` | 119 K | 91 000 (cached)  | —                               | —              | <qwen3_coder_vram_analysis.md#real-world-awq-performance-2026-01-24> |
| `h-qwen3c-awq` | 128 K | 28 600 (cold)    | —                               | —              | <qwen3_coder_vram_analysis.md#real-world-awq-performance-2026-01-24> |
| `h-qwen3c-awq` | 130 K | 100 000 (cached) | —                               | —              | <qwen3_coder_vram_analysis.md#real-world-awq-performance-2026-01-24> |

(1) **gemma4 reasons by default** and uses `delta.reasoning` (not
`delta.reasoning_content` like gpt-oss), so the initial parser missed
its tokens entirely. End-to-end times are real (~30 tok/s implied) but
per-phase decomposition isn't recovered. Followup at
<runs/2026-04-28_gemma_followup/README.md> patched the parser and tried
to suppress reasoning via `think: false` — but `/v1/chat/completions`
silently ignores that field, so gemma kept reasoning. Throughput
reported as combined decode (no split) is the right next move; deferred.

(2) Confirmed CPU-offload-bound: `ollama ps` shows
`19%/81% CPU/GPU` for the resident model; ~13 GB of weights in CPU RAM.
See <runs/2026-04-28_initial/README.md#followup-cpu-offload-confirmed-for-gpt-oss120b>.

### Long-context recall (NIAH)

| Config         | 119K | 128K | 130K | Method                                                                                        |
| -------------- | ---- | ---- | ---- | --------------------------------------------------------------------------------------------- |
| `h-qwen3c-awq` | ✅   | ✅   | ✅   | <../../../x/local_llm/test_long_context.py> hardcoded-key recall, single depth (last section) |
| all others     | —    | —    | —    | TODO                                                                                          |

Single-depth NIAH catches the worst case for many models (information at
end of context recalled best when it's the recency bias). A full pyramid
across depths × contexts would tell us where each model's recall actually
fails — see "proposed benchmarks" below.

### Cold load time (model load + first token, off `lvm-proxmox-hdd`)

| Config      | t_done warmup (s) | Effective MB/s | Notes                                                                                        |
| ----------- | ----------------- | -------------- | -------------------------------------------------------------------------------------------- |
| `c-gpt20`   | 181               | ~75            | 13.8 GB MXFP4-in-GGUF                                                                        |
| `c-gpt120`  | 1013              | ~80            | 65.4 GB MXFP4-in-GGUF — exceeds 64 GB VRAM, so post-load operation is also CPU-offload-bound |
| `c-gemma31` | 262               | ~130           | 33.8 GB Q8_0; effective rate higher than gpt-oss models, possibly due to model layout        |

Effective throughput consistent with HDD-bound cold load. See
<backend_comparison.md#storage-class> for the storage-class trade-off.

Source: <runs/2026-04-28_initial/README.md>.

### Reasoning quality — AIME-2024 first-10 (small-N)

Strict scoring via Inspect AI's stock `aime_scorer` (penalizes
format-violation, which is a real model capability gap). Permissive
re-grade in parens — handles `\boxed{N}` / `\(N\)` for context.

| Config    | N   | reasoning_effort | strict pass@1    | (regrade) | avg out_tok | wall   | Source                                     |
| --------- | --- | ---------------- | ---------------- | --------- | ----------- | ------ | ------------------------------------------ |
| `c-gpt20` | 30  | low              | 12/30 (0.40)     | (20/30)   | 8 928       | 1h 13m | <runs/2026-04-29_aime_gpt20_n30/README.md> |
| `c-gpt20` | 30  | **medium**       | **21/30 (0.70)** | (26/30)   | 8 415       | 0h 59m | <runs/2026-04-29_aime_gpt20_n30/README.md> |
| `c-gpt20` | 30  | high             | 15/30 (0.50)     | (19/30)   | 9 333       | 0h 45m | <runs/2026-04-29_aime_gpt20_n30/README.md> |
| `c-gpt20` | 10  | low              | 5/10 (0.50)      | (7/10)    | 8 186       | 0h 11m | <runs/2026-04-28_aime_gpt20/README.md>     |
| `c-gpt20` | 10  | medium           | 3/10 (0.30)      | (8/10)    | 9 303       | 0h 09m | <runs/2026-04-28_aime_gpt20/README.md>     |
| `c-gpt20` | 10  | high             | 7/10 (0.70)      | (7/10)    | 14 187      | 0h 16m | <runs/2026-04-28_aime_gpt20/README.md>     |

**Headline at full N=30:** medium decisively wins (3σ above low, 2σ
above high) — the inverted-U with `reasoning_effort` is a real signal,
not noise. High over-thinks: 11 genuine wrong answers vs 4 at medium.
Stderr ~0.09 at N=30. The N=10 numbers were misleading — small-N
variance dominated.

**Token usage barely moves with the knob:** mean output tokens range
8 415 → 9 333 (~10%) across efforts, while per-problem variance
within an effort spans 1K to 41K. `reasoning_effort` is more like a
hint than a budget on this model+endpoint.

### Coding quality — HumanEval N=164

Inspect AI's `inspect_evals/humaneval` task. Execution-graded (binary
pass/fail per problem via Python sandbox). Original 164-problem
dataset; HumanEval+ not supported by inspect_evals.

| Config    | N   | reasoning_effort | pass@1              | stderr | total out_tok | wall  | Source                                           |
| --------- | --- | ---------------- | ------------------- | ------ | ------------- | ----- | ------------------------------------------------ |
| `c-gpt20` | 164 | low              | 157/164 (0.957)     | 0.016  | 134 K         | 16:00 | <runs/2026-04-29_humaneval_gpt20/README.md> (\*) |
| `c-gpt20` | 164 | medium           | **159/164 (0.970)** | 0.013  | 129 K         | 11:34 | <runs/2026-04-29_humaneval_gpt20/README.md>      |
| `c-gpt20` | 164 | high             | **159/164 (0.970)** | 0.013  | 123 K         | 11:33 | <runs/2026-04-29_humaneval_gpt20/README.md>      |

(\*) Low-effort wall includes ~4 min one-time Docker sandbox image pull.

**Headline:** `gpt-oss:20b` is at HumanEval saturation. Effort levels
indistinguishable; AIME's inverted-U cannot replicate here (no
headroom). **Output tokens decrease** with effort (134 K → 129 K →
123 K) — same direction as AIME, more pronounced. Need a
less-saturated coding eval (BigCodeBench, LiveCodeBench, or SWE-bench)
for actual model discrimination.

## Known caveats

### Cluster Ollama

- **gpt-oss streaming finishReason bug** breaks OpenCode integration —
  `finishReason` is returned as object instead of string, causes `ZodError`
  in `processor.ts`. Tracked at [opencode#7439](https://github.com/anomalyco/opencode/issues/7439).
  In <../../../nix/home/opencode/default.nix> these are routed via the
  cluster LiteLLM endpoint, not direct Ollama.
- **MXFP4 weights are not native FP4 on 5090** — Ollama dequantizes to
  bf16/fp16 for compute. Real perf gap vs vLLM/SGLang on same hardware.
- **No tensor parallelism** — `gpt-oss:120b` (~65 GB) is laid across both
  GPUs sequentially, not sharded. Decode is bandwidth-bound.

### Host vLLM (validated)

- **`--quantization awq` flag breaks `cyankiwi/...-AWQ-4bit`** — model uses
  `compressed-tensors` format, vLLM auto-detects. Don't pass the flag.
- **Default `--max-num-seqs 256` OOMs at long context during sampler warmup.**
  Cap at 32 for 262K context configs.
- **AWQ Qwen3-Coder removes thinking mode** — base model property
  (Qwen3-Coder is post-trained without thinking fusion); not a quant artifact.
  For thinking + tools, use `Qwen3-30B-A3B` (non-Coder).
- **No GPU P2P** — wyrm2 is a Proxmox VM; vLLM falls back to NCCL
  CPU-mediated allreduce. Adds ~0.5 GiB per-GPU staging memory and
  some latency. Not a memory crisis at AWQ; budget it in for any new model.

### Both backends

- **Qwen3-Coder is not a thinking model** — don't benchmark reasoning
  quality on it; use plain Qwen3-32B / DeepSeek-R1 / gpt-oss for that.
- **bf16 30B-class won't fit on 2× 5090 with TP=2** —
  see <vllm_history.md#bf16-weights-dont-fit-and-tp2-doesnt-save-you>.

## Off-the-shelf eval runners

The vast majority of useful evals are already packaged. Each of these takes
an OpenAI-compatible base URL + model name. Prefer these over hand-rolled
scripts.

| Repo                                                                                                                                               | Use for                                                                                  | Why                                                                                             |
| -------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| [openai/simple-evals](https://github.com/openai/simple-evals)                                                                                      | MMLU, GPQA, MATH, AIME, HumanEval, MGSM, DROP                                            | Cleanest "press a button" runner. OpenAI's reference implementation. One sampler class per API. |
| [EleutherAI/lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness)                                                            | Anything with a leaderboard number (200+ tasks)                                          | `--model local-chat-completions --model_args base_url=...,model=...`                            |
| [evalplus/evalplus](https://github.com/evalplus/evalplus)                                                                                          | HumanEval+, MBPP+ (code, with test augmentation)                                         | Single CLI, OpenAI backend built in.                                                            |
| [ShishirPatil/gorilla → BFCL](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard)                                | Tool / function calling                                                                  | The standard tool-use leaderboard. OpenAI-compatible adapter.                                   |
| [LiveCodeBench/LiveCodeBench](https://github.com/LiveCodeBench/LiveCodeBench)                                                                      | Contamination-resistant coding                                                           | Fresh problems by date; pass `--openai_base_url`.                                               |
| [UKGovernmentBEIS/inspect_ai](https://github.com/UKGovernmentBEIS/inspect_ai) + [inspect_evals](https://github.com/UKGovernmentBEIS/inspect_evals) | Modern eval framework (UK AISI), growing task list (AIME, MATH, GPQA, MMLU-Pro, agentic) | `inspect eval inspect_evals/aime2024 --model openai/<name>` against an `OPENAI_BASE_URL`.       |
| [openai/gpt-oss/evals](https://github.com/openai/gpt-oss/tree/main/evals)                                                                          | gpt-oss-specific (handles harmony + `reasoning_effort` correctly)                        | Use these when validating gpt-oss configs — they don't strip thinking tokens by accident.       |

`vllm bench serve` (ships with vLLM) is the right tool for raw throughput
measurements, not quality.

## Proposed benchmark suite

Designed for "reasonable N for good signal in a reasonable time on this GPU."
Each suite below targets a specific question and runs in <1 hour. **Use the
off-the-shelf runners above wherever possible** — the commands below are
illustrative entry points, not bespoke scripts to maintain.

### Suite 1: Smoke throughput (~10 min/config)

Single-stream prefill+decode TPS at three context lengths. Catches gross
regressions and gives apples-to-apples numbers across configs.

```bash
# vLLM ships this. For an OpenAI-compatible server (works for Ollama too):
vllm bench serve \
  --backend openai-chat \
  --base-url http://<host>:<port> \
  --endpoint /v1/chat/completions \
  --model <served-model-name> \
  --dataset-name random \
  --random-input-len 1024 --random-output-len 256 \
  --num-prompts 32
```

Run for input lengths 1024 / 8192 / 32768 (and 131072 for long-ctx configs).
Records prefill TPS, decode TPS, p50/p99 TTFT, p50/p99 ITL.

### Suite 2: Concurrent throughput (~15 min/config)

Aggregate decode TPS at N = 1, 4, 8, 16. Tells us where the engine
saturates and how much headroom we have for multi-tenant.

```bash
vllm bench serve --request-rate 4 --num-prompts 256 ...   # ~1 min
vllm bench serve --request-rate 8 --num-prompts 256 ...
# escalate until p99 ITL doubles vs N=1
```

### Suite 3: NIAH pyramid (~20 min/config)

Recall accuracy across depths × contexts. Existing
<../../../x/local_llm/test_long_context.py> covers one depth. Easy to
extend: place the key at depth ∈ {10%, 50%, 90%, 99%} × context
∈ {8K, 32K, 128K} = 12 trials per model. Pass/fail per trial.

### Suite 4: Reasoning quality, fast (~30 min for 30B, ~60 min for 120B)

**AIME 2024** — 30 problems, integer answers, no grading ambiguity.
Standard frontier-reasoning eval. Gives pass@1 and lets us measure the
reasoning-token ratio (tokens in `reasoning_content` / total tokens).

```bash
# lm-eval-harness has it built in:
pip install lm-eval[api]
lm_eval --model local-chat-completions \
  --model_args base_url=http://<host>:<port>/v1/chat/completions,model=<served-model-name>,num_concurrent=4 \
  --tasks aime24_nofigures \
  --apply_chat_template --num_fewshot 0
```

Also useful: `gpqa_diamond_zeroshot` (198 questions, ~45 min at our
decode rate).

For gpt-oss specifically, set `reasoning_effort` per request — compare
`low` vs `medium` vs `high` on the same problem set to verify the knob
actually moves quality and at what token-budget cost.

### Suite 5: Coding quality, fast (~15 min)

**HumanEval+** — 164 problems, ~200 output tokens each, pass@1 with
exec sandbox. Fast smoke test for whether tool-using configs got worse.

```bash
pip install evalplus
evalplus.evaluate --dataset humaneval \
  --model <served-model-name> --backend openai \
  --base-url http://<host>:<port>/v1
```

For real signal on coding, **LiveCodeBench v6** (~400 problems, 1–2 hours)
is contamination-resistant. Worth the time when validating a new
"production" config; skip for routine smoke.

### Suite 6: Tool-use accuracy (~20 min)

**BFCL v3** subset — Berkeley Function Calling Leaderboard. Categories:
simple, parallel, multi-turn. Our cluster routes mostly through OpenCode,
which exercises tool calls heavily; a regression here is what users
actually feel.

```bash
git clone https://github.com/ShishirPatil/gorilla
cd gorilla/berkeley-function-call-leaderboard
# follow their README; supports OpenAI-compatible endpoints
```

### Suite 7: Cold-load time (1 measurement per config)

```bash
# Time from `kubectl rollout restart` to "Engine ready" log line.
# For Ollama: time the first /api/generate call on a fresh model load.
# For vLLM: time from container start to /v1/models returning 200.
```

This is the number that justifies (or doesn't) the
NVMe-vs-HDD-storage-class question.

## Recommended cadence

- **On every config change** that touches flags, model variant, or quant:
  Suite 1 + Suite 7. Catches setup regressions in <15 min.
- **On every model swap** (new served model, replacing an old one):
  Suite 1 + Suite 3 + Suite 4 (or Suite 5 for coder models). ~1 hour.
- **Quarterly on stable configs**: full Suite 1–6. Catches drift from
  upstream backend updates.

## Where to put results

Append summary numbers to the tables above. For the underlying run that
produced them:

- **One file per run** at `cluster/docs/inference/runs/YYYY-MM-DD_<config-id>_<suite>.md`.
  Contents: the exact pod manifest (or `kubectl run` command), the eval
  command line, the model/server config the eval saw, raw summary output,
  and a one-line conclusion. This is the "what we actually ran" record so
  the table numbers stay reproducible.
- **Larger raw artifacts** (lm-eval JSONs, BFCL run dirs) go alongside the
  per-run markdown in the same `runs/` directory, or under
  <../../../debug/> with a link from the run file.
- **Then** copy the headline numbers into the tables above and link the row
  back to the run file.

This applies to any one-off pod we use to produce a result we care about,
not just benchmarks — the rule is "if it produced a number we wrote down,
the manifest that produced it lives in the repo."

## See also

- <backend_comparison.md> — engine choice and current state
- <vllm_history.md> — what we learned configuring vLLM the first time
- <qwen3_coder_vram_analysis.md> — VRAM math; throughput numbers came from here
- <../../../x/local_llm/test_long_context.py> — existing NIAH script
