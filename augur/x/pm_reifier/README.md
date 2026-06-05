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

| file                                    | what                                                                                |
| --------------------------------------- | ----------------------------------------------------------------------------------- |
| `run_spike.py`                          | one-shot: ask for N dense monthly scenarios in one call, reweight to markets        |
| `run_windowed.py`                       | conversational rollout — advance 12 months/turn, real history, OpenAI events        |
| `run_reify_joint.py`                    | **forward reify** via the sharp joint kernel: roll a cloud, reweight, report ESS    |
| `kernel.py`                             | per-step kernel, ENUMERATION proposal: N weighted joint options (`pit`/`draw`)      |
| `kernel_percentile.py`                  | per-step kernel, PERCENTILE proposal: elicited quantile function (p1..p99)          |
| `kernel_iid.py`                         | enumeration reframed as an explicit i.i.d. sample (±thinking) — finding 6           |
| `kernel_joint.py`                       | percentiles + N joint samples in one emission; `sharp` = anti-grid (the deployable) |
| `backtest.py`                           | teacher-forced one-step calibration backtest (PIT scoring) + shared helpers         |
| `backtest_percentile.py`                | same backtest for the percentile kernel (PIT by inverse quantile function)          |
| `backtest_iid.py` / `backtest_joint.py` | backtests for the iid / joint(+sharp) kernels                                       |
| `backtest_rolling.py`                   | rolling-origin, multi-horizon free-running calibration                              |
| `backtest_statespace.py`                | structured state-space baseline on the same window (analytic PIT, no API)           |
| `backtest_llama.py`                     | leakage-free percentile probe on a local known-cutoff model via ollama — finding 8  |
| `openai_history.json`                   | OpenAI's **public** funding-round/tender history (private marks excluded)           |
| `plot_*.py`                             | rollout fan plot + small-multiples calibration scorecard → `results/*.png`          |

Macro history is fetched by **`//augur/data:fetch_real_history`** (Yahoo for sp500/BTC, FRED for
CPI/home/rent; date-ranged) — promoted out of `x/` into the curated `augur/data` directory.
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
within-message prefix caching makes cheap (see `docs/z_ai_api.md`).

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

