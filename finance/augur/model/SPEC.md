# `structural_macro` — what it models, and what it does not

A small purpose-built economy for one question: **how should I allocate my current assets?**
It exists because the general providers answer a different question. `independent` gives every
series its own random walk, so a bond fund's price and its payout can drift apart for no
economic reason. `vecm`/`state_space` fit a joint factor model, but on ONE aligned window that
the shortest series truncates — currently ETH, at ~2017. This model has no crypto, so nothing
forces it to a short window, and its coupling is structural rather than estimated.

## What it promises

**Emissions are dollar primitives, never ratios.** Per-unit price, per-unit monthly
distribution, and a CPI level. Nothing downstream divides, and no rate exists anywhere in the
simulator: a fund pays `units × distribution_per_unit`, exactly as a bond pays `face × rate`.

**One rate move prices the whole sleeve coherently.** Price and payout are two different
functions of the same latent state, so a rate rise moves a fund's price down and its payout up
without anything downstream knowing they are related — and _not proportionally_: the price
takes its hit in the month the yield moves, the payout converges over roughly the fund's
duration. That lag is the model's central claim and is what reproduces 2022–2025.

**An instrument is a config row, not a factor.** A symbol, a duration, a static spread. Adding
a fourth fund adds a row, not a fourth random walk. There is no "factor" concept in the public
surface at all; what happens between the state and the emissions is the model's own business.

**Per-rollout seeding.** A rollout's path depends only on its own seed, never on the batch.

## What is fitted, and on what

| Block       | Source                       | Window            | Months |
| ----------- | ---------------------------- | ----------------- | ------ |
| short rate  | FRED `FEDFUNDS`              | 1954-07 – 2026-07 | 865    |
| term spread | FRED `GS10` − `FEDFUNDS`     | 1954-07 – 2026-07 | 865    |
| inflation   | FRED `CPIAUCSL`              | 1947-01 – 2026-06 | 952    |
| equity      | Yahoo `VFINX` adjusted close | 1980-01 – 2026-08 | 559    |
| `rate_beta` | `VFINX` on Δ`FEDFUNDS`       | 1980-01 – 2026-07 | 558    |

Each **marginal** is fitted on its own longest history; only a **cross-block** parameter needs
a shared window, because a covariance off a non-overlap is undefined rather than merely noisy.
`rate_beta` is the model's one cross-block parameter and the only thing paying that cost.

Instrument durations, spreads, prices and tax character are **not** fitted — they are facts
about specific funds, stated in config.

## Gaps

Every one of these is a reason a number out of this model can be wrong. They are ordered by
how much they move an allocation answer.

1. **Inflation is i.i.d. around a constant drift, and nothing reacts to it.** The monthly rate
   draws an independent shock, so the price level is a random walk with no persistence. Two
   consequences, both measured against CPI 1947–2026:
   - **No regimes, and 30-year uncertainty ~5× too narrow.** Realized monthly inflation has
     AR(1) ρ = **0.57**; this model assumes 0. Over 30 years the model's 1σ band on the price
     level is ×2.64–3.01, where history delivered ×1.95–4.85 (a spread **4.8×** the model's).
     A 1970s decade is unreachable here, and so is a sustained-low one.
   - **No Fed reaction.** The short rate and trailing-12-month inflation correlate at **0.70**
     in the data — the strongest relationship in this whole model's subject matter — and at
     exactly **0** here, because the two processes share no state.

   Together these bias the _bond_ sleeve specifically, and hard. A spend indexed to CPI against
   a fund with a fixed nominal starting yield turns a small gap between the two into a
   near-certainty compounded over 30 years, with no mechanism for rates to rise in response and
   close it. **Any all-bond or bond-heavy result from this model should be read as pessimistic
   for that reason**, and the fix is structural: inflation as an AR(1) rate, and an inflation
   term in the short rate's drift, so the two stop being independent.

2. **Equity and rates are independent.** `rate_beta` fits to **+1.57 (R² = 0.0041)** over
   1993–2026 and **−0.62 (R² = 0.0051)** over 1980–2026: the sign is not stable across windows
   and neither explains half a percent of variance. So it is zero and the model carries no
   bond/equity coupling at all. **A question that turns on bond/equity correlation is not
   answered here** — which is exactly what a 60/40 study turns on, making this the largest gap.
3. **Equity history is 46 years.** VFINX 1980–2026 is the longest _total-return_ series
   obtainable, covering 1987, 2000 and 2008 but not 1929 or the 1970s inflation. `^GSPC`
   reaches 1970 and corroborates both the drift (8.29% price + ~2.9% yield ≈ 11.2%) and the
   volatility (15.3% vs 15.4%), but it is price-only, so using it directly would mean choosing
   a dividend add-back — a modelling decision, not a data pull.
4. **No cyclical credit spread.** A muni's spread over the curve is a constant, so the model
   cannot produce a muni selloff that Treasuries escape — which is exactly what a credit event
   looks like, and exactly when a floor is tested.
5. **Mismatched inflation and equity windows.** Inflation reaches 1947, equity only 1993, so
   the implied real equity return pairs a sample containing the 1970s with one that does not.
   It lands near the long-run realized real figure, which is a coincidence, not a control.
6. **The rate means are barely identified.** OLS on a near-unit-root series biases mean
   reversion upward and pins the long-run mean weakly: the same fit gives a 4.93% short-rate
   mean over 1954–2026 and 1.71% over 1990–2026. Read the sigmas; sweep the means.
7. **A linear curve.** The yield at an instrument's duration interpolates between the short
   rate and the 10-year point. It orders cash, a short fund and an intermediate fund correctly;
   it cannot price a barbell against a bullet.
8. **No equity distribution.** `IncomeCategory` has no qualified-dividend rate, so an equity
   dividend routed through the interest path would be overtaxed as ordinary income. Equity
   emits a total-return price and no payout — consistent, but it means dividend TIMING and its
   tax treatment are absent.
9. **No held-to-maturity instrument.** Every fixed-income holding here is a marked fund with
   duration risk. A TIPS or Treasury ladder held to maturity has none, so a study whose floor
   is a real ladder should expect this model to understate that floor.
10. **Nothing is regime-switching.** Rates mean-revert around one level with one volatility.
    The 2009–2021 ZIRP era and 1981 are the same process here, differing only by draw.
11. **No housing, no private equity, no crypto.** By design — compose with another provider
    through `CompositeModel`. Listed so the absence is a statement rather than an oversight.

## Not fittable here

The provider implements `Sampler` only — not `Fittable`, not `Scorable`. Its parameters are
config defaults produced offline by `//finance/augur/fit:structural_macro` and pasted in with
provenance, deliberately: the fit does not go through the joint fit's single aligned window,
and that separation is what buys the long histories in the table above.
