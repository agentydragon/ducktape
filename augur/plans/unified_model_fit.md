# Unified model fit (history + prediction-market calibration)

Status: **draft / design exploration** (started 2026-06-04; revised to fit by Monte-Carlo
scoring rather than requiring differentiable marginals). Not yet scheduled in
`augur/plans/roadmap.md`. This is the "what could the training story be" doc; it does not
describe shipped behavior.

Companion reading:

- `augur/plans/whole_model_calibration.md` — how calibration scores the joint model today.
- `augur/plans/prediction_market_calibration.md` — the M2.x PE-channel fit work (dilution).
- `augur/fit/model.py` — the `Fittable` / `Scorable` protocols this doc builds on.
- `augur/TODO.md` — the public backlog, including the items this would subsume.

## Motivation

Augur's training story is a collection of disconnected stages rather than one workflow. The
abstractions are good; the pipeline around them is not. Concretely, a deployed model today is a
**mix of**:

- a **fitted** macro artifact (`state_space` block-shrunk, or `vecm`), produced by
  `augur/fit/main.py`;
- **hand-pasted fitted scalars** — the PE dilution params from `augur/fit/derive_dilution_prior.py`
  (OLS, M2.2-A) or `augur/fit/bayes_dilution.py` (NUTS, M2.2-D) are emitted as a paste-ready YAML
  block a human copies into the deployment config;
- **hand-set structural knobs** — most of `private_equity_risk.py` (hazards, tender cadence,
  scale-reversion shape) is set by judgment;
- **hand-set runtime conditioning** — the current spot levels (BTC/ETH/Zillow/CPI/SPY) the macro
  model conditions on are typed into the config by hand.

And the three "data" roles never connect:

- **training data** (`augur/data/` public series; a private `observations.jsonl` for PE) is used
  to fit;
- **eval** (`augur/fit/metrics_report.py`) scores predictive log-density / CRPS, but largely as
  **resubstitution** — on the same data the model was fit on, with no clean held-out split — and
  only for the models wired into its active list (VECM, Independent), _not_ the deployed
  `state_space` macro;
- **prediction-market calibration** (`augur/calibration/`) scores the assembled model against
  live market prices entirely **post hoc**. Nothing in `augur/fit/` ever optimizes it.

So calibration — arguably the truest external signal we have about whether the joint model's
marginals are sane — is a scoreboard, never an objective.

## The objective

Fit parameters `θ` of the exogenous model (macro and eventually PE) to

```
L(θ) = − Σ_{t, h} logdensity_θ(history)   +   λ · Σ_{m ∈ markets} D( p̂_θ(m) ‖ price(m) )
            └── historical fit ──┘                  └──── calibration fit ────┘
```

- the **historical** term: the model's predictive score on held-out / rolling-origin history
  (predictive log-density / CRPS — what `augur/fit/scoring.py` already computes, now used as a
  loss);
- the **calibration** term: for each market `m`, the model's implied probability `p̂_θ(m)` vs the
  market `price(m)`, under a proper divergence `D` (KL / Brier / log-loss);
- `λ`: a tunable knob trading off "match history" against "match the crowd."

Crucially, **neither term needs to be a smooth/differentiable function of `θ`.** That choice is
what lets us keep any model (including the numpy `state_space` macro and the discrete-event PE
rollout) and use markets of any kind without special-casing.

## How we fit it: Monte-Carlo scoring (default), pathwise gradients (optional)

The estimator is decoupled from the objective. The default — and lowest-headache — estimator is
**Monte-Carlo scoring with a gradient estimate that requires neither the rollout nor the reward to
be smooth**:

1. Run `N ≈ 10k` rollouts of the exogenous model at the current `θ`.
2. Compute the scalar `L(θ)` from those rollouts: the historical predictive term plus
   `λ ·` the calibration term (each market's `p̂_θ(m)` is just the fraction of rollouts satisfying
   it, compared to the price).
