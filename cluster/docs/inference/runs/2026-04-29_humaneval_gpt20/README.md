# 2026-04-29 HumanEval N=164 on gpt-oss:20b

Full-N (all 164 HumanEval problems) sweep across `reasoning_effort` ∈
{low, medium, high} on cluster Ollama's `gpt-oss:20b`. Mirrors the
AIME-2024 N=30 sweep at <../2026-04-29_aime_gpt20_n30/>; the differences
are the task (`inspect_evals/humaneval` vs `inspect_evals/aime2024`),
the limit (164 vs 30), and the addition of `--sandbox docker` (HumanEval
scores by executing model-generated Python in a sandbox; binary pass /
fail per problem; no regrade pass needed).

This is the P0#1 item from <../../TODO.md#1-standard-coding-benchmark-on-gpt-oss20b>.

## Goals

Two questions to answer:

1. **Is `gpt-oss:20b` decent at AI-powered coding?** Public HumanEval
   leaderboards put strong instruction-tuned 20–30B models in the
   70–95% range. A number in that band confirms the model we already
   serve is fine for everyday coding-assistant use. Below that, we
   should reach for an alternative.
2. **Does the AIME inverted-U on `reasoning_effort` replicate for
   coding?** The AIME N=30 finding was that **medium** dominates and
   **high** over-thinks (medium 0.70, high 0.50, low 0.40 strict
   pass@1). If coding shows the same shape, "default to medium" is the
   broader recommendation. If coding prefers low or high, the optimal
   default depends on workload.

## What ran

- **Driver**: <run_humaneval.py> (`DEFAULT_LIMIT=164`, `--sandbox docker`).
- **Inspect logs**: <eval_logs/{low,medium,high}/\*.eval> (all
  transcripts + per-sample timings/usage; read with `inspect log dump`).
- **Stdout transcript**: <raw_output.txt>.
- **Summary JSON**: <summary.json> (per-effort exit codes).
- **Endpoint**: `https://ollama.allegedly.works/v1` with bearer token
  from the in-cluster Secret.
- **Sandbox**: `docker` (Inspect default for HumanEval). Per-sample
  Python execution, 30 s timeout per sample (humaneval task default).

### Configuration

