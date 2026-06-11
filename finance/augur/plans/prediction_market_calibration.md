# Augur — Prediction-Market Calibration

Compare the augur **structured** exogenous model's rollouts against prediction-market
consensus (Manifold) and use the gaps to adjust the model. The calibration surface is
read-only with respect to the model; model changes are deliberate and land in
`augur/model/`.

## Architecture (implemented)

- **`augur/calibration/`** — generic, reusable:
  - `catalog.py` — a typed discriminated-union `MarketCatalog`
    (`ExactMarket` / `CorrelateMarket` / `UnmappableMarket`, so invalid combinations are
    unrepresentable) with a typed `CatalogMetadata` (`as_of`, `augur_model_as_of`, and a
    `model_anchor_date` property month indices are measured from).
  - `resolvers.py` — per-rollout resolution: a `RolloutTrajectory` (one rollout's slice
    of the augur→sim `PrivateEquityBundle`) resolves each `exact` market to YES / NO /
    UNRESOLVED. Event markets always map cleanly (`ipo_by_date`, `pre_ipo_failure`); with
    the opt-in M2 valuation channel, `valuation_by_date` ("valuation ≥ $X by date") is also
    scoreable for issuers carrying `company_valuation_usd` (UNRESOLVED otherwise). Revenue
    remains unmodeled.
  - `evidence_clients.py` — `EvidenceMarketReader` reading mirrored market snapshots
    from the augur-evidence checkout (2026-06: replaced the live `manifold.py` /
    `polymarket.py` / `kalshi.py` clients + valkey cache); tests inject a hermetic stub.
  - `calibration.py` — `run_calibration(...) -> CalibrationResult` (p_model + Wilson CI +
    unresolved share vs the live p_market for scored markets; price + reason + an optional
    related augur signal for surfaced ones) plus a `mark_fan` percentile helper.
  - `ipo_prior.py` — `derive_public_market_anchors(catalog, price_client)` turns the
    `ipo_by_date` markets into a monotone `public_market_cdf_anchors` vector for the model
    (the end-to-end "markets feed the model" path); a `derive_ipo_prior` binary prints a
    paste-ready config block.
- **`augur/api`** — an exogenous-only `POST /api/calibration/run` (no personal-finance
  sim) over the materialized model presets; `preset_id` defaults to the deployment's
  shared `default_model_id`. `GET /api/calibration` surfaces the configured catalog's metadata
  (`CalibrationInfo`: label/issuer), not the markets themselves.
- **`augur/frontend`** — a Product | Calibration tab: model picker, scored vs surfaced
  markets, and the issuer mark fan.
- **gaffer-private** — the deployment supplies the curated `catalog.yaml`
  (`gaffer_augur/openai_stock/markets/`), registers it under `calibration_catalog`, and
  wires `MANIFOLD_API_KEY`.

Prices come from the git-mirrored snapshots in the augur-evidence repo (2026-06; the
scraper refreshes every catalog-referenced market each run — before that they were
fetched live per request through a valkey read-through cache).

## Findings — live `structured` model vs Manifold (~6k rollouts, pre-M1)

| market (deadline)            | p_model | p_market |
| ---------------------------- | ------- | -------- |
| IPO before 2027              | 0.04    | 0.75     |
| IPO before 2028              | 0.10    | 0.89     |
| IPO before 2029              | 0.16    | 0.93     |
| IPO by 2030                  | 0.22    | 0.80     |
| collapse/acquired before IPO | 0.18    | 0.09     |

Two coupled gaps: (1) **going-public timing** — the model's flat, memoryless
`annual_public_market_probability = 0.07` accumulates P(IPO by t) slowly and never
saturates, while the market treats an OpenAI IPO as near-term and near-certain (a rising
hazard saturating ~2028); the model is both too low AND the wrong _shape_. (2) **Pre-IPO
failure runs hot (0.18 vs 0.09)** — largely a consequence of (1): staying private longer
accrues more exposure to the (small) collapse/acquisition hazards. Fixing IPO timing pulls
both toward the market.

