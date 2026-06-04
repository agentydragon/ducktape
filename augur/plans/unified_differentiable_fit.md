# Unified differentiable fit

Status: **draft / design exploration** (started 2026-06-04). Not yet scheduled in
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
_marginals_ are sane — is a scoreboard, never an objective.

## The idea

Make the model's **predictive marginal** the single source of truth for both halves of a joint
training objective, and fit by gradient descent through the marginals — **not** through the Monte
Carlo rollout.

The key realization is that we do not have to differentiate the (discrete-event, numpy) rollout to
put calibration in the loss. A prediction market scores a **marginal** of the model — `P(S&P > 10k
by 2030)`, `P(CPI YoY ∈ [2%, 3%])`, `P(IPO by month m)`. Those marginals are smooth, often
closed-form functions of the model parameters, even when the rollout that _also_ produces them is
not differentiable.

This is not a new framework bolted on — it is the `Scorable` protocol that already exists
(`augur/fit/model.py`):

```python
class Scorable(Sampler, Protocol):
    def predictive(self, historical: HistoricalSeries, t: int, *, horizon: int = 1) -> dist.Distribution | None: ...
```

`predictive(...)` already returns a `numpyro.distributions.Distribution` over the cumulative
h-step log-return, and for every model in augur today (`vecm`, `independent`) it is a **closed-form
Gaussian** (`augur/fit/scoring.py`). It is already JAX. We just don't train against it.

## The objective

Fit parameters `θ` of a macro (and eventually PE) model to

```
L(θ) = − Σ_{t, h} logdensity( predictive_θ(history, t, h) | realized_return[t→t+h] )    # historical fit
       + λ · Σ_{m ∈ markets} D( p_θ(m) ‖ price(m) )                                       # calibration fit
```

where:

- the **historical** term is exactly what `augur/fit/scoring.py` already computes (joint and
  per-factor predictive log-density, CRPS) — now used as a loss, evaluated on a **held-out /
  rolling-origin** split rather than resubstitution;
- `p_θ(m)` is the model's implied probability for market `m`, derived from the **same predictive
  marginals**;
- `D` is a proper divergence on the `[0,1]` market (KL / Brier / log-loss);
- `λ` is a tunable weight trading off "match history" against "match the crowd."

Optimize with SVI / Adam in NumPyro (the stack the VECM and dilution fits already use). The
posterior-vs-point-estimate choice is orthogonal — start with MAP/SVI point estimates, keep the
door open to full posteriors later (the dilution NUTS work already does this).

### Mapping markets to differentiable marginals

Every catalog mapping kind (`augur/calibration/`) becomes a differentiable function of `θ`:

| Market kind                                                             | Marginal                                                                    | Differentiable?                                                           |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `level_at_date` / `level_by_date` threshold (S&P, BTC, ETH, home value) | `Φ((log K − μ_h(θ)) / σ_h(θ))` — Gaussian predictive CDF                    | **Yes, closed form.** `μ_h, σ_h` are smooth in `θ` via the NumPyro model. |
| `inflation_yoy` bucket                                                  | difference of two Gaussian CDFs over a YoY-return marginal                  | **Yes, closed form.**                                                     |
| multinomial bucket family                                               | softmax-like vector of CDF differences                                      | **Yes, closed form.**                                                     |
| `ipo_by_date` / event-by-date                                           | the **hazard CDF** `_public_market_marginal_cdf` (`private_equity_risk.py`) | **Yes** — smooth in the hazard params, no rollout needed.                 |
| `pre_ipo_failure` / collapse                                            | collapse-hazard CDF                                                         | **Yes** if expressed as a hazard CDF rather than MC-counted.              |
| PE `valuation_by_date` / mark thresholds                                | marginal of the PE mark process                                             | **Hard today** — see Obstacles.                                           |

The headline: **the macro / level / crypto / inflation markets — the bulk of the catalog — are
already Gaussian-marginal and differentiable.** That is the high-value, low-friction win.

## Staged plan

### Stage 0 — eval discipline first (prerequisite, cheap)

Before optimizing anything against held-out / calibration signal, make the eval honest:

- Add a real **train / held-out split** (and keep rolling-origin) to `augur/fit/metrics_report.py`,
  and put the **deployed** macro (`state_space`) into its active list so we benchmark what we ship.
- This is independently useful and de-risks every later stage (you cannot tell if the joint
  objective helped without an out-of-sample yardstick).

### Stage 1 — macro-only differentiable joint fit (the real unlock)

- Pick the JAX-native macro (`vecm` already qualifies; `state_space` would need its fit ported from
  empirical-moments to a JAX objective, or a JAX twin).
