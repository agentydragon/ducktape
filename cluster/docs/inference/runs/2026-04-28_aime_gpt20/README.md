# 2026-04-28/29 AIME-2024 small-N on gpt-oss:20b

First quality-eval pass against the cluster Ollama deployment. AIME-2024
problems 1–10 (the dataset's first 10 of 30, ID order from the HF copy)
across `reasoning_effort` ∈ {low, medium, high}, gpt-oss:20b, single
concurrency. Driven from out-of-cluster against the public
`https://ollama.allegedly.works/v1` endpoint with the bearer token from
the in-cluster Secret — no in-cluster Job needed.

## What ran

- **Driver**: <run_aime.py> (uv hashbang script, sweeps `--efforts`).
  Calls `inspect eval inspect_evals/aime2024 --model openai/gpt-oss:20b`
  once per effort level via the Inspect AI CLI.
- **Re-grader**: <regrade.py> (uv hashbang). Reads each
  `eval_logs/<effort>/*.eval`, applies a permissive answer extractor
  (handles `\boxed{N}`, `\(N\)`, `$N$`, `ANSWER: N`), writes
  <regrade.json>, prints a markdown summary.
- **Inspect logs**: <eval_logs/{low,medium,high}/\*.eval> — canonical
  Inspect AI artifact, contains every prompt, response, score, and
  per-sample timing/usage. Read with `inspect log dump <path>` (run
  inside the uv venv).
- **Raw stdout**: <raw_output.txt> (Inspect's summary + `inspect eval`
  stdout, low effort onwards).

### Configuration

| Knob               | Value                                                                                                                                                                   |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Model              | `gpt-oss:20b` via `https://ollama.allegedly.works/v1` (bearer-token auth)                                                                                               |
| Eval               | `inspect_evals/aime2024` (Inspect AI 0.3.x), task version 3                                                                                                             |
| Dataset            | `Maxwell-Jia/AIME_2024`, first 10 problems, no shuffle, 0-shot                                                                                                          |
| `reasoning_effort` | swept low / medium / high (passed via Inspect's `--reasoning-effort`)                                                                                                   |
| `max_tokens`       | unset (Inspect default; observed cap not hit — `limit: None` on every sample)                                                                                           |
| Concurrency        | Inspect default (parallel; wall_time < sum of per-sample working_time)                                                                                                  |
| Prompt template    | "Solve … the last line of your response should be of the form `ANSWER: $ANSWER` … you do not need to use a `\boxed` command." (verbatim from Inspect's `aime2024` task) |

## Results

Headline numbers — **strict (Inspect's stock `aime_scorer`) is the
primary**. It penalizes format-violation, which is a real model
capability gap that affects production. The re-graded number is a
diagnostic: it shows how many "incorrect" responses are mathematically
right but formatted in `\boxed{N}` / `\(N\)` despite the prompt
explicitly forbidding `\boxed`.

| effort | inspect pass@1  | regrade pass@1 | format-only fails | avg out_tok | median out_tok | max out_tok | sum working_s | sum total_s | wall (eval CLI) |
| ------ | --------------- | -------------- | ----------------- | ----------- | -------------- | ----------- | ------------- | ----------- | --------------- |
| low    | **5/10 (0.50)** | 7/10 (0.70)    | 2                 | 8 186       | 3 823          | 29 055      | 1 370         | 4 081       | 10:36           |
| medium | **3/10 (0.30)** | 8/10 (0.80)    | 5                 | 9 303       | 7 522          | 27 791      | 1 850         | 3 055       | 9:22            |
| high   | **7/10 (0.70)** | 7/10 (0.70)    | 0                 | 14 187      | 4 959          | 40 384      | 2 249         | 5 868       | 16:01 (est.)    |

Stderr at N=10 is ~0.15. All three strict numbers are within
~1 stderr of each other; treat as ~0.5–0.7 noise band.

### Per-sample (strict) details

Source: <regrade.json> (the script also extracts everything below
straight from each `.eval` log).

`C` = correct per Inspect; `I` = incorrect per Inspect. `regrade ✓` =
permissive extractor agrees with target. `extracted` is what Inspect's
scorer pulled from after `ANSWER:`.

#### low

| problem    | target | inspect | extracted | regrade ✓   | out_tok | working_s |
| ---------- | ------ | ------- | --------- | ----------- | ------- | --------- |
| 2024-I-2   | 25     | C       | `25`      | ✓           | 1 349   | 42.8      |
| 2024-I-3   | 809    | I       | `808`     | ✗ (genuine) | 2 290   | 13.7      |
| 2024-I-4   | 116    | C       | `116`     | ✓           | 1 247   | 18.6      |
| 2024-I-8   | 197    | I       | `273`     | ✗ (genuine) | 9 609   | 270.6     |
| 2024-I-11  | 371    | C       | `371`     | ✓           | 29 055  | 216.7     |
| 2024-I-12  | 385    | I       | `10`      | ✗ (genuine) | 23 492  | 159.4     |
| 2024-II-4  | 33     | C       | `33`      | ✓           | 2 741   | 297.0     |
| 2024-II-6  | 55     | C       | `55`      | ✓           | 2 187   | 282.4     |
| 2024-II-11 | 601    | I       | `\]`      | ✓ (format)  | 6 071   | 33.5      |
| 2024-II-12 | 23     | I       | `\]`      | ✓ (format)  | 3 823   | 35.3      |

#### medium

| problem    | target | inspect | extracted | regrade ✓                 | out_tok | working_s |
| ---------- | ------ | ------- | --------- | ------------------------- | ------- | --------- |
| 2024-I-2   | 25     | C       | `25`      | ✓                         | 2 596   | 212.9     |
| 2024-I-3   | 809    | C       | `809`     | ✓                         | 3 222   | 254.4     |
| 2024-I-4   | 116    | C       | `116`     | ✓                         | 1 969   | 106.9     |
| 2024-I-8   | 197    | I       | `1073`    | ✗ (genuine)               | 18 248  | 212.5     |
| 2024-I-11  | 371    | I       | `\(371\)` | ✓ (format)                | 17 423  | 97.6      |
| 2024-I-12  | 385    | I       | `\]`      | ✗ (genuine, model said 7) | 27 791  | 199.6     |
| 2024-II-4  | 33     | I       | `\]`      | ✓ (format)                | 1 642   | 238.2     |
| 2024-II-6  | 55     | I       | `\]`      | ✓ (format)                | 3 796   | 230.7     |
| 2024-II-11 | 601    | I       | `\(601\)` | ✓ (format)                | 7 522   | 36.3      |
| 2024-II-12 | 23     | I       | `\]`      | ✓ (format)                | 8 817   | 260.6     |

#### high

(See <regrade.json>; all 7 of 10 correct also pass regrade — `high`
appears to follow the format consistently.)

## Findings

1. **Strict pass@1 is in a 0.3–0.7 band at N=10** — too noisy to
   distinguish effort levels. Need to rerun on the full 30-problem set
   (or larger) before drawing conclusions about which effort is "best".
2. **Format compliance varies wildly with effort** — `low` violates
   format on 2/10 samples, `medium` on 5/10, `high` on 0/10. Counter-
   intuitive: the model is _less_ willing to follow "no `\boxed`" at
   medium effort than at high. Possibly an Ollama / Harmony-template
   interaction; unverified.
3. **`reasoning_effort` IS plumbed through** — average output tokens
   per problem rise low (8 186) → medium (9 303) → high (14 187).
   Not a dramatic 10× knob, but a clear effect, so Inspect's
   `--reasoning-effort` is reaching the model via Ollama's OpenAI-compat.
4. **Token budget never hit** — `limit: None` on every sample, even
   the 40 384-output-token high-effort run. So we're measuring true
   reasoning length, not truncation. (Worth keeping the implicit cap in
   mind: Inspect's default for chat-completions models, not 16k.)
5. **Inspect's `aime_scorer` is fragile.** It greps the substring after
   `ANSWER:` and exact-matches an integer; it can't see `\boxed{N}` or
   `\(N\)` or `$N$`. For models that don't follow format perfectly this
   underestimates pass@1 by 0.0–0.5 (here, biggest gap on `medium`).
   Worth filing upstream; in the meantime <regrade.py> documents the
   workaround.
6. **Wall time per sample is variance-dominated, not effort-dominated.**
   Hardest problems (2024-I-11) at low effort took 217s (29K tokens);
   the same problem at medium took 98s (17K tokens). N=1 per problem is
   not a serious latency benchmark.

## Reproducing

```bash
cd cluster/docs/inference/runs/2026-04-28_aime_gpt20
./run_aime.py                        # full sweep low,medium,high × 10
./regrade.py                         # re-grade existing eval_logs/
./run_aime.py --efforts high --limit 5   # quick partial repro
```

Requires: `kubectl` (for fetching the `ollama-bearer-token` Secret) and
`uv` on PATH. Driven from any host that can reach
`ollama.allegedly.works/v1` over HTTPS.

## Headline finding for `benchmarks.md`

`gpt-oss:20b` (cluster Ollama, MXFP4) on AIME-2024 first-10 problems,
strict scoring: pass@1 ≈ 0.3–0.7 at N=10 across reasoning_effort levels.
Format compliance is the dominant signal; medium effort is the worst at
following the "no `\boxed`" instruction.

## Next pass candidates

1. **Full N=30 sweep** at one effort to resolve the noise band.
2. **Format-compliance probe** as a separate eval — give the model a
   trivial problem and instrument adherence to "ANSWER: N", varying
   effort. Smaller, faster signal than AIME for that question.
3. **Try the same eval against a stricter model** (e.g. DeepSeek-R1 or
   Qwen3-Reasoning, once we have one loaded) to see whether the
   format-violation pattern is gpt-oss-specific or general.
4. **Patch Inspect's `aime_scorer`** with the regrade extractors and
   send a PR upstream.