| Knob               | Value                                                                                            |
| ------------------ | ------------------------------------------------------------------------------------------------ |
| Model              | `gpt-oss:20b` via `https://ollama.allegedly.works/v1` (bearer-token auth)                        |
| Eval               | `inspect_evals/humaneval` (Inspect AI), original 164-problem dataset (`openai/openai_humaneval`) |
| HumanEval+         | **not used** — `inspect_evals` doesn't ship EvalPlus's augmented test fixture                    |
| `reasoning_effort` | swept low / medium / high                                                                        |
| `max_tokens`       | unset (Inspect default)                                                                          |
| Sandbox            | `docker` (Inspect's default Python container, 30 s execution timeout per sample)                 |
| Concurrency        | Inspect default (limited by deployment's `OLLAMA_NUM_PARALLEL=1`)                                |
| Scoring            | binary pass/fail by test execution; `pass@1 = correct / 164`                                     |

Total wall time across all three efforts: **~39 min** (low 16:00 incl.
~4 min Docker image pull on first run, medium 11:34, high 11:33).

## Headline

**`gpt-oss:20b` is excellent at HumanEval.** All three effort levels
land in the saturation band (95.7% – 97.0%); medium and high are tied
within stderr.

| effort | pass@1              | stderr | total tokens (in / out) | wall (eval CLI) |
| ------ | ------------------- | ------ | ----------------------- | --------------- |
| low    | **0.957** (157/164) | 0.016  | 171 K (37 K / 134 K)    | 16:00 (\*)      |
| medium | **0.970** (159/164) | 0.013  | 166 K (37 K / 129 K)    | 11:34           |
| high   | **0.970** (159/164) | 0.013  | 160 K (37 K / 123 K)    | 11:33           |

(\*) Includes ~4 min one-time pull of Inspect's Python sandbox image.

Stderr at N=164 is ~0.013–0.016 (binomial). The medium-vs-low gap
(0.013) is ~1 stderr — within noise, not a real difference. Medium and
high are identical. **No inverted-U replication possible** — there's
not enough headroom (4.3% above low) to see the shape AIME showed.

## Metrics

### Output token usage by effort

Counter-intuitive: **higher effort uses fewer output tokens.**
Aggregate output across 164 problems decreases monotonically:
134 K (low) → 129 K (medium) → 123 K (high). Same direction the AIME
N=30 run hinted at — `reasoning_effort` is not a strong "more
thinking" lever for `gpt-oss:20b`. On easy problems (HumanEval) higher
effort actually trims fluff.

### Wall and effective decode rate

After the first-run image pull, all three efforts ran in **~11.5 min**
for 164 samples = ~4.2 s per sample average. Inspect's parallel queue
yields effective concurrency of ~2× over the deployment's
`OLLAMA_NUM_PARALLEL=1` ceiling. Decode rate consistent with the
`gpt-oss:20b` MXFP4 ceiling on a single 5090 (~150–180 tok/s).

### Failure mode breakdown

Not extracted in this pass — see TODO at end of doc. With only 5–7
failures per effort and ~95% saturation, the per-sample analysis is
low-priority.

## Findings

1. **`gpt-oss:20b` is decent for AI-powered coding** — at least on
   HumanEval-shaped problems (function-level Python, well-defined
   spec, hand-written tests). 96–97% pass@1 is at the saturation
   ceiling for this benchmark.
2. **HumanEval is saturated for this model** — too saturated to test
   the AIME inverted-U on coding. Need a less-saturated eval
   (BigCodeBench or LiveCodeBench, TODO P1#3 / new follow-on) to
   resolve "does the inverted-U hold for coding."
3. **`reasoning_effort` does not increase token usage on easy
   problems** — same finding as AIME, even more pronounced here:
   higher effort actually decreases aggregate output. Treat the knob
   as advisory, not a budget.
4. **Wall time is much faster than estimated.** ~12 min per effort at
   N=164 (after first-run image pull), not the ~30 min I'd projected
   from AIME. Coding tasks generate shorter responses; saturation
   means most samples finish quickly without long reasoning chains.

## Reproducing

```bash
cd cluster/docs/inference/runs/2026-04-29_humaneval_gpt20
./run_humaneval.py                            # full sweep, ~2h wall
./run_humaneval.py --efforts medium --limit 5 # quick partial repro
```

Requires: `kubectl` (for fetching the `ollama-bearer-token` Secret),
`uv` on PATH, and **Docker on the host** (for the sandbox). Driven from
any host that can reach `ollama.allegedly.works/v1` over HTTPS.

## Headline for `benchmarks.md`

`gpt-oss:20b` (cluster Ollama, MXFP4) on stock HumanEval N=164,
execution-graded: pass@1 = 0.957 / 0.970 / 0.970 at low / medium /
high `reasoning_effort`. Saturation band; effort levels not
distinguishable. Need BigCodeBench or LiveCodeBench for finer
discrimination.

## Caveats

- **HumanEval is saturated for modern models.** If `gpt-oss:20b` lands
  ≥90%, this single eval can't discriminate against a hypothetical
  better model. Follow-on (TODO P1#3): rerun with BigCodeBench (1 140
  problems, broader stdlib) or LiveCodeBench (contamination-resistant)
  for the model-comparison pass.
- **Contamination risk.** HumanEval has been public since 2021;
  gpt-oss-20b's training likely saw solutions. Acceptable for "is this
  decent" — not as ranking ground-truth.
- **First-run Docker pull.** Inspect's default Python sandbox image
  pulls once; that time is not "real eval time" and shouldn't be read
  as model latency.
- **OLLAMA_NUM_PARALLEL=1 bottleneck** carries over from the AIME run.
  Not optimizing here.

## Open questions / next pass candidates

1. **BigCodeBench (Complete) at N=164** — the immediate follow-on
   for less-saturated discrimination. See <../../TODO.md>.
2. **SWE-bench pilot** — agentic coding eval, started at
   <../2026-04-29_swebench_pilot_gpt20/> as a 1-problem smoke test
   for tool-call reliability before any larger run.
3. **Failure-mode breakdown** — parse `eval_logs/<effort>/*.eval` to
   classify each fail as timeout / exception / wrong logic and
   identify problem overlap across efforts. Low priority at this
   saturation; do if BigCodeBench result raises questions.
4. **Same eval against an alternative model** — TODO P1#3 in the
   inference roadmap.