Seeding worlds with **real** recent macro tails (`//augur/data:fetch_real_history`) anchors them on actual levels
and momentum; extending the tail 1 yr → 5 yr (so the model sees BTC's full 2021–26 boom/crash cycle)
**modestly widens coverage** (e.g. `cpi>108` 0.73→0.94, `sfhome>110` 0.13→0.38, S&P/BTC upside off
exactly-zero) but doesn't fix the extreme tails. **OpenAI is modelled as augur's PE issuer is** —
discrete events (`primary_round`/`secondary_tender`/`ipo`/`collapse` with a post-money valuation), not
a smooth path — fed its **public** funding history. That's the right shape: the tender-by / ipo-by /
valuation markets reweight cleanly.

### 5. Ways the LLM can express the per-step distribution

- **Sample (N weighted options)** — `kernel.py`. Non-parametric: multimodal / skew / fat tails + discrete
  events native; gives draws directly. Cost: small N under-resolves the deep tail; needs the weights.
  **Empirically the worst-calibrated** (thin tails + location bias — see the comparison below): "list
  diverse options" collapses toward the mode and truncates the tail.
- **Parametric (Gaussian / Student-t / mixture)** — cheap to over-draw but imposes a shape (Gaussian's
  thin symmetric tails are wrong for finance) and reduces the LLM to coefficient-picking.
- **Percentiles / quantile function** — `kernel_percentile.py`. Elicit a fixed grid (p1..p99); PIT is
  the inverse quantile function at the realized value, and the stated p1/p99 make tail coverage directly
  measurable. **Best _marginal_ calibration of the three** (bias removed, honest p1/p99 — see below): naming
  the percentiles forces an explicit tail commitment the enumeration never makes.
  > **⚠️ But the percentile approach is NOT deployable as `Q`, for two independent reasons:**
  >
  > 1. **It is marginal-only.** A percentile/quantile is a scalar concept; there is no canonical
  >    multivariate percentile (no canonical order on ℝⁿ). `kernel_percentile` elicits five _independent_
  >    univariate marginal CDFs and throws away the cross-series dependence — so it cannot produce a joint
  >    draw (a shared `u` across series → comonotone; independent `u` → zero correlation; both wrong). The
  >    reify path needs the joint (risk-off hits equities+crypto together), so a product-of-marginals is
  >    unusable as the base measure.
  > 2. **Test-what-you-deploy.** Even patched with a copula for sampling, the backtest would then score a
  >    _different_ generative process than the one we sample from — the calibration evidence wouldn't
  >    transfer. Whatever defines the distribution must be **one process**, scored and sampled identically.
  >
  > So `kernel_percentile` is a **diagnostic** — it isolates that the enumeration kernel's failures are a
  > marginal-calibration problem, and proves the model _can_ state honest marginal tails when asked
  > directly. It is not a deployable kernel. The deployable kernel must emit a **joint** in a single
  > unified process — which the sharp joint kernel (finding 6) turns out to do, no copula/AR hybrid needed.

### 6. Freehand sampling grids the quantile function — but that's a prompt artifact, fixable

Why is freehand sampling (`kernel.py`) biased + thin-tailed while stating percentiles
(`kernel_percentile.py`) is calibrated? A ladder of experiments on glm-4.5, same 2024-06 window, all
scoring the realized value's PIT against the **sample cloud** (`pit_samp`):

| variant                                       |      mean PIT | tail-escape | what the model did                                  |
| --------------------------------------------- | ------------: | ----------: | --------------------------------------------------- |
| enumeration (`kernel.py`)                     |          0.62 |         31% | grid over a too-narrow self-anchored range → thin   |
| iid reframe (`kernel_iid.py`, think off/on)   | 0.585 / 0.598 |   31% / 38% | same gridding; framing + reasoning irrelevant       |
| joint, commit-then-sample (`kernel_joint.py`) |          0.43 |          4% | grid over the _correct_ range → over-dispersed      |
| **joint, sharp anti-grid prompt**             |      **0.40** |     **13%** | **density-weighted draw that tracks the marginals** |
| _(percentile, read directly — marginal only)_ |        _0.48_ |       _10%_ | _calibrated, but not deployable_                    |
| state-space baseline (structured `Q`)         |          0.40 |         23% | log-ret Gaussian, calibrated tails, over-predicts   |
| calibrated target                             |          0.50 |         20% |                                                     |

The **state-space baseline** (`backtest_statespace.py`, PR #1909) scores augur's structured `Q` — a joint
monthly log-return Gaussian, analytic one-step PIT — on the identical window. Through the
moving-block-bootstrap scorecard it is MISCALIBRATED on **one** axis (over-predicts, bias only; its
Gaussian tails are calibrated), where the **enumeration** kernel fails on **two** (under-predicts +
thin), so on this window the mechanistic model beat the original LLM kernel — but the **sharp joint
kernel** matches it on dispersion and recentres the bias. CIs + per-model PIT histograms for all six in
`results/compare.png` / `results/compare_stats.json`.

**The mechanism.** Asked for N "samples", the model first returns them **evenly spaced from its stated p1
to p99** — a **quantile grid**, not a density-weighted i.i.d. draw. It reads "give me N samples" as "give
me representative cases" (be helpfully comprehensive), so it ladders across its range instead of drawing
randomly. Self-anchored that grid spans a too-_narrow_ range (thin tails); after committing to wide
percentiles it spans the _correct_ range but is ~uniform-over-range (over-dispersed, tail-escape 4%).

**But it's promptable.** Telling the model explicitly — _"this is a random i.i.d. draw, NOT a grid; do
not space evenly; for EACH series ~half the values land in [p25,p75], ~1 in 10 beyond p10/p90, ~1 in 50
beyond p1/p99; near-duplicates expected; don't sort"_ (the `sharp` mode of `kernel_joint.py`) — flips the
samples to genuinely density-weighted (per-window: ~54–62% inside [p25,p75], ~20–25% in the tails). The
sample cloud then **tracks the model's stated marginals** (sample tail-escape 13% ≈ stated 11%; spread
ratio 1.5→1.21). Note the frequency targets must be stated **per series** — a multivariate percentile is
undefined — and the cross-series dependence is left to the model ("capture realistic relationships"), not
prescribed.

**Where this lands.** The **sharp joint kernel is a viable deployable `Q`**: a genuine joint (dependence
within each cross-section), density-weighted, one unified scored-and-sampled process — no autoregressive
machinery needed. Its residual miscalibration (mildly over-wide, slight over-predict bias) now lives in
the model's _stated_ distribution, not the sampling, and over-coverage is the safe direction for the
reweight (the one-way valve). The model's stated quantiles are good; once told concretely how to draw, it
samples from them faithfully. (`kernel_iid.py`, `kernel_joint.py`, `backtest_iid.py`, `backtest_joint.py`;
results in `results/backtest_iid.json`, `results/backtest_joint.json`, `results/backtest_joint_sharp.json`.)

### 7. The sharp joint kernel runs end-to-end in the forward reify path

`run_reify_joint.py` rolls a cloud of forward trajectories step-by-step through the sharp joint kernel
(seeded from the same real-history pipeline as the backtest, rolling N_HIST window), then max-ent
reweights the cloud to illustrative markets. **It works: 16/20 full 12-month trajectories, ESS 9.9
(62%)** — the reweight is real, not fiction. The headline win vs the old enumeration runs: the **BTC
upside tail is now covered** (31% of paths cross +150% → that tail market reweights cleanly 0.31→0.16),
where enumeration put zero mass there ("smarter model = worse base measure"). The honest tails of the
sharp kernel carry into the forward cloud.

Two caveats it surfaced: (a) with only 16 compounded 12-month paths the cloud is **coarse** — nearby
markets go collinear (sp500>+8%, sp500>+20%, BTC>+50% all share raw 0.75: nothing lands _between_ +8%
and +20%), so the reweight can't separate them; more/finer rollouts are the fix, not a kernel change.
(b) Illustrative thresholds must be chosen non-degenerately — inflation>+3%/yr and rent>+4%/yr are
near-certain (raw 1.0), so those markets can't move. (`run_reify_joint.py`; result in
`results/reify_joint.json`.)

