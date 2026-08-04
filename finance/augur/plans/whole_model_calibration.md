# Whole-model calibration

Status: in progress. Backlog + tick-offs live in `augur/TODO.md` under
"Whole-model calibration". This doc is the design; delete/tombstone it once the
work is fully landed.

## Idea

Calibration today scores one **private-equity issuer** ("a stock"): it samples
that issuer's `PrivateEquityBundle`, slices it into per-rollout
`RolloutTrajectory`s, and resolves event markets (`ipo_by_date`,
`pre_ipo_failure`, `valuation_by_date`) apples-to-apples against live
prediction-market prices, scoring `D_KL(market ‖ model)` per market.

The augur model is really a **single joint generative distribution** over many
observables over time — `sp500`, `inflation`, `crypto:*`, `home_value:*`,
`rent:*` (the level roles) **plus** per-issuer PE event/valuation paths.
Each prediction market is a **marginal**: a (usually Bernoulli, sometimes
categorical) functional of one rollout's trajectory, with a market-implied
probability. We want to measure how well the joint model reproduces the
marginals implied by markets across **all** channels, not just one issuer.

Framing: the markets are marginals; per-market `D_KL(market ‖ model)` is the
divergence of the joint model's implied marginal from the market's. **v0 only
measures, per market** — no aggregation into a single loss (markets have very
different volumes/credibility; how to weight is its own decision), and no
fitting against them.

## What changes

1. **Scoring lifts from one issuer to every channel — vectorized over rollouts.**
   The model emits each channel as a `(rollout, month)` numpy matrix; that is the
   substrate. A macro resolver is a **vectorized** functional over the channel's
   matrix that returns per-rollout outcome **counts** (`yes`/`no`/`unresolved`,
   or per-bucket counts for a categorical), with no per-rollout Python object.
   Both PE-event and macro resolutions reduce to a `ResolutionCounts`, which a
   single `_clean_row` builder turns into a scored row. The Bernoulli KL helper
   is already channel-agnostic and is reused unchanged. (The existing PE path
   keeps its per-`RolloutTrajectory` loop for now; macro is fully vectorized.)

2. **Macro resolvers.** New event functionals over a level path:
   - `level_at_date` — point-in-time threshold (`value at month m {above,below} X`).
     Kalshi/Manifold S&P "above N **on** Dec 31" are point-in-time, not "ever by".
   - `inflation_yoy` — `path[m]/path[m-12] - 1 {above,below} X%` (Kalshi `KXCPIYOY`).
   - (range/bucket handled by the categorical family below.)

3. **Anchoring is mandatory for macro thresholds.** A sampled `sp500`/`inflation`
   path is only comparable to a market about the real index once anchored to the
   **live spot level** at `as_of`. The catalog carries per-series `anchors`
   (spot value at `as_of`); the engine rescales via
   `anchor_sampled_series_levels`.

4. **Categorical (multinomial) bucket families.** Kalshi lists S&P / CPI as a
   family of mutually-exclusive buckets. A `BucketFamily` carries the platform,
   series, `at_date`, and an ordered list of `(market_id, low, high)` buckets.
   The engine fetches each bucket price, normalizes them into a categorical
   `p_market`, computes the model categorical (fraction of rollouts landing in
   each bucket at `at_date`), and scores **multinomial** `D_KL(market ‖ model)`
   in bits, with per-bucket `p_market`/`p_model`.

## Initial wired markets (already-integrated platforms only)

- **S&P 500** → `sp500` level series:
  - Manifold `hISQySnLnu` "[ACX 2026] S&P 500 close above 7,500 at end of 2026"
    → `level_at_date(sp500, above 7500, 2026-12-31)`.
  - Kalshi `KXINXY-26DEC31H1600-*` bucket family → categorical over S&P close
    buckets on 2026-12-31.
- **CPI/inflation** → `inflation` level series:
  - Kalshi `KXCPIYOY-26*-T3.0` "CPI inflation above 3.0% for the year ending …"
    → `inflation_yoy(above 3.0%, …)`.

## Gotchas (keep tracking)

- **Anchoring**: thresholds are meaningless without anchoring the macro series to
  the live spot at `as_of`. The anchor lives in the catalog (data), refreshed
  alongside prices.
- **Price-index vs total-return**: Kalshi/Manifold reference the `^GSPC` price
  index. Score against the **price-index** `sp500` series (FRED `SP500`), not a
  total-return SPY proxy, or the threshold is biased up over time.
- **"by date" vs "on date"**: event markets are "ever by deadline"; Kalshi index
  buckets are point-in-time "on date". Different resolvers — don't conflate.
- **Bucket family normalization**: live per-bucket prices rarely sum to exactly
  1; normalize before computing categorical KL. Drop the family (don't 500) if a
  bucket fails to fetch, same per-row tolerance as today.
- **Horizon coverage**: a market whose `at_date`/deadline exceeds the sampled
  horizon is UNRESOLVED for that rollout; a family whose `at_date` is beyond the
  horizon is unscoreable and surfaced, not scored.
- **Unmodeled channels**: a market binding a series the active preset can't emit
  (`emittable_level_keys`) must surface as "unmodeled", never 500 — mirrors the
  `sample_sanity` `unmodeled` status.
- **No aggregate yet**: deliberately per-market only in v0. Volume-weighting /
  per-channel rollups deferred until the weighting question is decided.

## Deferred (see TODO)

- Drop `CalibrationCatalogConfig.issuer`: the catalog's per-market bindings
  declare their own target; the run covers the union of referenced
  issuers/series.
- Per-channel / volume-weighted aggregate metric once the weighting policy is
  chosen.
- Macro level fans (sp500/inflation percentile bands) in the calibration view,
  via a generalized `level_fan` (today's `mark_fan` is PE-only).