3. Step `θ` to improve `L` with either:
   - **score-function / REINFORCE**: `∇_θ E_τ[R(τ)] = E_τ[R(τ) · ∇_θ log p_θ(τ)]`. Needs only the
     per-rollout trajectory log-prob's gradient in `θ` (cheap for a Gaussian state-space — a sum of
     per-step log-probs); the reward `R` itself stays arbitrary and non-differentiable; or
   - **gradient-free / black-box**: CMA-ES, SPSA, or finite differences directly on `θ`. The
     combined macro/PE parameter vector is modest (factor drifts, vols, a covariance, a handful of
     hazard/dilution scalars), so a derivative-free optimizer is entirely viable and needs **zero**
     autodiff anywhere.

Why this is the preferred default:

- **works with any sampler as-is** — the numpy `state_space` macro and the discrete-event PE
  rollout included; no JAX port, no continuous relaxations (Gumbel-softmax), no smoothness proofs;
- **the reward can be anything** — calibration KL, sanity-band penalties, tail targets — none of it
  has to be differentiable;
- **the estimator does not dictate the model** — `state_space` stays a first-class, deployable
  macro; `vecm` remains available; future models drop in without re-deriving gradients.

Variance and cost are the real engineering, and they are manageable:

- **Common random numbers (CRN)** — fix the rollout seed set across `θ` evaluations so score
  _differences_ reflect `θ`, not Monte-Carlo noise. This is the single biggest lever; it turns
  finite-difference / SPSA into a low-variance estimator and pairs naturally with augur's existing
  fixed rollout-seed convention.
- **Baselines / control variates** for REINFORCE (subtract a per-batch mean reward; the historical
  term can serve as a baseline).
- `N ≈ 10k` is cheap — the sim already tensorizes the rollout axis — so the budget is
  `#optimizer-steps × N × #rolling-origins`, not memory. Far-future thin markets are the noisiest
  contributors; weight them down (see Obstacles).

**Optional low-variance shortcut.** Where a model exposes a closed-form predictive marginal — the
`Scorable.predictive(...) → numpyro.distributions.Distribution` interface that already exists in
`augur/fit/model.py` and returns closed-form Gaussians for `vecm` / `independent` today — the
macro/level/inflation market terms have exact analytic probabilities `Φ((log K − μ_h)/σ_h)` and can
use pathwise gradients instead of MC for lower variance. This is a nice-to-have for the Gaussian
macro factors, never a requirement, and is unavailable for the discrete PE channel.

## The markets are just reward terms

Under MC scoring every catalog mapping kind (`augur/calibration/`) is simply "fraction of rollouts
that satisfy it" vs the price — no per-kind differentiability story:

| Market kind                                          | Model probability `p̂_θ(m)`                      |
| ---------------------------------------------------- | ----------------------------------------------- |
| `level_at_date` / `level_by_date` (S&P/BTC/ETH/home) | fraction of rollouts with level ≥ K at the date |
| `inflation_yoy` bucket / multinomial family          | fraction of rollouts in each bucket             |
| `ipo_by_date` / `pre_ipo_failure` / collapse         | fraction of rollouts with the event by the date |
| PE `valuation_by_date` / mark thresholds             | fraction of rollouts with mark ≥ K              |

The calibration loss is `Σ_m D(p̂_θ(m) ‖ price(m))`. (With the optional closed-form path, the
macro/level rows may instead use the exact Gaussian CDF for lower variance — but you do not have
to, and the discrete rows always go through rollouts.)

## Staged plan

### Stage 0 — eval discipline first (cheap, worth doing regardless)

- Add a real **held-out tail split** plus **some rolling-origin** to `augur/fit/metrics_report.py`,
  scored at multiple horizons. Every-month origins are ideal but `#origins × fit_cost` adds up, so
  start with a handful of origins and expand as the budget allows.
- Put the **deployed** `state_space` macro into the active list so we benchmark what we ship (today
  it scores VECM + Independent only).