### 8. Leakage-free probes (local known-cutoff models) — capability, not leakage, dominates; can't yet isolate leakage

glm-4.5's June-2024 cutoff is fuzzy and leaky, so "anchor at cutoff, predict to now" can be recall.
Open models with **documented training cutoffs** give a HARD leakage-free window. Running the percentile
kernel locally via ollama (`backtest_llama.py`, schema-constrained output so even a weak model emits the
right structure):

| model (cutoff)               | window           | mean PIT | tail-escape | realized beyond p1/p99 |  JSD |
| ---------------------------- | ---------------- | -------: | ----------: | ---------------------: | ---: |
| glm-4.5 (June-2024, leaky)   | 2024-07..2026-03 |     0.48 |         10% |                     2% | 0.05 |
| llama3.1:8b (Dec-2023, hard) | 2024-01..2025-06 |     0.69 |         56% |                    32% | 0.17 |
| llama2:7b (Sep-2022, hard)   | 2022-10..2024-03 |     0.40 |     **70%** |                **52%** | 0.22 |

Both hard-cutoff open models are **catastrophically overconfident** — llama2's stated 98% interval holds
only **48%** of the time (it emits near-degenerate, almost-linear quantiles with a ±6% monthly BTC band).
Crucially the overconfidence **tracks model capability** (llama2 worse than llama3.1 worse than glm-4.5),
**not** cutoff recency — so the thin tails are a **capability artifact, not leakage**. That walks back the
tempting "leakage flattered glm-4.5" read: these small models are **too weak to isolate leakage** — they
fail on capability (can't state honest wide quantiles) before leakage even enters. The mean-PIT/bias axis
is the only place leakage might show, and it's noisy and era-dependent (llama3.1 under-predicts the
2024–25 climb at 0.69; llama2 over-predicts the 2022-23 chop at 0.40), so it's not a clean signal either.

