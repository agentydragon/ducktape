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

| file                     | what                                                                         |
| ------------------------ | ---------------------------------------------------------------------------- |
| `run_spike.py`           | one-shot: ask for N dense monthly scenarios in one call, reweight to markets |
| `run_windowed.py`        | conversational rollout — advance 12 months/turn, real history, OpenAI events |
| `kernel.py`              | per-step transition kernel, ENUMERATION proposal: N weighted joint options   |
| `kernel_percentile.py`   | per-step kernel, PERCENTILE proposal: elicited quantile function (p1..p99)   |
| `backtest.py`            | teacher-forced one-step calibration backtest (PIT scoring)                   |
| `backtest_percentile.py` | same backtest for the percentile kernel (PIT by inverse quantile function)   |
| `backtest_rolling.py`    | rolling-origin, multi-horizon free-running calibration                       |
| `backtest_statespace.py` | structured state-space baseline on the same window (analytic PIT, no API)    |
| `openai_history.json`    | OpenAI's **public** funding-round/tender history (private marks excluded)    |
| `plot_*.py`              | rollout fan plot + calibration histograms / horizon plots → `results/*.png`  |

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
  > unified process (see the enumeration-reframing work below).
  > Caveat: a quantile is per-series scalar (awkward for the joint+events step); the N-sample handles the
  > joint natively. Hybrid for the forward path: percentile marginals for the joint + the N-sample for the
  > cross-series coupling and discrete events.

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
| calibrated target                             |          0.50 |         20% |                                                     |

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

### 8. Leakage-free probe (llama3.1:8b) — glm-4.5's calibration is leakage-suspect

glm-4.5's June-2024 cutoff is fuzzy and leaky, so "anchor at cutoff, predict to now" can be recall, not
forecast. `llama3.1:8b` has a **documented Dec-2023 training cutoff** and open weights, so 2024-01
onward is a **hard** leakage-free window. Running the percentile kernel on it locally via ollama
(`backtest_llama.py`, anchor 2023-12, 18 steps 2024-01..2025-06, 79 PITs):

| metric (pooled)               | llama3.1:8b (hard cutoff) | glm-4.5 percentile (leaky) | calibrated |
| ----------------------------- | ------------------------: | -------------------------: | ---------: |
| mean PIT                      | **0.69** (under-predicts) |                       0.48 |       0.50 |
| tail-escape                   |       **56%** (very thin) |                        10% |        20% |
| realized beyond stated p1/p99 |                   **32%** |                         2% |         2% |
| JSD-to-uniform                |                0.166 bits |                  0.05 bits |          0 |

The hard-cutoff model is **badly overconfident** (its stated 98% interval holds only 68% of the time)
and **strongly under-predicts** the 2024–25 bull run. Two confounded readings, not separable from one
model: (a) **leakage flattered glm-4.5** — its well-centred percentiles (mean 0.48, _no_ bull-run
under-prediction) look like partial recall once a genuinely-blind model under-shoots the climb as honest
forecasting should; (b) **capability** — an 8B is just worse at stating honest wide quantiles. The
cleanest tell is the **under-prediction direction**: llama (0.69) and glm-4.5 _enumeration_ (0.62) both
under-shoot the bull run while glm-4.5 _percentile_ (0.48) conspicuously doesn't. **Treat the glm-4.5
calibration numbers as leakage-suspect**; a rigorous leakage-free verdict needs a _stronger_
known-cutoff model (Llama-3.1-70B / a frontier open model) on a GPU. **Infra note:** CPU inference is
~5 tok/s and ollama's OpenAI-compat `json_object` 500s on the heavy schema, so on a CPU box only the
light percentile probe is feasible — the sharp-joint kernel and forward reify need a GPU.
(`backtest_llama.py`; `results/backtest_llama3-1-8b.json`.)

## Calibration backtest — the rigorous validation

Anchor `glm-4.5` at its **leakage-probed June-2024 cutoff** (it doesn't know the 2024–26 OpenAI rounds
or end-2025 BTC), so 2024-06 → now is genuinely out-of-sample. Score the realized next value as a PIT
within the kernel's options; ground truth from the date-ranged fetcher. ~0 pp weekly quota.

> **Window note — the Oct-2025 CPI gap (20 months, not 21).** The backtest scores the strict
> intersection of months present in all five series. BLS published **no October-2025 CPI** (the
> government shutdown disrupted collection), so both FRED CPI series — `inflation` (CPIAUCSL) and
> `rent:sf_ca` (CUUR0000SEHA) — have **no `2025-10` row at all** (confirmed source-side on
> re-download; the Yahoo and Case-Shiller series do have it). The intersection therefore drops
> `2025-10` for every series, leaving 20 scored months (`2024-07`…`2026-03`). Side effect: the
> `2025-09 → 2025-11` move is treated as one step, so that step's return is mildly inflated for the
> non-CPI series. Left as-is (one month barely moves n_eff≈5–7); not forward-filled (would invent a
> CPI value BLS never published).

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