- Independently useful: you cannot tell whether the joint fit helped without an out-of-sample
  yardstick.

### Stage 1 — macro joint fit by MC scoring

- Fit the **deployed** macro (`state_space` is fine; `vecm` also fine — the estimator is
  model-agnostic) to `historical + λ·calibration` over the macro/level/crypto/inflation markets,
  using CRN + SPSA/CMA-ES (or REINFORCE if we want gradients).
- Sweep `λ`; report both terms on the held-out split and the calibration page.
- Deliverable: one fit command produces the macro artifact (drifts / vols / covariance **and** the
  conditioning the config now hand-sets), calibration-aware — replacing the fit-then-hand-tune loop.
  `state_space` stays the option.

### Stage 2 — fold in PE + event markets

- Because the estimator is Monte-Carlo, PE and event markets are just **more reward terms** over the
  existing numpy PE rollout — no hazard-CDF derivation, no JAX port, no relaxations. This is exactly
  where MC scoring pays off versus a closed-form/differentiable approach (which made PE "the hard
  part"). The cost is variance / throughput (more rollouts), not new math.
- This retires the hand-pasted dilution path: the dilution / hazard scalars become part of the
  jointly-fit `θ`, with provenance, instead of OLS/NUTS output copied into the config by hand.

## What this fixes (coherence wins)

- **deployed = fitted** — the macro artifact (drifts, vols, _and_ conditioning) comes out of one fit
  command, `state_space` included; no hand-pasting.
- **calibration becomes an objective with a knob** (`λ`), not a post-hoc scoreboard.
- **one dilution fit** — Stage 2 folds dilution/hazards into `θ`, retiring the blocked M2.2-A OLS
  path (`derive_dilution_prior.py`, ~8000× 10y mark) and the hand-copy step.
- **honest eval** — Stage 0 replaces resubstitution with a held-out + rolling-origin split that
  includes the deployed model.
- **provenance** — a fit run ties artifact → evidence → params, replacing "paste this YAML block."

## Obstacles and open questions

- **Estimator variance.** MC scores/gradients are noisy; CRN + baselines are mandatory, and thin
  far-future markets are the noisiest. This replaces the old "`state_space` is empirical / must be
  ported to JAX" obstacle — with MC scoring that is no longer a concern.
- **Calibration weighting `λ` and market quality.** Markets vary wildly in liquidity and horizon; a
  flat `λ` over-weights thin far-future markets. Likely need per-market weights (volume,
  time-to-resolve) — dovetails with the "aggregate metric / weighting TBD" item in `augur/TODO.md`.
- **Identifiability.** Calibration pulls hardest exactly where history is thin (far-future tails);
  that is the point, but it risks the markets dominating. The held-out historical term and a
  sensible `λ` are the guardrails; watch for the fit chasing a single mispriced market.
- **Stationarity / regime.** History-fit and market-fit can genuinely disagree (the market prices a
  regime the historical window lacks). `λ` is the dial; a feature, not a bug, but monitor it.
- **Resolution lag.** Many catalog markets resolve years out, so the calibration term is scored
  against _current prices_, not outcomes — it teaches the model to agree with the crowd, not to be
  right. Useful (the crowd is a strong prior) but state it plainly; never conflate with backtested
  accuracy.
- **Compute budget.** `#optimizer-steps × N × #rolling-origins`. Budget the rolling-origin count
  and `N`; CRN keeps the per-step variance low enough that modest `N` works.

## Non-goals

- Differentiability of the rollout is **not** required — that is the whole point of the MC-scoring
  default. The closed-form/pathwise path is an optional variance reduction for the Gaussian macro
  terms only.
- Not proposing to fit through the `augur/sim` product rollout (policies, taxes, cashflows) — only
  the **exogenous** model's parameters.
- Not removing the offline NUTS fits; they remain valid ways to produce priors / full posteriors,
  and can seed `θ`.
- Not a near-term commitment; staged so the macro win (Stage 1) can land before PE (Stage 2).