**Verdict:** a CPU-class open model **cannot** settle whether glm-4.5's good calibration is skill or
leakage — it's confounded by the capability gap, and these models are simply badly calibrated regardless.
A real leakage-free test needs a **strong** known-cutoff model (≥70B / a frontier open model) on a GPU.
**Infra note:** CPU inference is ~5 tok/s and ollama's OpenAI-compat `json_object` 500s on the heavy
schema, so on a CPU box only the light percentile probe is feasible (the sharp-joint kernel and forward
reify need a GPU); pass a JSON **schema** as ollama's `format` to force structure on weak models.
(`backtest_llama.py`; `results/backtest_llama3-1-8b.json`, `results/backtest_llama2-7b.json`.)

## Methodology & rigor

We score a **distribution**, so metrics are distributional (PIT / rank histograms, tail-escape, JSD) and
comparisons are **relative** (LLM vs structured `Q` vs crowd). Calibration — _are the stated
uncertainties honest_, the rank histogram, no baseline, independent of how (un)predictable the era was —
is kept separate from **skill** (CRPS vs a random-walk / structured baseline, which cancels era difficulty).

**Window & statistics.** The teacher-forced backtests anchor `glm-4.5` at its leakage-probed June-2024
cutoff and score the realized next value as a PIT over 20 months (`2024-07`…`2026-03`, the strict
5-series intersection; ground truth from the date-ranged fetcher; ~0 pp weekly quota). The monthly PITs
are strongly autocorrelated (ρ₁≈0.6 → **n_eff≈5–7**), so i.i.d. KS/χ² p-values (~1e-5) are
anti-conservative; every "MISCALIBRATED" verdict rests on the serial-dependence-robust **moving-block
bootstrap** 95% CI, not the p-values. Per-series JSDs sit in the n=20 noise floor — only the pooled
bootstrap and histogram shape are trustworthy. Small-multiples scorecard: `results/compare.png`.

> **Oct-2025 CPI gap (20 months, not 21).** BLS published no October-2025 CPI (government shutdown), so
> the two FRED CPI series have **no `2025-10` row** while Yahoo/Case-Shiller do; the strict intersection
> drops it for all series. The `2025-09→2025-11` step is then mildly inflated for the non-CPI series.
> Left as-is (one month barely moves n_eff); documented in `augur/data/SOURCES.md`.

**Does miscalibration compound with horizon?** Rolling-origin, free-running from 4 origins
(`backtest_rolling.py`): **no.** Under-prediction is horizon-independent (mean PIT ~0.70 at every h),
while the dispersion deficit is **front-loaded and self-corrects** (tail-escape 60% at h=1 → ~15% by h=8
as the free-running cone widens). So the enumeration `Q` needed a de-bias nudge at all horizons + more
_initial_ spread — exactly what the sharp joint kernel (finding 6) supplies. (M=8 ensemble, n≈20/horizon,
correlated origins → the flat ~0.70 mean is the robust signal, the tail trend suggestive.)

## Status & next steps

**Done.** Percentile diagnostic → the **sharp joint kernel** (finding 6) is a calibrated,
density-weighted, single-unified-process joint `Q` — validated in the one-step backtest, the
structured-`Q` comparison (it beats both the enumeration kernel and the state-space baseline on the same
scorecard), and end-to-end in the **forward reify** (finding 7: ESS 62%, BTC upside tail covered).
Leakage-free probes (finding 8) show CPU-class open models can't isolate leakage — capability dominates.

**Open**, in priority order:

1. **Strong known-cutoff model on a GPU** — the only way to settle whether glm-4.5's calibration is skill
   or leakage (finding 8). Needs ≥70B / a frontier open model; the cluster GPU Ollama (wyrm2) is the
   natural host but was last seen **down**.
2. **Finer reify cloud** (finding 7) — more rollouts + non-degenerate market thresholds to
   de-collinearize the markets and resolve the cloud past its current 16-path coarseness.
3. **Trim the residual** — the sharp joint is mildly over-wide + slightly over-predicts; try a sharper
   percentile grid / temperature, plus the **compact handoff** (carry only last levels + a regime note)
   to cut the stateful input tokens on long rollouts.
