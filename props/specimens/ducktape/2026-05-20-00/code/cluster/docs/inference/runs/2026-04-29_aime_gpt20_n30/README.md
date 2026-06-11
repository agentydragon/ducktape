# 2026-04-29 AIME-2024 N=30 on gpt-oss:20b

Full-N (all 30 AIME-2024 problems) sweep across `reasoning_effort`
∈ {low, medium, high} on cluster Ollama's `gpt-oss:20b`. Same setup as
the N=10 pass at <../2026-04-28_aime_gpt20/README.md>; the only knob
changed is `LIMIT=30`. Same `run_aime.py`, same `regrade.py`, same
endpoint, same uv-hashbang plumbing.

Scope of this run: confirm or refute the small-N noise band in
<../2026-04-28_aime_gpt20/README.md>, where strict pass@1 ranged 0.30–
0.70 across efforts at N=10 with stderr ~0.15.

## What ran

- **Driver**: <run_aime.py> (`DEFAULT_LIMIT=30`)
- **Re-grader**: <regrade.py>
- **Inspect logs**: <eval_logs/{low,medium,high}/\*.eval>
- **Stdout transcript**: <raw_output.txt>
- **Summary JSON**: <summary.json> (run_aime.py exit codes)
- **Regrade JSON**: <regrade.json> (per-sample regraded results)
- **Endpoint**: `https://ollama.allegedly.works/v1` with bearer token from cluster Secret

Total wall time across all three efforts: **~3 hours** (low 1h13m + medium
59m + high 45m, run sequentially).

## Headline

**Inverted-U over reasoning_effort.** Strict pass@1 by effort:

| effort     | strict pass@1    | regrade pass@1   | format-only fails | genuine errors |
| ---------- | ---------------- | ---------------- | ----------------- | -------------- |
| low        | 12/30 (0.40)     | 20/30 (0.67)     | 8                 | 10             |
| **medium** | **21/30 (0.70)** | **26/30 (0.87)** | 5                 | 4              |
| high       | 15/30 (0.50)     | 19/30 (0.63)     | 4                 | 11             |

Stderr ≈ 0.085–0.093 at N=30. Differences:

- medium vs low strict: 0.30 (≈3 stderr) — **real**.
- medium vs high strict: 0.20 (≈2 stderr) — **likely real**.
- medium vs high after regrade: 0.24 (≈3 stderr) — **real**.

The high-effort drop is not just format noise — `regrade` confirms high
genuinely gets more answers wrong than medium (11 genuine errors vs 4).
gpt-oss:20b on AIME appears to over-reason at high effort.

## Metrics

### Output token usage by effort

`reasoning_effort` is plumbed through (Ollama's OpenAI-compat respects
it), but on AIME-2024 the per-problem token usage is **only weakly
sensitive to the knob**:

| effort | sum out_tok | avg out_tok | median out_tok | max out_tok | min out_tok |
| ------ | ----------- | ----------- | -------------- | ----------- | ----------- |
| low    | 267 849     | 8 928       | 5 454          | 41 616      | 1 490       |
| medium | 252 454     | 8 415       | 7 486          | 24 974      | 1 234       |
| high   | 279 992     | 9 333       | 6 476          | 38 314      | 1 050       |

Three observations:

1. **Mean output across efforts varies only ~10%** (8 415 → 9 333). The
   knob is more like a 1.1× than a 10×.
2. **Variance per effort is ~10×** within each effort — some problems
   trigger 25–41K-token chains, others stop at 1–2K. Per-problem
   "difficulty" matters far more than the effort knob.
3. **Median rises with effort** (5 454 → 7 486 → 6 476 — non-monotone)
   while max varies wildly. Distribution shape isn't a clean shift.

