# PM-reifier spike: can an LLM emit augur-shaped trajectories as a _base measure_?

Throwaway experiment for `augur/plans/interpolating_prediction_markets.md`. **Not production
code** — it lives in `x/` and is not Bazel-built.

## The question

The plan reifies prediction-market _marginals_ into a sampleable _joint_ over trajectories by
min-KL projection from a **base measure `Q`**. `Q` can be our structured models or an LLM. This
spike asks: **can an LLM be `Q`** — emit a diverse cloud of trajectories _in augur's native shape_
that, after **one max-ent reweight to the market prices**, match those prices _without the
effective sample size collapsing_? (Collapsing ESS = the LLM never proposed worlds in the
market-implied region, so reweighting is fiction.)

## augur's native trajectory shape

augur's macro model (`augur/model/state_space.py`) emits a **dense monthly level path per factor**,
shape `(rollout, horizon_months+1, factors)`, factors being augur wire-ids: `inflation` (CPI index),
`sp500`, `crypto:BTC`, `home_value:<loc>`, `rent:<loc>`, plus private-equity issuer marks. So the
LLM is asked for exactly that: **dense monthly paths** over those series (no annual knots, no
post-hoc interpolation — month resolution end to end) plus one PE issuer (OpenAI: monthly valuation
path + IPO month). Market thresholds are evaluated at specific **month indices** on the dense paths.

## What it does (`run_spike.py`)

- `pick_model()` probes a **cheapness-ordered** candidate list (free GLM-4.7-Flash / 4.5-Flash on the
  general endpoint first, then GLM-4.7-FlashX `$0.07/$0.40`, then coding-plan models) and uses the
  first that answers. The coding-plan key reaches the free general endpoint, so the run is **$0**.
- prompts the picked model (`thinking` disabled, `json_object`) for `4 × 4 = 16` scenarios per
  variant, each a dense monthly trajectory (horizon 57 months, 2026-06 → 2031-03 — just past the
  furthest market).
- two variants: **unconditioned** (the LLM's own world-prior — the real base-measure test) and
  **conditioned** (the crowd prices are shown — an upper bound on calibration).
- computes each market's model probability = fraction of scenarios satisfying it, **reweights** the
  empirical sample (`w_i ∝ exp(Σ λ_m·indicator)`, ridge-softened) to match the crowd prices, and
  reports **ESS = 1/Σwᵢ²**, **length discipline** (how many paths hit the exact horizon), and
  month-to-month **smoothness**.
- logs every request+response to `transcripts/`, scenarios + summary to `results/`, and the z.ai
  weekly-quota delta.

Run: `python3 augur/x/pm_reifier/run_spike.py` (key from `$ZAI_API_KEY` or `/tmp/zai_key`, mirrored
from the `claude-sandbox` `zai-api-key` secret). Market prices here are **illustrative** plausible
values, not pulled live.

## Findings (2026-06-04, free GLM-4.7-Flash, dense monthly, 32 scenarios)

**Local form is good; the blockers are length discipline and sample size.**

| metric                                | unconditioned | conditioned |
| ------------------------------------- | ------------: | ----------: |
| valid scenarios                       |    **6 / 16** | **10 / 16** |
| paths at exact length (of 80)         |         **1** |      **11** |
| path length min / median (exp 58)     |       54 / 60 |     53 / 56 |
| mean / p95 max monthly \|log-return\| |   0.06 / 0.12 | 0.17 / 0.40 |
| ESS (of valid)                        |     3.5 (59%) |   3.9 (39%) |

- **Length discipline is the dominant failure.** The model will _not_ count array entries: only
  **1/80** (unconditioned) and **11/80** (conditioned) arrays hit the requested 58 entries; lengths
  scatter from 53 to 60+ across the 6 series _within a single scenario_. Any scenario with one path
  too short to reach the furthest market month is dropped — that alone discards **~⅔** of the sample.
- **Paths are locally well-formed.** When valid, month-to-month moves are smooth (mean max monthly
  \|log-return\| ~6% unconditioned) with sane cross-asset co-movement — no teleporting. Conditioning
  on crowd prices makes paths **jumpier** (0.06 → 0.17 mean, p95 0.40) as the model forces tail
  outcomes to hit the probabilities.
- **Reweighting is directionally right but sample-starved.** Raw marginals pull toward the targets
  after the tilt (e.g. `sp500>6000` 0.67 → 0.53 against a 0.55 target). But with only 6–10 valid
  scenarios the indicator columns of correlated markets become **collinear**, so paired markets
  collapse to a shared value the reweight can't separate — `sp500>6000@2027` and `>7500@2030` both
  land at 0.53/0.56, the BTC pair both at 0.43. The binding constraint is **too few valid scenarios
  to satisfy 9 marginals independently**, not ESS geometry.
- **Cost: negligible.** 32 scenarios = **64,682 tokens**, all on the **free** tier; z.ai weekly quota
  `2% → 2%` (**0 pp**). `thinking: disabled` zeroes reasoning tokens. Dense paths are token-heavy
  (~8k tokens / 90–170 s per 4-scenario call), but free.

### Read

Dense raw-path emission lands in augur's representation and is _locally_ faithful (smooth, coherent),
but two things make it impractical as-is:

1. **No length discipline** — the model can't reliably emit fixed-length arrays, silently shredding
   the usable sample. A real pipeline would need to **repair/resample lengths** (truncate/pad/interp
   to the grid) before anything downstream, or stop asking the LLM to count.
2. **Sample economics** — independently matching `K` marginals needs many more valid scenarios than
   dense emission cheaply yields (~⅔ wasted to (1), ~8k tokens each).

Both point the same way: have the LLM emit something **augur can densify deterministically** rather
than raw monthly arrays — i.e. a regime-segmented monthly **drift/vol/correlation** schedule
(`monthly_log_return_mu` / `_cov`, augur's own generative primitive), expanded to dense paths by the
state-space roller. That removes the counting problem entirely and makes each scenario cheap. Next
run: **monthly knots done that way**, more scenarios, real catalog prices, and a structured-`Q`
baseline to compare ESS/coverage against.