## Model-adjustment stages

These change the structured model (`augur/model/private_equity_risk.py`) and/or its config;
the calibration report is the metric that says whether a change closed the gap.

### M1 — empirical, market-derived going-public timing ✅ (landed)

The flat `annual_public_market_probability` was never evidence-fit (it carried no
IPO-timing information), so the model now accepts the prior **empirically**:
`PrivateEquityRiskIssuerConfig.public_market_cdf_anchors` — `(month, cumulative_probability)`
pairs pinning a front-loaded, saturating CDF. Within each bucket the monthly hazard is
constant via survival interpolation, `h = 1 − (S_{i+1}/S_i)^(1/(m_{i+1}−m_i))`; past the
last anchor it reverts to the flat `annual_public_market_probability` TAIL (so the market
saturating at ~0.93 does NOT force the residual mass to "never"). Empty anchors → the legacy
flat-hazard behaviour, so existing configs are unchanged. For openai's anchors
{2027:0.75, 2028:0.89, 2029:0.93} that is ~18 %/mo (2026) → ~6.7 %/mo (2027) → ~3.7 %/mo
(2028), exact at every anchor (vs today's flat ~0.6 %/mo). `ipo_prior.py` derives the
anchors from live Manifold prices, dropping CDF-violating noise (e.g. a "by 2030 = 0.80"
below 2029's 0.93). Competing risks (collapse can preempt) make realized P(IPO by t) a hair
under the anchors; M1 also pulls pre-IPO-failure down. The `(cdf_anchors → monthly hazard +
tail)` machinery is generic — reusable for any event we get market term structure for.

### M2 — company valuation + dilution, coupled to the per-unit mark ✅ (mark v1 landed)

augur used to emit only a per-**unit** mark, so (a) the valuation markets ($1T-by-date) were
unmappable and (b) the per-share value the user holds ignored **dilution** (new shares in
funding rounds erode per-share value even as the company cap grows).

**Landed (v1, opt-in).** `PrivateEquityRiskIssuerConfig` gains a `current_valuation_usd`
anchor (`V0`); when set, the per-unit latent mark stops being a standalone Student-t random
walk and becomes a quantity DERIVED from a sampled company valuation `V(t)` and a
deterministic dilution factor:

```
latent_mark(t) = current_mark_usd × (V(t) / V0) / dilution_factor(t)
dilution_factor(t) = (1 + annual_dilution_rate) ** (t / 12)   # dilution_factor(0) = 1
```

`V(t)` is a log-space Student-t random walk anchored at `V0`, driven by its OWN derived seed
stream (`<issuer>:pe_risk_valuation`) and its own RW params (`valuation_monthly_log_return_mu/
sigma`, `valuation_student_t_nu`). Only ratios enter the mark, so `V0`/`shares0` cancel at
`t=0` and `latent_mark[:,0] == current_mark_usd` exactly while `company_valuation_usd[:,0] ==
current_valuation_usd`. The valuation flows out on the new `PrivateEquityBundle`
`company_valuation_usd` channel, which makes `valuation_by_date` markets scoreable
(`resolvers.py`).

**Opt-in / zero-regression.** Leaving `current_valuation_usd` unset selects the legacy
independent latent-mark random walk verbatim and emits an all-zeros `company_valuation_usd`
channel (the channel-off sentinel: a positive market cap is never all-zeros). The valuation
seed stream is NOT derived in the off branch, so the mark/event arrays stay byte-identical to
pre-M2 for every existing config — guarded by a fixed-seed regression test.

### M2.2 — stochastic dilution + evidence fit

v1's `dilution_factor(t) = (1 + annual_dilution_rate)^(t/12)` is **deterministic**: an
identical curve in every rollout, so dilution only shifts the per-unit-mark distribution
down — it adds **zero** per-share spread. All current holding-value uncertainty comes from
`V(t)`. M2.2 makes share count per-rollout-random and fits the parameters to evidence, in two
themes — **structure** (what the random object is) and **fit** (how its parameters are
chosen) — staged A→D so each lands on its own.

**Structure.** Two physically-distinct sources of dilution uncertainty:

1. _Rate uncertainty (epistemic)._ We don't know the long-run mint rate. Model as a
   per-rollout fixed draw `r ~ LogNormal(μ_r, σ_r)`, applied geometrically
   `shares(t) = shares0 · (1 + r)^(t/12)`. log-share variance grows **quadratically** in t →
   a widening cone. This dominates the 10-year per-share spread (the holding-value question).
2. _Timing/lumpiness (aleatoric)._ Even given a rate, primary rounds land at random months
   with random sizes (~4–14%/round, ~annual). A within-path process; variance grows
   **linearly** in t. Matters for path shape; averages out for terminal value. Maps to the
   real split: continuous employee mint (PPU/RSU → the smooth drift `r`) + discrete primary
   rounds (lumpy). Secondaries (e.g. the 2025-10 ~$500B employee tender) **re-price but do not
   dilute**, so any lumpy term must fire on _primary_ rounds only — a new event kind distinct
   from the model's existing (secondary) tender events.

**Fit.** A latent-trajectory (state-space) fit in `augur/fit` (which already ingests
`valuation_observation`s but collapses them to a static `scale_prior.current_market_cap_usd`):

```
log price(t)  = log V(t) − log shares(t)  (+ obs noise)   ← tender prices (tight) + FMV (loose, lagged)
log V(t)      = latent GBM (drift μ_V, vol σ_V)           ← valuation_observations
log shares(t) = log shares0 + (t/12)·log(1+r)
```

Likelihood constraints the data **forces** (naive fitting gets these wrong): (a) attribute
share growth to _primary_ rounds only — a _secondary_ valuation obs constrains `V(t)` but not
`shares`; observations are already round-type tagged. (b) The $687.69 FMV is a stale (~4mo)
administrative mark → large/lagged obs noise, unlike tight tender prices; `V0/shares0` ≈
$824/sh vs the $687.69 mark — that ~20% wedge **is** the staleness and the fit should _explain_
it via the lag term, not force-reconcile it. (c) `shares0 ≈ 1.034B` is a soft recap-era prior,
not an anchor. Identifiability is honest: `μ_r`, `μ_V`, `σ_V` are reasonably pinned (implied
shares ≈ 418M @2023-04 → 749M @2024-10 → ~1.2B @2026 ≈ **30–40%/yr**, _above_ v1's hand-set
20%); **`σ_r` is poorly pinned by ~5 paired points — by design**, since that sparsity _is_ the
dilution uncertainty M2.2 expresses, so `σ_r` should come out wide and a fit should say so via
a fat posterior (argues for propagating parameter uncertainty into the per-rollout draw).

**Staging** (tracked in `augur/TODO.md`):

- **M2.2-A — per-rollout stochastic dilution rate ✅ (sampler + generic fit landed).** Each
  rollout draws `r = annual_dilution_rate · exp(annual_dilution_rate_log_sigma · z)`,
  `z ~ N(0, 1)` — a **median-anchored** LogNormal (`median(r) == annual_dilution_rate`
  exactly; NOT mean-anchored, which would inflate dilution by `exp(σ²/2)` as σ grows) — off
  its OWN `<issuer>:pe_risk_dilution` seed stream (mirroring `V(t)`'s `:pe_risk_valuation`),
  into the existing coupled-mark formula. The new config knob `annual_dilution_rate_log_sigma`
  flows through ONE unified path: with σ=0 (default) `exp(0)=1` collapses the draw so every
  rollout gets exactly `annual_dilution_rate`, byte-identical to the M2 deterministic factor
  (no `if σ==0` special-case; proven by a byte-identity regression test). Because the draw is
  on an independent stream, the mark/event/regime/valuation arrays are unperturbed when σ
  turns on — only the per-share mark spread widens (a quadratic-in-t cone). The generic fit
  lives in `augur/fit/dilution_prior.py` (+ a `derive_dilution_prior` binary mirroring
  `derive_ipo_prior`): an implied-shares (`valuation/price`) log-linear OLS recovers
  `annual_dilution_rate = exp(slope) − 1` and `annual_dilution_rate_log_sigma` from residual
  scatter (honestly wide with ~5 points). Independent of `V(t)` (conservatively _over_-states
  per-share spread by omitting the hedge in B). Touches only the dilution side — no new bundle
  event machinery. **Follow-up:** gaffer config wiring (running `derive_dilution_prior` on the
  private evidence → the issuer's `config.yaml` values) is not yet done.
- **M2.2-B — V↔dilution correlation.** Good-company worlds (V rises fast) raise more primary
  capital → dilute more, so per-share is partly hedged; drawing `r` independent of `V` _over_-
  states per-share spread. Make _both_ `V`'s log-drift and `r` per-rollout draws from a joint
  fitted posterior with correlation ρ. Requires making `V(t)`'s drift per-rollout (a change to
  the V sampler, not just dilution), so it is its own phase.
- **M2.2-C — lumpiness + secondary rigor (deferred).** Discrete primary-round dilution as a new
  V-coupled event kind, distinct from the existing (secondary) tender events; likelihood that
  attributes only primary rounds to share growth and treats secondaries as pure re-pricing.
- **M2.2-D — full Bayesian posterior + decaying valuation drift (designed; prototype validated).**
  Two coupled changes, both needed; a NUTS prototype on the openai evidence (2026-05-30,
  16 obs) established the numbers below.

  **Why the OLS fit (M2.2-A) cannot be deployed.** Running `derive_dilution_prior` on the
  gaffer openai evidence yields `annual_dilution_rate ≈ 0.476`, `valuation_monthly_log_return_mu
≈ 0.1075` (~244%/yr). Validated against `sample_sanity.yaml`'s `private_equity_mark_checks`
  (512 rollouts × 120 mo): m12 p50 = 2.39 (band [0.7,1.8]) and **m120 p50 = 8035** (band
  [0.3,4.0]) — fails by ~2000×. Root cause is twofold: (1) the OLS log-linear slope extrapolates
  the 2023-25 boom as a _forward_ rate; (2) `mark = current·V(t)/V0/(1+r)^(t/12)` couples drift
  and dilution, which OLS fits jointly but the config then compounds independently, so dropping
  just `mu_V` back to 0.02 makes the 48% dilution undershoot the floor (m120 p50 = 0.22).

  **Part 1 — NUTS posterior (prototyped, big improvement, still insufficient alone).** A small
  numpyro state-space model (latent log-V random walk + deterministic log-share path; per-obs
  `uncertainty_log_sigma` as the likelihood scale; INFORMATIVE priors: `mu_V ~ N(0.02, 0.025)`,
  `log(1+r) ~ N(log1.20, 0.30)`) over ALL 16 observations, fit with NUTS, mirrors the existing
  `augur/model/vecm.py` numpyro pipeline (~90 lines; built + ran in one shot). Posterior medians:
  `annual_dilution_rate ≈ 0.29` (p5–95 0.19–0.40), `annual_dilution_rate_log_sigma ≈ 0.05`,
  `mu_V ≈ 0.071` (~134%/yr). The priors regularize the 4-point extrapolation: m120 p50 drops
  8035 → **381** (21× better) — but **still fails the gate**, because a constant-drift random
  walk compounds even the regularized near-term rate over 10 years. Bayesian inference fixes the
  _uncertainty_ and over-extrapolation, not the _structural_ forever-drift.

  **Part 2 — drift structure: scale-dependent mean reversion (the load-bearing fix).** The first
  cut tried a CALENDAR-time decay `mu_V(t) = mu_∞ + (mu_0 − mu_∞)·0.5^(t/τ)` (shipped in the
  sampler + `bayes_dilution.py`). On the openai evidence it improves a lot but still overshoots:
  even with a tight long-run prior the NUTS fit keeps `mu_∞ ≈ 0.033/mo (~49%/yr)` and `m120 p50
≈ 100×`, because (a) every observation is still rising steeply through the latest one, so the
  data gives no reason to believe the boom ended, and (b) calendar decay replays the historical
  boom from t=0=now. **Calendar time is the wrong axis.** The right structure makes drift a
  function of company SIZE, not elapsed time:

  ```
  mu_V(s) = mu_mature + (mu_young − mu_mature) · exp( −max(0, s − s_young) / s_scale )
  ```

  where `s = log V(t)` is the realized log enterprise value. A small company (`s ≤ s_young`)
  gets the hot `mu_young`; a large one reverts toward `mu_mature`, and KEEPS maturing as it
  grows. This is data-driven, not a hidden prior: the model tames the boom _because OpenAI is now
  $852B_ (an observed fact), and it self-corrects per rollout — a rollout that stays small keeps
  its upside, one that booms matures early. Empirical backing: firm growth-rate dispersion scales
  as `σ ~ S^(−0.2)` and mean growth declines with size (Stanley/Amaral scaling laws; decacorns
  "grow slower"). Implementation note: state-dependent drift makes `V(t)` a genuine SDE, so
  `_sample_company_valuation_vectorized` becomes a per-month loop (vectorized across rollouts) —
  a contained sampler change, trivial cost at 120 × few-thousand.

  **Research-grounded starting values (conservative; population-fittable later).** Until a
  startup-panel fit (below) replaces them, use educated guesses:
  - `mu_mature` ≈ **0.008/mo (~10%/yr nominal)** — the S&P 500's ~100-yr nominal CAGR. A mega-cap
    at maturity is not assumed to beat the index. Deliberately conservative.
  - `mu_young` ≈ 0.035–0.05/mo (~50–80%/yr) — decacorn-era growth; the fit lifts it from data.
  - `s_young` / `s_scale` — maturity onset around $10–50B, reverting over the back half of the
    size range. Tune so the openai forward path lands in the `sample_sanity` bands.

  **Catastrophic loss is a SEPARATE channel — do not fold it into drift.** Asymptoting the
  central drift toward ~market return does NOT undersell "lose everything," because total loss is
  a discrete-event / no-realization phenomenon that lives in the hazard channels (collapse,
  legal-impairment, forced-recovery) and the no-liquidity tail, not in the going-concern drift.
  Population base rates make this vivid and confirm augur currently _under_-states it: of 2010–15
  Series C companies only **38% exited within a decade (62% never delivered liquidity)**; post-
  2021, **>80% of unicorns sit below their peak and ~30% fell under $1B**; WeWork went $47B →
  bankruptcy (−99.9%). So the realization-risk tail is empirically _fat_, and the pre-IPO-failure
  calibration gap we already measured (model 0.014 vs market 0.092) is the same under-statement.
  **Linked work item:** bump the collapse / no-liquidity hazards toward these population base
  rates (tracked in TODO.md; it is the loss-tail counterpart to this drift work).

  **North star — hierarchical population prior (the real reason to be Bayesian).** One company's
  16 points can never answer "does the boom persist?"; only a _reference class_ can. Fit the
  scale-reversion hyperparameters `(mu_young, mu_mature, s_young, s_scale)` + dilution + the
  loss-hazard base rates across a POPULATION of startup trajectories, then treat each issuer
  (openai) as one draw shrunk toward the population (partial pooling). That converts every
  educated guess above into a pooled estimate with provenance, and simultaneously calibrates the
  loss tail. **Survivorship bias is the central trap:** public/cheap startup datasets over-
  represent survivors, which would bias drift up and the loss tail down — exactly backwards for a
  realization-risk model. The dataset must include down rounds, shutdowns, and never-exited
  "walking dead." Reserve schema room for terminal/failure outcomes, not just valuation marks.
  This is the eventual home of the dilution evidence schema's primary/secondary round labels and
  round sizes too (M2.2-C), since a population fit wants the lumpy-vs-continuous split.

  **Deploy mechanism (orthogonal, later).** All of the above is the GENERATIVE structure; how the
  posterior is propagated forward at deploy time is a separate axis. Today: freeze the posterior
  SUMMARY (means/SD) into scalar config knobs, resampled cheaply by the existing numpy sampler
  (like vecm's `.npz`). A later upgrade is full posterior-PREDICTIVE deploy — the openai PE
  channel sampling forward from the conditioned posterior (carrying the full joint + latent-state
  uncertainty, not just marginals). That is more honest fan-out but needs a private trained-
  artifact pipeline in gaffer (the evidence is private), mirroring `state_space_macro_artifact.json`.
  It does NOT fix the median overshoot — that is the generative structure's job — so it follows
  the scale-reversion work, not precedes it.

  **Status.** Shipped (sampler + fit, synthetic-green): the scale-reversion drift is in the
  sampler (`ValuationDriftScaleReversion` submodel, integrated as an SDE) and in the NUTS fit
  (`augur/fit/bayes_dilution.py`, also SDE via `jax.lax.scan`). The Bayesian dilution rate is
  sane and the structure recovers a known shape from synthetic data.

  **KEY FINDING — openai alone cannot identify the reversion shape (proves the population prior
  is necessary, not just nicer).** Running the full scale-reversion fit end-to-end on the real
  openai evidence blows up: `mu_mature → 411%/yr` (32σ off its prior), `shares0 → $6e3`, dilution
  negative. Cause: **every openai valuation observation ($28B+, 2023 on) is already in the
  "large" regime** — there is NO observed small-company phase to anchor `mu_young` vs `mu_mature`
  or the onset — and the 2019–21 price-only stretch (no paired valuation) adds a `shares0`↔`V`
  level degeneracy. A single company "already huge and booming" cannot, even in principle, tell
  you how growth decays with size. So:
  - **openai deployment (DONE — `fit_scale_reversion_shape=False`, the default).**
    `fit_bayesian_dilution_prior` now fixes the reversion SHAPE (`mu_mature`, `mu_young`, onset,
    scale) at the `BayesianDilutionPriors` centers (each justified in that dataclass's per-field
    docs) and samples only the identifiable params (level, σ_v, share count, dilution). On the
    real openai evidence this is stable (0 divergences) and lands `r≈0.27`, `mu_mature=10%/yr`,
    forward `σ_v≈43%/yr` — **inside all four `sample_sanity` mark bands** (m12 p50≈1.0, m120
    p50≈0.45, both p1..p99 in band). The forward `σ_v` prior is deliberately tighter than the
    boom-era in-sample scatter (~73%/yr), for the same forward-vs-in-sample reason as the drift.
  - **TODO — fit the shape via the hierarchical population prior** (below): the ONLY thing that
    can _identify_ the reversion shape, because it borrows the small-company phase from _other_
    companies; the single-issuer blow-up above is the empirical justification for prioritizing it.
    Until then the fixed shape is a load-bearing educated guess, not a fitted result. When the
    population fit lands, it replaces the fixed `BayesianDilutionPriors` shape centers and the
    `fit_scale_reversion_shape=True` path becomes the per-issuer fit shrunk toward that population.

### M3 — IPO lockup: probabilistic + refined

The structured model already sets a _fixed_ `public_market_lockup_months: 6` and the sim
honours it (sales gate on `liquidity_open = ~liquidity_blocked` in
`augur/sim/engine/jax_engine.py`), so the user genuinely cannot sell during the post-IPO lockup
today. Refine: (a) sample the lockup **duration per rollout** from a model-owned
distribution (different worlds get different lockups) instead of a constant 6 months;
(b) confirm the central duration (typical lockups are 90–180 days) and that it applies to
the user's PE lots; (c) consider gradual post-lockup selling / price impact instead of
instant full liquidity. Lower priority than M1/M2.

### M4 — close the loop

After each model change, re-run `/api/calibration` and watch the per-market
`|p_model − p_market|` shrink. The calibration tab is the live metric. Keep the evidence-fit
regularizer — Manifold is play-money; move _toward_ it, don't overfit. The hand-fit and any
market-tuned model stay separate epistemic objects.
