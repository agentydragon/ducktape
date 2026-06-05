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

## Windowed variant (`run_windowed.py`): conversational rollout

Same markets + reweight harness, but each world is a **conversation** advancing `W = 12` months per
turn; we concatenate fixed-size windows, so the horizon length is enforced by **us** (retry a window
that miscounts) rather than begged from the model. Each world is its own conversation (an independent
draw; `BATCH_SIZE > 1` rolls deliberately-distinct worlds per thread as a coverage knob), run in
parallel with 429/timeout backoff. The opening prompt seeds a **12-month recent-history tail** per
series (`HISTORY`) so worlds start from real levels and carry recent momentum/volatility forward — in
real augur usage this is the as-of series tail from augur's data store (and anchoring as-of a past
date is what enables rolling-origin backtests). The tail here is illustrative.

### Findings (2026-06-05, free GLM-4.5-Flash, 15 worlds, 60-month horizon, 12-month windows)

| metric                          | one-shot dense |             windowed |
| ------------------------------- | -------------: | -------------------: |
| valid scenarios                 |      6–10 / 16 |            **15/15** |
| paths at full length            |      1–11 / 80 |            **75/75** |
| output tokens / world           |          ~2.0k |                ~2.0k |
| input tokens / world            |         ~0.56k |            **~6.5k** |
| ESS (of valid)                  |        3.5–3.9 |                  6.3 |
| mean max monthly \|log-return\| |      0.06–0.17 |                 0.09 |
| length-enforcement cost         |              — | 2 retries / 79 calls |

- **Length discipline is solved.** Chunking to 12 months and retrying a miscounted window yields
  **75/75 full-length paths** (every path exactly 61) vs 1–11/80 one-shot. The model miscounted only
  **twice in 79 windows**; the retry fixed both. Nothing dropped — **15/15 valid**.
- **The cost moved to input tokens.** This run kept the full conversation thread (stateful), so each
  window re-reads the growing history → **~6.5k input tokens/world** (97k total), ~10× the one-shot
  prompt and ~3× this run's own output. Output/world is unchanged (~2k) because the count of emitted
  numbers is fixed. The one-shot's length-counting problem became a context-management problem —
  exactly the predicted trade.
- **More valid scenarios eased sample-starvation.** With 15 valid (vs 6–10), correlated paired
  markets no longer perfectly collapse — `sp500>6000`/`>7500` separated to 0.65/0.52 (identical
  one-shot), the BTC pair to 0.36/0.26. Reweight fidelity improves with sample size.
- **Free, 0 pp quota** (127k tokens; `glm-4.7-flash` was 429-saturated, so the probe fell to
  `glm-4.5-flash`).

### Paid coding tier (GLM-4.7): faster, but a worse base measure

Re-run on the **paid coding endpoint** (`glm-4.7`) to escape free-tier 429s: median **9 s/window**
(vs 25–170 s throttled on flash), **0 retries over 80 windows** (it counts to 12 flawlessly), 16/16
valid, 80/80 full length. Burn: 138k tokens moved the **5 h** quota +1 pp and the **weekly** quota
0 pp (below the integer-% resolution) — negligible against a 50% cap. Tracked in `quota_log.jsonl`
(the token-quota API exposes only an integer %, so we pair it with our own token totals).

But `glm-4.7` is a **worse base measure** than `glm-4.5-flash` here — it is too conservative:

```
btc>150k@2027-12   target 0.50   raw 0.00   reweighted 0.00
cpi>110@2029-12    target 0.60   raw 0.00   reweighted 0.00
```

Zero of 16 worlds cross BTC>150k or CPI>110 (smoothness 0.05 — very tight, no tails). When the base
measure puts **zero mass** on a market's region the indicator column is all-zeros and **no reweight
can lift it** — you cannot upweight worlds the model never proposed. That is the concrete form of
"ESS collapse = reweighting is fiction." The sloppier flash model actually _covered_ more (BTC raw
0.27 vs 0.00). **Lesson: a base measure wants dispersion/coverage, not calibration** — a "smarter"
model that hugs the median is the wrong tool. Levers to force coverage: higher temperature,
conditioning on the prices, or the `BATCH_SIZE > 1` distinct-worlds knob.