- Implement `p_θ(m)` for the **level / inflation / crypto** market kinds as closed-form predictive
  CDFs (Stage table rows 1–3).
- Optimize `historical_logdensity + λ·calibration_KL` by SVI. Sweep `λ`; report both terms on the
  held-out split and the calibration page.
- Deliverable: a macro artifact whose factor drifts/vols are jointly informed by history **and** by
  what Manifold/Kalshi/Polymarket imply, produced by one command — replacing the fit-then-hand-tune
  loop for the macro channel.

### Stage 2 — event markets via hazard CDFs

- Fold `ipo_by_date` / collapse-style markets in using the model's **hazard CDFs**
  (`_public_market_marginal_cdf` and the collapse hazard) as `p_θ(m)` — still no rollout
  differentiation. This makes the IPO/collapse hazard params calibration-aware.

### Stage 3 — PE marks (the hard part, deferred)

- The structural PE sampler (`private_equity_risk.py`) is **numpy and MC-only**; its mark marginals
  (`P(mark > X)`) come from counting rollouts, not a closed form. Options, in rough order of effort:
  1. derive **closed/semi-closed marginal approximations** for the PE mark quantities the catalog
     actually scores (often a lognormal-ish marginal of `V(t)/V₀ / dilution(t)`), and differentiate
     those — mirrors Stages 1–2;
  2. **port the PE rollout to JAX** and differentiate through it, with continuous relaxations
     (Gumbel-softmax) for the discrete IPO/collapse/tender jumps, or score-function (REINFORCE)
     gradients for the discrete parts;
  3. keep PE on the existing offline NUTS fits (`bayes_dilution`, `bayes_mint_streams`) and only
     _wire_ their outputs automatically (see "coherence wins") without making them calibration-aware.
- Decide per-channel whether PE markets are worth the JAX port; the macro win does not depend on it.

## What this fixes (coherence wins)

Even partial adoption collapses several of the incoherences catalogued in the model review:

- **deployed = fitted.** The macro artifact (drifts, vols, _and_ the conditioning the config now
  hand-sets) comes out of one fit command; no hand-pasting.
- **calibration becomes an objective with a knob**, not a post-hoc scoreboard — `λ` makes the
  history-vs-market tradeoff explicit and tunable instead of implicit in hand-tuning.
- **one dilution fit, not two blocked-and-unwired ones.** Stage 3 (or even just auto-wiring) retires
  the M2.2-A OLS path (`derive_dilution_prior.py`, currently blocked — ~8000× 10y mark) in favor of
  the principled fit, with provenance.
- **honest eval.** Stage 0 replaces resubstitution scoring with a held-out split that includes the
  deployed model.
- **provenance.** A fit run ties artifact → evidence → params, replacing the "paste this YAML block"
  step that today loses the link between deployed numbers and the data/run they came from.

## Obstacles and open questions

- **`state_space` is empirical, not a JAX objective.** Either port it (fit by maximizing the same
  predictive log-density in JAX) or accept that the differentiable fit lives on `vecm` and retire
  `state_space` as the deployed macro. (Open: is the block-shrinkage that makes `state_space`
  attractive expressible as a differentiable penalty? Probably yes — it is a regularizer.)
- **Discrete events break end-to-end rollout autodiff.** Avoided in Stages 1–2 by differentiating
  marginals/hazard-CDFs; only resurfaces if we attempt Stage 3 option 2.
- **Calibration weighting `λ` and market quality.** Prediction markets vary wildly in liquidity and
  horizon; a flat `λ` over-weights thin far-future markets. Likely need per-market weights (volume,
  time-to-resolve) — this dovetails with the "aggregate metric / weighting TBD" item already in
  `augur/TODO.md`.
- **Identifiability.** Calibration pulls hardest exactly where history is thin (far-future tails).
  That is the point — but it risks the markets dominating. The held-out historical term and a
  sensible `λ` prior are the guardrails; watch for the fit chasing a single mispriced market.
- **Stationarity / regime.** History-fit and market-fit can genuinely disagree (the market prices a
  regime the historical window doesn't contain). `λ` is the dial; this is a feature, not a bug, but
  it needs monitoring.
- **Resolution lag.** Many catalog markets resolve years out, so the calibration term is scored
  against _current prices_, not outcomes — it teaches the model to agree with the crowd, not to be
  right. That is still useful (the crowd is a strong prior) but should be stated plainly and never
  conflated with backtested accuracy.

## Non-goals

- Not proposing to differentiate through the `augur/sim` product rollout (policies, taxes,
  cashflows) — only through the **exogenous** model's predictive marginals.
- Not removing the offline NUTS fits; they remain valid ways to produce priors and full posteriors.
- Not a near-term commitment; this is a direction, staged so the macro win can land without the PE
  port.