**Structured-model baseline (`backtest_statespace.py` + `plot_compare.py`).** Is the LLM's
miscalibration actually _bad_? Score augur's structured `Q` on the **exact same window**: the
state-space model is a joint monthly log-return Gaussian, so its one-step marginal for series _s_ is
`N(μ_s, σ_s²)` and the analytic PIT is `Φ((log(realized/last) − μ_s)/σ_s)` — pure local compute, no
API. To stay apples-to-apples we fit `μ_s, σ_s` on the **identical trailing 24-month window** the LLM
kernel saw each step (`//augur/x/pm_reifier:backtest_statespace`, runs on RBE). Both through the same
scorecard:

| metric (pooled, n=100)        |         LLM enumeration (glm-4.5) |                  LLM percentile (glm-4.5) | state-space (log-ret Gaussian) |
| ----------------------------- | --------------------------------: | ----------------------------------------: | -----------------------------: |
| mean PIT [block-boot 95% CI]  |       **0.62 [0.50, 0.75]** (out) |            **0.48 [0.42, 0.56]** (**in**) |    **0.40 [0.32, 0.48]** (out) |
| tail-escape [CI]              | **0.31 [0.23, 0.42]** (out, thin) |         **0.10 [0.07, 0.14]** (out, wide) | **0.23 [0.12, 0.36]** (**in**) |
| realized beyond stated p1/p99 |                                 — |                  **2%** (honest 98% int.) |                              — |
| JSD-to-uniform                |                        0.052 bits |                                0.053 bits |                     0.056 bits |
| verdict                       |          MISCALIBRATED (two axes) | MISCALIBRATED (**dispersion only, mild**) |      MISCALIBRATED (bias only) |

First-pass read (state-space vs LLM **enumeration**): both biased in **opposite directions** — the LLM
under-predicted the 2024–26 bull run (PIT piled high); the trailing-window state-space over-predicted
(PIT piled low, extrapolating decelerating momentum). The decisive split was **dispersion**: enumeration
was significantly **thin-tailed** (overconfident), the state-space tails were calibrated.

**The percentile kernel changes the verdict.** Asking the LLM for its quantile function (p1..p99)
instead of N enumerated options — same model, same window — **removes both enumeration failures at
once**: the location bias vanishes (mean PIT 0.48, CI now straddles 0.5) and the thin tails are gone.
The explicit extreme commitment is **honest** — realized fell outside the stated p1/p99 exactly 2% of
the time (the calibrated rate). It over-corrects only mildly: tail-escape drops to 0.10 (the p10–p90
band is now a touch too _wide_), the opposite, gentler failure. So the elicitation format, not the
model, was the binding constraint — naming the percentiles (and being forced to commit to a p1/p99)
beats asking for "diverse options," which collapses toward the mode and truncates the tail. This is the
representation `Q` should use going forward (with the README's hybrid for the joint cross-section, since
a quantile is per-series scalar). (Same n≈20-effective caveat; the bias removal and the tail-escape sign
flip are the robust signals.)

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
capability. Ground truth is already in hand (`//augur/data:fetch_real_history` takes date ranges). Candidates:
Llama 2 (~Sep 2022), Mistral 7B (~2023), Llama 3.1 (~Dec 2023), Gemma 2 (~mid 2024).

Next, in priority order:

1. **Force coverage** — was the binding blocker; the **percentile kernel largely solves it** (bias gone,
   thin tails gone, honest p1/p99 — see the comparison above). Remaining: trim the mild p10–p90
   over-width (a sharper percentile grid / temperature), and confirm the all-zero upside markets come
   alive in the forward reify path under percentile elicitation.
2. **More anchors (rolling origin)** to tighten the borderline CIs, now across all three proposals. The
   structured-`Q` baseline on the same scorecard is **done** (above); the percentile-vs-enumeration
   comparison is **done** — percentile wins. Next is whether the percentile kernel's edge holds across
   more anchors and a longer-cutoff model.
3. **Wire the percentile kernel into the forward reify path** (the backtest uses it; the reify path still
   uses the windowed enumeration rollout), with the **hybrid** for the joint cross-section (percentile
   marginals + N-sample coupling) and the **compact handoff** (carry only last levels + a regime note).

**Infra.** The known-dated-model backtest wants a 7–8B model with reliable JSON; the cluster GPU Ollama
(wyrm2) is the natural host but is **currently down** (CPU-local is workable but slow). z.ai `glm-4.5`
(June-2024 cutoff) is what the current backtest uses — convenient and leakage-clean, but a short window.