### Real macro data + OpenAI as events (GLM-4.7, paid)

Grounded run: macro series seeded from **real** recent tails (`fetch_real_history.py` — Yahoo
sp500/BTC, FRED CPI/Case-Shiller-SF/rent), so worlds start from the real 2026 anchors (sp500 ~7584,
BTC ~63k mid-drawdown, indices rebased to 100) with real momentum; OpenAI modelled as augur's PE
issuer is — discrete **events** (`primary_round`/`secondary_tender`/`ipo`/`collapse`) emitted per
window, fed OpenAI's **public** funding history (`openai_history.json`; private marks excluded).
15/15 valid, 75/75 full length, 0 worlds dropped (5xx retried), 66 OpenAI events over 15 worlds.
Burn: 191k tokens, weekly **0 pp** (three runs total ≈ 0 pp weekly, ~1% on the 5 h window; see
`quota_log.jsonl`).

| market                   | target |  raw | reweighted |
| ------------------------ | -----: | ---: | ---------: |
| sp500>8500@2027-12       |   0.55 | 0.00 |       0.00 |
| sp500>11000@2030-12      |   0.40 | 0.07 |       0.26 |
| btc>90k@2027-12          |   0.50 | 0.00 |       0.00 |
| btc>180k@2030-12         |   0.30 | 0.00 |       0.00 |
| cpi>108@2029-12          |   0.70 | 0.73 |       0.78 |
| sfhome>110@2030-12       |   0.50 | 0.13 |       0.41 |
| sfrent>108@2029-12       |   0.60 | 0.27 |       0.60 |
| openai_tender_by_2028-06 |   0.80 | 0.87 |       0.89 |
| openai_ipo_by_2029-12    |   0.45 | 0.67 |       0.53 |
| openai_val>2T@2030-12    |   0.50 | 0.13 |       0.36 |

- **OpenAI-as-events is the right shape.** Discrete tenders/rounds/IPO match augur's PE bundle; the
  tender-by and ipo-by markets are well-covered and reweight cleanly, and even the $2T-valuation tail
  now gets 0.13 raw (vs 0.00 under the smooth-path model). This was your call and it lands.
- **Upside coverage is still the binding blocker.** GLM-4.7 under-generates upside tails: **zero**
  worlds recover BTC to $90k (from $63k) within 18 mo or reach $180k in 5 yr, and sp500 doesn't clear
  +12% by 18 mo — so those markets stay raw 0.00 and the reweight cannot lift them. The steady
  climbers (CPI, rent, home) and the OpenAI events match well. Same lesson, sharper: **coverage /
  dispersion is the constraint, not calibration — a conservative model can't be reified onto the
  upside.**
- **Two bugs this surfaced and fixed:** the model mean-reverted the CPI _index_ around 100 instead of
  climbing it (relabeled as a cumulative price level → `cpi>108` coverage 0.00 → 0.73); transient
  `503 DNS resolution failure`s dropped worlds (now retried as 5xx).

### History context length (1 yr → 5 yr)

Extending the recent-history tail from 12 to **60 months** (`fetch_real_history.py` N=60; the model now
sees BTC's full 2021 boom → 2022 crash → 2025 peak → 2026 drawdown and the post-2021 inflation/rent/home
climb) modestly **widens coverage**, mostly on the steady climbers and the near-zero markets:

| market (raw)       | 1 yr | 5 yr |
| ------------------ | ---: | ---: |
| sp500>8500@2027-12 | 0.00 | 0.06 |
| btc>90k@2027-12    | 0.00 | 0.06 |
| cpi>108@2029-12    | 0.73 | 0.94 |
| sfhome>110@2030-12 | 0.13 | 0.38 |
| sfrent>108@2029-12 | 0.27 | 0.38 |
| openai_ipo_by_2029 | 0.67 | 0.81 |

