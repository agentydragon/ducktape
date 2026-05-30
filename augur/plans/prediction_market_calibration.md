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
    UNRESOLVED. Only EVENT markets map cleanly (`ipo_by_date`, `pre_ipo_failure`); augur
    models neither company valuation nor revenue.
  - `manifold.py` — an injectable `ManifoldClient` (httpx) fetching live YES
    probabilities per market; tests inject a hermetic stub.
  - `calibration.py` — `run_calibration(...) -> CalibrationResult` (p_model + Wilson CI +
    unresolved share vs the live p_market for scored markets; price + reason + an optional
    related augur signal for surfaced ones) plus a `mark_fan` percentile helper.
  - `ipo_prior.py` — `derive_public_market_anchors(catalog, price_client)` turns the live
    `ipo_by_date` markets into a monotone `public_market_cdf_anchors` vector for the model
    (the end-to-end "markets feed the model" path); a `derive_ipo_prior` binary prints a
    paste-ready config block.
- **`augur/api`** — an exogenous-only `POST /api/calibration/run` (no personal-finance
  sim) over the materialized model presets; `preset_id` defaults to the deployment's
  shared `default_exogenous_preset_id`. The configured catalog rides on `/api/bootstrap`.
- **`augur/frontend`** — a Product | Calibration tab: model picker, scored vs surfaced
  markets, and the issuer mark fan.
- **gaffer-private** — the deployment supplies the curated `catalog.yaml`
  (`gaffer_augur/openai_stock/markets/`), registers it under `calibration_catalog`, and
  wires `MANIFOLD_API_KEY`.

Prices are ALWAYS fetched live (no stored snapshots).

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

### M2 — company valuation + dilution, decoupled from per-unit mark

augur emits only a per-**unit** mark, so (a) the valuation markets ($1T-by-date) are
unmappable and (b) the per-share value the user holds ignores **dilution** (new shares in
funding rounds erode per-share value even as the company cap grows). Add a company
market-cap trajectory + a share-count/dilution process with `mark = cap / shares`: the
valuation markets become scoreable (more calibration signal) and the holding value reflects
dilution — the most realistic-but-missing piece for the actual finance question. Cost: a few
more parameters to fit. **Worth it — stage after M1.**

### M3 — IPO lockup: probabilistic + refined

The structured model already sets a _fixed_ `public_market_lockup_months: 6` and the sim
honours it (sales gate on `liquidity_open = ~liquidity_blocked` in
`augur/sim/engine/phases.py`), so the user genuinely cannot sell during the post-IPO lockup
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
