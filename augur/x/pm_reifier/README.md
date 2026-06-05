# PM-reifier spike: can an LLM be a base measure `Q` for augur?

Throwaway experiment for `augur/plans/interpolating_prediction_markets.md` (design PRs #1903 / #1904).
**Not production code** — lives in `x/`, not Bazel-built.

## The question

The plan reifies prediction-market _marginals_ into a sampleable _joint_ over trajectories by min-KL
projection from a **base measure `Q`**. `Q` can be our structured state-space model or an LLM. This
spike asks: **can an LLM be `Q`** — emit a diverse cloud of trajectories _in augur's native shape_
that, after **one max-ent reweight to the market prices**, match the crowd _without the effective
sample size collapsing_? (Collapsed ESS ⇒ the LLM never proposed worlds in the market-implied region,
so the reweight is fiction.)

## augur's native trajectory shape

The macro model (`augur/model/state_space.py`) emits a **dense monthly level path per factor**, shape
`(rollout, horizon_months+1, factors)`, factors being augur wire-ids: `inflation` (CPI index), `sp500`,
`crypto:BTC`, `home_value:<loc>`, `rent:<loc>`, plus private-equity issuer marks. The LLM is asked for
exactly that — dense monthly paths over those series — plus the OpenAI PE issuer. Market thresholds
are evaluated at specific month indices on the paths.

## Components

| file                    | what                                                                         |
| ----------------------- | ---------------------------------------------------------------------------- |
| `run_spike.py`          | one-shot: ask for N dense monthly scenarios in one call, reweight to markets |
| `run_windowed.py`       | conversational rollout — advance 12 months/turn, real history, OpenAI events |
| `kernel.py`             | the shared per-step transition kernel: N weighted joint next-month options   |
| `backtest.py`           | teacher-forced one-step calibration backtest (PIT scoring)                   |
| `backtest_rolling.py`   | rolling-origin, multi-horizon free-running calibration                       |
| `fetch_real_history.py` | real macro series from Yahoo (sp500/BTC) + FRED (CPI/home/rent), date-ranged |
| `openai_history.json`   | OpenAI's **public** funding-round/tender history (private marks excluded)    |
| `cache_probe.py`        | measures z.ai prompt-cache granularity                                       |
| `plot_*.py`             | rollout fan plot + calibration histograms / horizon plots → `results/*.png`  |

`results/` holds summaries, plots, and `quota_log.jsonl` (per-run token + z.ai-quota burn).
`transcripts/` (every request/response) is **git-ignored** — written locally, not committed.
Operational z.ai behavior (caching, rate-limit tiers, param quirks, quota API) lives in
`docs/z_ai_api.md`. Market prices in the reify runs are **illustrative**, not pulled live.

## What we learned

### 1. Reify works, but raw dense emission doesn't

An LLM _is_ a viable base measure: across runs the markets reweight onto the crowd prices and ESS
holds (the cloud covers the market-implied regions). But asking for whole dense arrays in one shot
fails on **length discipline** — the model won't reliably count to the horizon (only **1–11 of 80**
arrays hit the requested length; lengths scatter 53–60+ _within_ one scenario), which silently drops
~⅔ of scenarios and leaves the reweight **sample-starved** (paired correlated markets go collinear and
can't separate). Paths are locally well-formed (smooth, sane cross-asset co-movement) — the problem is
structural, not local.

### 2. Windowing → the per-step kernel (the emission architecture)

**Windowed** (`run_windowed.py`): advance 12 months per conversational turn and concatenate, so _we_
enforce the horizon length (retry a miscounted window). Length discipline solved — **75/75 full-length
paths**, nothing dropped, only ~2 retries / 79 windows. The cost moves to **input tokens**: the
stateful thread re-reads the growing history (~6.5k → ~270k with a 5-yr context), which z.ai's
within-message prefix caching makes cheap (`cache_probe.py`).

**Per-step kernel** (`kernel.py`, the current architecture): the LLM as an explicit stochastic
transition kernel `p(x_{t+1}|x_{≤t})`. At each step it returns **N weighted _joint_ options** for the
next month — each option a full cross-section over all series (cross-series dependence captured _within
each draw_, not stitched from marginals), plus any sparse OpenAI events. You draw one and append to a
compact history rebuilt into the first message (≤3 messages). Why per-step: enumeration ("list diverse
possibilities") beats implicit sampling (which collapses to the mode); LLM-proposes/you-draw kills the
bad-RNG failure; per-step branching compounds into diverse paths; and it's scorable per step (→ the
backtest below). No explicit latent state — the LLM re-infers regime/vol from history each step, which
_marginalizes_ over latent uncertainty (more honest spread than conditioning on one latent draw).

### 3. The recurring failure: conservatism / under-dispersion (coverage, not calibration)

The min-KL reweight is a **one-way valve** — it re-weights worlds the LLM proposed, it can't invent
new ones (`support(P) ⊆ support(Q)`). So the binding constraint is **coverage**: where the LLM puts
_zero_ mass on a market's region, the indicator column is all-zeros and **no reweight can lift it**
(the concrete form of "ESS collapse = fiction"). Every run hits this on the upside tails. A sharp
corollary: a **"smarter" model is a worse base measure** — paid `glm-4.7` hugs the median (zero of 16
worlds cross BTC>150k or CPI>110), while the sloppier `glm-4.5-flash` covered more. **You want
dispersion/coverage, not calibration** in `Q`; the reweight supplies the calibration. Levers to force
coverage: higher temperature, conditioning on the prices, a `BATCH_SIZE>1` distinct-worlds knob, or
the random-percentile representation below.

### 4. Grounding: real data + OpenAI as events

Seeding worlds with **real** recent macro tails (`fetch_real_history.py`) anchors them on actual levels
and momentum; extending the tail 1 yr → 5 yr (so the model sees BTC's full 2021–26 boom/crash cycle)
**modestly widens coverage** (e.g. `cpi>108` 0.73→0.94, `sfhome>110` 0.13→0.38, S&P/BTC upside off
exactly-zero) but doesn't fix the extreme tails. **OpenAI is modelled as augur's PE issuer is** —
discrete events (`primary_round`/`secondary_tender`/`ipo`/`collapse` with a post-money valuation), not
a smooth path — fed its **public** funding history. That's the right shape: the tender-by / ipo-by /
valuation markets reweight cleanly.

### 5. Ways the LLM can express the per-step distribution

- **Sample (N weighted options)** — default. Non-parametric: multimodal / skew / fat tails + discrete
  events native; gives draws directly. Cost: small N under-resolves the deep tail; needs the weights.
- **Parametric (Gaussian / Student-t / mixture)** — cheap to over-draw but imposes a shape (Gaussian's
  thin symmetric tails are wrong for finance) and reduces the LLM to coefficient-picking.
- **Percentiles / quantile function** — fixed grid (p10..p90) truncates the tail; **random-percentile**
  (draw `u∼U(0,1)`, ask for the `u`-th quantile) is inverse-transform sampling via the LLM, and drawing
  `u` tail-weighted gives **guaranteed tail coverage + built-in importance weights** — the cleanest
  tail lever. Caveat: a quantile is scalar (awkward for the joint+events step); the N-sample handles
  the joint natively. Hybrid: N-sample for the joint + an occasional "give me a p99-tail scenario".

## Calibration backtest — the rigorous validation

Anchor `glm-4.5` at its **leakage-probed June-2024 cutoff** (it doesn't know the 2024–26 OpenAI rounds
or end-2025 BTC), so 2024-06 → now is genuinely out-of-sample. Score the realized next value as a PIT
within the kernel's options; ground truth from the date-ranged fetcher. ~0 pp weekly quota.

**One-step (`backtest.py`, teacher-forced, 100 PITs).** Two failures, both as predicted:

| metric (pooled)              |         value | read                                                |
| ---------------------------- | ------------: | --------------------------------------------------- |
| tail-escape (PIT ≤.1 or ≥.9) |  31% (vs 20%) | **overconfident / thin-tailed** (home 40%, BTC 35%) |
| mean PIT                     | 0.62 (vs .50) | **under-prediction** (under-shot the 2024–26 climb) |

The formal verdict (on `results/backtest.png`): KS/χ² reject PIT uniformity at p~1e-5 _under an i.i.d.
null_, but the monthly PITs are strongly autocorrelated (ρ₁≈0.6 → **n_eff≈5**), so those p-values are
anti-conservative. The serial-dependence-robust **moving-block bootstrap** gives mean PIT 0.62 CI
`[0.50, 0.75]` and tail-escape 0.31 CI `[0.23, 0.42]` — both 95% CIs exclude their calibrated nulls →
**MISCALIBRATED, borderline** (n_eff is tiny). Per-series JSDs are all within the n=20 noise floor;
only the pooled bootstrap + the histogram shape are trustworthy.

**Rolling-origin, multi-horizon (`backtest_rolling.py`, free-running from 4 origins).** Does it compound
with horizon? **No** — the two failures behave differently:

| h           | 1   | 2   | 3   | 4   | 5   | 6   | 7   | 8   |
| ----------- | --- | --- | --- | --- | --- | --- | --- | --- |
| mean PIT    | .79 | .69 | .68 | .69 | .72 | .73 | .71 | .72 |
| tail-escape | 60% | 45% | 40% | 20% | 39% | 45% | 30% | 15% |

- **Under-prediction is horizon-independent** (mean PIT ~0.70 at every horizon; origin-block CI stays
  above 0.5). The conservative bias appears at h=1 and neither compounds nor washes out.
- **The dispersion deficit is front-loaded and self-corrects.** Tail-escape is 60% at h=1 (the one-step
  ensemble is far too tight — chains share the same history) → ~15% by h=8 as the free-running cone
  widens with diverging paths. So `Q` needs a de-biasing nudge at _all_ horizons and more _initial_
  spread, not more long-horizon spread.

(Caveats: M=8 ensemble, n≈20/horizon, correlated origins/series → the flat ~0.70 mean is the robust
signal, the tail trend suggestive.)

## Validation methodology & next steps

We are scoring a **distribution**, so the metrics are distributional (PIT/rank histograms, CRPS,
reliability) and every comparison should be **relative** (LLM vs structured `Q` vs crowd). The clean
verdict is **calibration separated from skill**: the rank histogram answers "are the stated
uncertainties honest" with no baseline and _independent of how (un)predictable the era was_ (dissolving
the "error muddied by period difficulty" worry); skill is CRPS relative to a random-walk / structured
baseline, which cancels period difficulty.

**The leakage problem.** An LLM's cutoff is fuzzy and leaky — "anchor at its cutoff, predict to today"
can be recall, not forecast. So the workhorse testbed is an **old, known-dated open model** (weights
released on date D can't have seen past D — a hard cutoff). Older = longer resolved-future window =
real per-horizon statistics; every anchor after D is a leakage-free **rolling origin**. Tradeoff: old
models are weaker at JSON, but the requirement is _a known date + reliable structured output_, not
capability. Ground truth is already in hand (`fetch_real_history.py` takes date ranges). Candidates:
Llama 2 (~Sep 2022), Mistral 7B (~2023), Llama 3.1 (~Dec 2023), Gemma 2 (~mid 2024).

Next, in priority order:

1. **Force coverage** — the binding blocker (temperature / price-conditioning / random-percentile /
   `BATCH_SIZE>1`), measured by whether the all-zero upside markets come alive.
2. **More anchors (rolling origin) + the structured-`Q` baseline** on the same scorecard — tighten the
   borderline calibration CIs and get the apples-to-apples "is the LLM better or worse than the
   mechanistic model" number.
3. **Wire the kernel into the forward reify path** (the backtest uses it; the reify path still uses the
   windowed rollout), with the **compact handoff** (carry only last levels + a regime note) to cut the
   stateful input tokens.

**Infra.** The known-dated-model backtest wants a 7–8B model with reliable JSON; the cluster GPU Ollama
(wyrm2) is the natural host but is **currently down** (CPU-local is workable but slow). z.ai `glm-4.5`
(June-2024 cutoff) is what the current backtest uses — convenient and leakage-clean, but a short window.
