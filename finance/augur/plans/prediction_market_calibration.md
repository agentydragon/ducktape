# Augur prediction-market calibration

This is the compact architecture and model-design record for comparing Augur's
structured model against prediction markets. Active work items live in
`../TODO.md`, especially the whole-model calibration and M2.2 stochastic
dilution sections.

## Purpose

Prediction markets provide external marginal beliefs about outcomes Augur
already simulates or should simulate. Calibration runs compare Augur rollout
frequencies with market-implied probabilities, then model changes land
deliberately in `augur/model/` or fitting code. The calibration surface itself
is read-only with respect to the model.

## Implemented Architecture

- `augur/calibration/catalog.py` defines a typed market catalog with exact,
  correlated, and unmappable market shapes.
- `augur/calibration/resolvers.py` maps one rollout into market outcomes such as
  IPO-by-date, pre-IPO failure, valuation-by-date, and macro level buckets.
- `augur/calibration/evidence_clients.py` reads mirrored market snapshots from
  the `augur-evidence` checkout. Live per-request market clients and the Valkey
  read-through cache were removed.
- `augur/calibration/calibration.py` produces per-market model probabilities,
  confidence intervals, unresolved share, related Augur signals, and issuer mark
  fan summaries.
- `augur/calibration/ipo_prior.py` derives monotone IPO CDF anchors from market
  term structure for paste-ready model config.
- `augur/api` exposes `GET /api/calibration` and
  `POST /api/calibration/run` over materialized model presets.
- `augur/frontend` has a Product | Calibration view with model selection,
  scored/surfaced markets, and the issuer mark fan.

The deployed catalog is private configuration. Public code should treat catalog
rows as data, not as hard-coded product assumptions.

## Landed Model Changes

### M1: Market-derived IPO Timing

The old flat `annual_public_market_probability` could not express a
front-loaded, saturating market CDF. Issuer config now supports
`public_market_cdf_anchors`: `(month, cumulative_probability)` pairs converted
to bucket hazards by survival interpolation. Empty anchors preserve the legacy
flat hazard. The derived anchors fix both the IPO timing shape and much of the
pre-IPO-failure overexposure caused by staying private too long.

### M2: Company Valuation and Dilution

Issuer config can opt into a company valuation channel with
`current_valuation_usd`. When enabled, the per-unit latent mark is derived from
sampled company valuation divided by a dilution factor:

```text
latent_mark(t) = current_mark * (V(t) / V0) / dilution_factor(t)
```

This makes valuation-by-date markets scoreable through
`company_valuation_usd`. Leaving `current_valuation_usd` unset keeps the legacy
mark random walk and emits the off-channel sentinel, so existing configs remain
byte-identical.

### M2.2: Stochastic Dilution and Evidence Fit

The first M2 dilution factor was deterministic, so all per-share spread came
from valuation. The code now supports per-rollout stochastic dilution via
`annual_dilution_rate_log_sigma`, plus evidence-fit helpers:

- `augur/fit/dilution_prior.py` and `derive_dilution_prior` fit a quick OLS
  implied-share trend. This is useful diagnostically but not deployable for the
  OpenAI evidence because the valuation trend over-extrapolates.
- `augur/fit/bayes_dilution.py` fits a Bayesian latent valuation and share path
  with uncertainty-aware observations.
- The forward sampler supports scale-dependent mean-reverting valuation drift,
  where young/small companies can have high drift that decays toward mature
  drift as valuation scale grows.

The key finding is identifiability: one issuer observed in one size regime
cannot identify the whole scale-reversion shape. The production direction is to
fix the shape at justified priors for single-issuer fits, fit only identifiable
parameters, and later learn the shape from a hierarchical population prior.

## Design Lessons

- Prediction markets are marginals. Compare and fit per market before inventing
  aggregate scores.
- CDF term structure needs monotonicity repair; market ladders can be noisy or
  internally crossed.
- Quote quality matters. The catalog exposes structured quotes, and low-quality
  rungs are down-weighted in fitting, but the UI still needs to show stale,
  one-sided, wide-spread, or no-quote states.
- Live market fetching should not sit in the request path. The stable boundary is
  the `augur-evidence` checkout shared by calibration and model evidence reads.
- For private-company valuation, median trajectory and loss/liquidity tail are
  separate problems. Mean-reverting drift does not model "lose everything";
  collapse, no-liquidity, legal impairment, and forced-recovery hazards need
  their own population-informed tail.

## Remaining Work

Use `../TODO.md` as the source of truth. The durable lanes are:

- Surface quote fetched-at and quote-quality states in the calibration UI.
- Add macro level fans and an eventually weighted aggregate calibration metric.
- Deploy the M2.2 Bayesian/scale-reversion fit once private config and
  `sample_sanity` agree.
- Add V/dilution correlation and primary-round lumpiness only after the median
  fit is deployable.
- Fit hierarchical population priors across many startup trajectories, including
  down rounds, shutdowns, and never-exited companies, to avoid survivorship bias.