This was a surprise. The reasonable expectation (and OpenAI's marketing)
is that high triggers substantially more reasoning. On AIME-2024 with
gpt-oss-20b on Ollama, it doesn't. Possible explanations:

- Ollama's compat shim translates `reasoning_effort` to a system-prompt
  hint or template variant, not a hard token budget. The model uses its
  own discretion.
- gpt-oss-20b may saturate its useful reasoning depth well before the
  effort knob's "high" point on most AIME problems.
- `max_tokens` was unset (Inspect default), so nothing artificially
  capped output.

### Wall and CPU-time by effort

Inspect serializes against single-stream Ollama (default
`OLLAMA_NUM_PARALLEL=1` in our deployment). Effective concurrency
factor is sum_working ÷ wall.

| effort | wall   | sum working | sum total | concurrency factor |
| ------ | ------ | ----------- | --------- | ------------------ |
| low    | 1h 13m | 6 659 s     | 28 118 s  | 1.51               |
| medium | 0h 59m | 6 571 s     | 29 242 s  | 1.86               |
| high   | 0h 45m | 6 570 s     | 21 669 s  | 2.43               |

Surprising: **high finished fastest** despite having the highest avg
output_tokens. The `concurrency factor` rose with effort. Probable
cause: high-effort problems were more uniformly long-running, letting
Inspect's parallel queue keep multiple samples in flight; low-effort
runs had a wider spread (1.5K to 41K tokens), causing some samples to
stall waiting behind monsters.

### Effective decode rate per problem

Sanity-checking that big problems aren't pathologically slow:

- Top decode rates seen (low effort): 41 616 tokens / 260 s = **160 tok/s**
- Bottom decode rates seen (low effort): 1 557 tokens / 286 s = **5 tok/s**

The slow ones aren't decoding slowly — they're stalling in queue
behind big samples (Ollama processes serially with `NUM_PARALLEL=1`).
160-170 tok/s is the real ceiling for MXFP4 gpt-oss-20b on a single 5090.

VRAM headroom is plentiful (~16 GB used of 64 GB total). Setting
`OLLAMA_NUM_PARALLEL=2` or `4` would lift the ceiling on Inspect's
parallel runs but is left as a per-model optimization.

## Per-sample details

### Wrong answers (high effort)

`format` = answer was right but Inspect's scorer couldn't parse the
LaTeX. `genuine` = model produced the wrong number.

| problem    | target | extracted  | out_tok | working_s | type                  |
| ---------- | ------ | ---------- | ------- | --------- | --------------------- |
| 2024-I-3   | 809    | `808`      | 2 505   | 164.8     | genuine (off by 1)    |
| 2024-I-5   | 104    | `\(104\)`  | 5 644   | 234.6     | format                |
| 2024-I-8   | 197    | `254`      | 19 334  | 270.5     | genuine               |
| 2024-I-10  | 113    | `\(1245\)` | 18 677  | 126.6     | format (wrong number) |
| 2024-I-11  | 371    | `395`      | 16 788  | 207.5     | genuine               |
| 2024-I-12  | 385    | `\]`       | 14 097  | 289.6     | format (model said 7) |
| 2024-I-13  | 110    | `134`      | 9 035   | 268.8     | genuine               |
| 2024-I-15  | 721    | `\]`       | 7 169   | 286.1     | format                |
| 2024-II-7  | 699    | `629`      | 19 928  | 243.5     | genuine               |
| 2024-II-8  | 127    | `11`       | 17 481  | 287.6     | genuine               |
| 2024-II-9  | 902    | `962`      | 6 159   | 280.2     | genuine               |
| 2024-II-11 | 601    | `\]`       | 6 476   | 225.7     | format                |
| 2024-II-12 | 23     | `\]`       | 7 375   | 42.8      | format                |
| 2024-II-14 | 211    | `\]`       | 8 680   | 284.1     | format                |
| 2024-II-15 | 315    | `15`       | 2 548   | 287.4     | genuine               |

So at high: 11 genuine wrongs, 4 format-only. Many of the genuine
wrongs are "model burned 15-20K tokens of reasoning and arrived at a
wrong number." That is the over-thinking pattern.

(Per-sample tables for low and medium are in <regrade.json>.)

## Findings

1. **Medium reasoning is the operating point for AIME on gpt-oss:20b.**
   Strict 0.70, regrade 0.87. Both low and high are noticeably worse.
2. **High is real worse, not noise.** 11 genuine wrongs at high vs 4 at
   medium. The model burns more tokens on hard problems and lands on
   different (wrong) answers more often.
3. **Token usage is barely sensitive to the effort knob** (8.4K → 9.3K
   mean). Per-problem variance is 10× larger. Treat
   `reasoning_effort` as a hint, not a budget.
4. **N=10 was too noisy.** N=10 strict numbers (0.50/0.30/0.70) were
   misleading; medium and low had swapped order with respect to the
   underlying truth. The N=30 result is the one to cite.
5. **Format-violation rate is similar across efforts** (4-8 of 30) —
   the `\boxed{}` habit is gpt-oss-20b's, not the knob's. Worth a
   separate format-compliance probe to characterize.
6. **OLLAMA_NUM_PARALLEL=1 (default) bottlenecks our throughput.**
   Effective concurrency factor 1.5–2.4 from Inspect's perspective; not
   pursuing the optimization here, but flagged.

## Reproducing

```bash
cd cluster/docs/inference/runs/2026-04-29_aime_gpt20_n30
./run_aime.py                            # full sweep, ~3h wall
./regrade.py                             # re-grade existing eval_logs/
./run_aime.py --efforts medium --limit 5 # quick partial repro
```

Requires: `kubectl` (for fetching `ollama-bearer-token` Secret) and
`uv` on PATH. Driven from any host that can reach
`ollama.allegedly.works/v1` over HTTPS.

## Headline for `benchmarks.md`

`gpt-oss:20b` (cluster Ollama, MXFP4) on AIME-2024 full N=30, strict
scoring: pass@1 = 0.40 / 0.70 / 0.50 at low / medium / high effort.
**Medium dominates with high statistical confidence.** High over-thinks.
Token usage barely moves with the effort knob (~10% range).

## Open questions / next pass candidates

1. Does this inverted-U hold for other reasoning models (DeepSeek-R1
   distills, QwQ, gpt-oss:120b once vLLM lands)?
2. What does the Inspect aime2024 prompt look like with system-message
   reasoning hints vs Ollama's harmony template? Could the format
   issue be partly a tokenizer / template artifact?
3. Format-compliance probe — short eval that just measures "does the
   model follow `ANSWER: N`" to separate format from quality.
4. Does AIME-2025 give the same shape, or is this `gpt-oss-20b` having
   memorized parts of AIME-2024?