The multi-year drift is now grounded in the real trend (CPI/rent/home), and S&P/BTC upside moves off
exactly-zero. But the **extreme tails stay uncovered** (btc>180k, openai_val>2T both 0.00) — a full
cyclical history still isn't enough to make GLM-4.7 imagine the biggest booms. Cost: input ~doubles
(60-month history carried in every stateful window → 271k tokens), still 0 pp weekly. See
`plot_rollouts.py` / `results/rollouts.png` for the visual.

### Next: force coverage, then compact handoff

**Force upside coverage first** — it is the binding blocker across every run. Levers: raise
temperature, condition on the market prices (so the model is told to include the boom tails),
`BATCH_SIZE > 1` distinct-worlds, or a less median-hugging model. Without coverage the reify is a
fiction on exactly the markets that matter most (the upside).

Then **compact handoff**: the input tokens (now ~150k, ~10k/world) are pure statefulness — each
window re-reads the growing thread. A fresh prompt per window carrying only the previous window's
ending levels + a one-line regime note (not the whole thread) should cut input toward ~1–2k/world
while keeping the long arc via the regime note, plus prompt-caching the shared prefix.

### Per-step kernel sampling (planned `run_kernel.py`)

Sample the rollout **one step at a time as a categorical-mixture proposal**: at each step ask the LLM
for N weighted options for the next step, draw one (keep its weight), append to a **compact history
rebuilt into the first message**, repeat — ≤3 messages, no accumulating back-and-forth. This is the
LLM as an explicit stochastic transition kernel `p(x_{t+1} | x_{≤t})`. Why: enumeration ("list
diverse possibilities") beats implicit sampling (which collapses to the mode — our conservatism);
LLM-proposes/you-draw removes the bad-RNG failure; per-step branching compounds into diverse paths.

**Each of the N options is a _joint_ cross-section** — the next single data point for _every_ series
we model, plus any sparse events that fire that step (OpenAI tender / round / IPO / collapse). The
options are samples of the whole next-step vector, so cross-series dependence (a risk-off step hits
equities and crypto together; a tender coincides with a valuation jump) is captured _within each
draw_, not stitched together from independent per-series marginals.

Design decisions:

- **No explicit latent state.** The LLM re-infers the latent (regime/vol) from the history each step,
  exactly as it infers the unseen latent on step 1. Carrying one explicit latent would condition on a
  single draw and _shrink_ dispersion; re-inferring marginalizes over latent uncertainty → more
  honest spread. (Watch: regime persistence vs over-switching — empirically checkable.)
- **Stable cache prefix.** Order `[system+schema]` → `[growing compact history]` → `[ask]`, and **drop
  the per-world seed** (diversity comes from the RNG draws, not a prompt token) so the prefix is
  byte-identical across worlds. The history is large (5-yr context + growing path ⇒ ~271k prompt
  tokens observed), so caching it is load-bearing, not second-order.
- **z.ai caches token prefixes _within_ a message (measured, `cache_probe.py`).** A ~12.5k-token
  prefix shared between two requests differing only in the trailing suffix is cached
  (`cached_tokens` 0 cold → 12544 on a differing-suffix follow-up). So the rebuilt-first-message
  growing prefix gets cached → the scheme is cheap.

Ways the LLM can express the per-step distribution:

- **Sample (N weighted options)** — default. Non-parametric: multimodal / skew / fat tails + discrete
  events (tenders/IPOs) native; hands you draws directly. Cost: small N under-resolves the deep tail;
  needs the weights for an honest density.
- **Parametric (Gaussian / Student-t / mixture)** — resolvable and cheap to over-draw, but imposes a
  shape (Gaussian's thin symmetric tails are wrong for finance) and reduces the LLM to
  coefficient-picking.
- **Percentiles / quantile function** — ask for the value at percentile `p`. A fixed grid (p10..p90)
  is smooth but _truncates the tail_ (never draws beyond the grid). **Random-percentile** (draw
  `u ∼ U(0,1)`, ask for the `u`-th quantile) is inverse-transform sampling via the LLM, and drawing
  `u` from a tail-weighted distribution gives **guaranteed tail coverage + built-in importance
  weights** — the cleanest tail-coverage lever, vs the N-sample which only covers a tail if the LLM
  volunteers it. Caveat: a quantile is scalar → awkward for the multivariate+discrete joint step
  (works per-component or for a scalar severity); the N-sample handles the joint natively. Hybrid:
  N-sample for the joint, with an occasional "give me a p99-tail scenario" request to force coverage.

Bonus — **validation falls out for free**: a kernel that emits a one-step-ahead distribution is
scorable per step against history (CRPS on the sample/quantiles), turning one path realization into
`T` resolved one-step predictions — the dense calibration signal the cutoff backtest otherwise lacks,
and the concrete (known-)black-swan-frequency check.

## Validating the LLM base measure (plan)

We are scoring a **distribution** (a base measure), not a point forecast, so metrics are
distributional and every comparison is **relative** — LLM vs the structured state-space `Q` vs the
crowd — on the same anchor + markets: Brier / log-loss + reliability diagram for binary markets; CRPS

- PIT/rank histograms for continuous series; explicit **tail coverage** (we already see upside
  under-coverage, so extremes will realize _more_ often than the model implies).

**The leakage problem (why a naive cutoff backtest fails).** An LLM's knowledge cutoff is fuzzy and
leaky — models routinely know post-cutoff events, so "anchor at its cutoff, predict to today" can be
_recall, not forecast_, and looks artificially good. You cannot build a cleanly held-out future for a
model whose training overlaps the test window.

Three tiers:

1. **Now, leakage-free, relative.** Raw-marginal divergence from _current_ crowd prices + ESS, LLM
   vs structured-`Q`. No outcomes, no leakage — measures whether the base measure covers the regions
   the crowd believes in (the raw/reweighted/ESS tables, with a structured-`Q` baseline column).
2. **Cutoff backtest — smoke test only.** (a) leakage probe: dated questions about post-cutoff
   events; if known, discount. (b) plot **relative skill** (vs random-walk / structured baseline, so
   intrinsic predictability cancels) as a function of anchor date; leakage shows up as skill
   approaching the _recall ceiling_ for windows inside training data, and the decay point estimates
   the cutoff. One realization per anchor → coarse cutoff detector, not a quality grade.
3. **Prospective scorecard — the real validation.** Log today's model distribution over dated,
   resolvable markets/series; score with CRPS / log-loss + calibration _as they resolve_. Only
   leakage-free distributional test; same scorecard for structured model + reified hybrid. Slow but
   true.

**Leakage-free backtest testbed: an old, known-dated open model.** A model whose weights came online
on date D cannot have seen anything after D — a **known hard cutoff**, no leakage guessing. Older =
longer resolved-future window = enough resolved instances for real calibration/CRPS/PIT (not the
single realization the GLM-cutoff backtest is stuck with). Use it to validate the harness + scoring
and characterize the calibration shape (esp. tail under-coverage); the production quality number for
GLM still comes from tier 3. Tradeoff: old/small models are weaker and worse at structured JSON —
fine for _methodology_ validation. The requirement is a **known training date + reliable structured
output**, not capability. **Ground truth is already in hand**: `fetch_real_history.py` (FRED/Yahoo)
takes date ranges, so for any past anchor we fetch the realized `[cutoff → today]` series and score
against it. Candidates by documented cutoff (older = longer backtest): Llama 2 (~Sep 2022), Mistral
7B (~2023), Llama 3.1 (~Dec 2023), Gemma 2 (~mid 2024).

**Infra (2026-06).** Cluster GPU Ollama (wyrm2, 2×RTX 5090, `ollama.allegedly.works`) is the natural
host but is **currently down** — bring it up if this becomes the path. Alternatively any small model
runnable locally that emits reliable structured JSON works. New harness pieces needed: an old-model
endpoint, anchoring the history fetch at a past date, and the scoring (CRPS/PIT/coverage); the
rollout harness itself is done.
