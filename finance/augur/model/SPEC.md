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

| Block       | Source                                | Window            | Months |
| ----------- | ------------------------------------- | ----------------- | ------ |
| short rate  | FRED `FEDFUNDS`                       | 1955-08 – 2026-06 | 850    |
| term spread | FRED `GS10` − `FEDFUNDS`              | 1955-08 – 2026-06 | 850    |
| inflation   | FRED `CPIAUCSL`, trailing-year log    | 1955-08 – 2026-06 | 850    |
| equity      | Ken French factors, CRSP total market | 1926-07 – 2026-06 | 1200   |
| `rate_beta` | equity on Δ`FEDFUNDS` (fits to zero)  | 1980-01 – 2026-07 | 558    |

The three macro states share ONE window because they are fitted JOINTLY as a VAR(1):
persistence, the Fed's reaction to inflation, and correlated innovations all come from that
single estimate, and `fit_macro_var` inner-joins the three series, so the shortest one sets the
start. Equity is a marginal and keeps its own much longer history; `rate_beta`, the one
parameter linking equity to the macro state, pays the common-window cost and comes out
indistinguishable from zero (gap 2).

**Deviation worth knowing:** the VAR reads `CPIAUCSL` (seasonally adjusted) while the historical
replay's record reads `CPIAUCNS` (not adjusted, and reaching 1913 rather than 1947 — gap 4). The
two are not interchangeable at monthly frequency, but both consumers read a trailing-year ratio,
which is where seasonality cancels.

Instrument durations, spreads, prices and tax character are **not** fitted — they are facts
about specific funds, stated in config.

## Gaps

Every one of these is a reason a number out of this model can be wrong. They are ordered by
how much they move an allocation answer.

1. **No regime switching, and the rate means are barely identified.** The joint VAR is a
   single linear process: 2009–2021 ZIRP and 1981 differ only by draw, not by regime. Its
   30-year inflation band (×2.11–4.78 simulated against ×1.95–4.85 realized) now matches
   history, but that is a stationary process reproducing a dispersion, not a model that knows
   regimes exist. It also slightly OVERSHOOTS the realized 30-year spread (0.41 against 0.31),
   though the realized figure comes from ~2.6 independent windows and is itself uncertain.

2. **Equity and rates are independent.** `rate_beta` fits to **+1.57 (R² = 0.0041)** over
   1993–2026 and **−0.62 (R² = 0.0051)** over 1980–2026: the sign is not stable across windows
   and neither explains half a percent of variance. So it is zero and the model carries no
   bond/equity coupling at all. **A question that turns on bond/equity correlation is not
   answered here** — which is exactly what a 60/40 study turns on.

   _How much it can move a 30-year answer, since "largest gap" invites the reader to assume it
   explains any disagreement._ Over 360 months the equity log-sd is `0.05290 × √360 = 1.004`
   and the CPI log-sd is ≈0.409 (from the ×2.11–4.78 band in gap 1) — so **CPI carries only
   ~14% of real-equity log-variance**. Propagating a correlation through
   `sd² = sd_eq² + sd_cpi² − 2ρ·sd_eq·sd_cpi` gives a 100%-equity 30-year real p5 of 1.02× at
   ρ = 0, 1.16× at ρ = +0.2, and 2.28× at the impossible ρ = +1. The historical replay puts
   that same percentile at **4.07×**, so coupling cannot close that gap: reaching 4.07× needs a
   30-year variance ratio of 0.059, which is a statement about **mean reversion** (gap 3), not
   about correlation. Two consequences worth stating plainly: the replay's 4.07× is not a p5 —
   it is the 42nd-worst of 839 overlapping windows off ~3.3 independent ones (gap 4) — and for
   a question about how deep a fixed-income floor should be, gaps 3, 9 and 10 each move the
   answer more than this one does.

3. **Equity is now fitted on the full century, and the window still matters.** The shipped
   default is the CRSP total market over 1926-07–2026-06: **10.37%/yr at 18.3% vol**. The same
   series over 1980–2026 gives 12.29% at 15.7%, so restricting to the recent past would raise
   the drift ~2pp and cut the volatility ~2.5pp. Nothing forces a century — it is a choice,
   and the honest reason for it is that a 30-year horizon should be priced against a record
   containing more than one regime.

   _Correction:_ an earlier revision claimed a 4pp window effect, measured as MITTX (1973–)
   against VFINX (1980–). That was wrong. MITTX returns 7.17%/yr while its own market returns
   11.34% over the **identical** window — the gap was manager drag or an adjusted-close defect,
   not history. Use `FRENCH_FACTORS`; MITTX stays in the evidence set only as a cross-check.

4. **The replay's record now reaches 1926, but the sample is still tiny.** `load_macro_history`
   assembles it from four series: Ken French's factors (CRSP total market AND the one-month
   T-bill, so equity and the short rate are aligned by construction), `LTGOVTBD` spliced under
   `GS10` for the long rate, and `CPIAUCNS` for prices. That is **1200 months — 840 overlapping
   30-year windows and ~3.3 independent ones**, against 199 and ~1.5 before.

   Two costs come with it. The spliced long rate carries an unquantified duration-mismatch
   offset before 1953 (`splice_at_seam`), and the record now spans regimes — a gold standard,
   no Fed, WWII rate pegs — whose relevance to 2026 is a judgement, not a data question. And
   even at 1871 the ceiling is ~5 independent windows: history cannot supply more, which is why
   a fitted model belongs beside the replay rather than instead of it.

5. **No cyclical credit spread.** A muni's spread over the curve is a constant, so the model
   cannot produce a muni selloff that Treasuries escape — which is exactly what a credit event
   looks like, and exactly when a floor is tested.
6. **Mismatched inflation and equity windows** — still true, but it now runs the other way, and
   the old text here described the pre-refit state. Equity is fitted on 1926-07–2026-06 (1200
   months, gap 3); the joint VAR inner-joins `FEDFUNDS`/`GS10`/`CPIAUCSL` and so starts at
   1955-08 (850 months). The implied real equity return therefore pairs a sample containing the
   1930s with one that does not. That is the deliberate consequence of the marginals-keep-their-
   own-history rule above, not an oversight — but it means the real figure is a quotient of two
   different centuries and should not be read as a fitted quantity.
7. **The rate means are barely identified.** OLS on a near-unit-root series biases mean
   reversion upward and pins the long-run mean weakly: the same fit gives a 4.93% short-rate
   mean over 1954–2026 and 1.71% over 1990–2026. Read the sigmas; sweep the means.
8. **The curve is clamped flat past 10 years.** `_instrument_yield` is
   `short_rate + min(duration/10, 1) * term_spread + spread`, so it interpolates the front and
   then STOPS: a 30-year bond is priced at exactly the 10-year yield. It orders cash, a short
   fund and an intermediate fund correctly, and it cannot price a barbell against a bullet — but
   the flat long end is the larger defect, because it is where a real ladder lives. Against the
   2026-07-30 real curve (10y 2.41%, 30y 2.98%) the missing 57bp is **−15.3% on a 30-year zero**,
   and it overstates a 30-year full-burn ladder's cost by about **$349k**.

   Fixing it is cheaper than it looks and is the keystone for three separate gaps. A Gaussian
   VAR(1) admits an affine term structure, `D(t, τ) = exp(A_τ + B_τᵀ x_t)`, whose `A` and `B`
   solve a linear recursion in the fitted parameters and are therefore COMPILE-TIME constants —
   `(n_tenors,)` and `(n_tenors, 3)`. The whole curve at every tenor and month is then a matmul
   against the `(R, H, 3)` state path that already exists: no new emitted series, no new
   stochastic dimensions, and cross-tenor consistency by construction rather than by
   interpolation. The REAL curve is the same recursion with `r − π` as the short rate, since
   that is another linear function of the same state; the breakeven falls out as the difference
   and is checkable against `T10YIE`.

   _Subtlety worth stating before anyone implements it:_ with zero risk premia the recursion
   prices the expectations-hypothesis curve, which undershoots long yields — while `term_spread`
   is fitted to realized `GS10 − FEDFUNDS` and so already embeds the historical term premium. A
   naive no-arbitrage recursion would therefore contradict the state it is built from. Either fit
   market prices of risk so the model's own 10-year reproduces the `term_spread` state (correct,
   and weakly identified off 850 months), or treat it as a fitted factor curve — Nelson–Siegel
   loadings with a fitted decay, so the long end is extrapolated by a shape fitted to data rather
   than clamped. The second is the smaller change and does not claim rigor this state vector
   cannot support.

9. **No equity distribution.** `IncomeCategory` has no qualified-dividend rate, so an equity
   dividend routed through the interest path would be overtaxed as ordinary income. Equity
   emits a total-return price and no payout — consistent, but it means dividend TIMING and its
   tax treatment are absent.
10. **No real yield, so a ladder cannot roll.** A ladder held from scenario start IS
    representable — the sim's `BondHolding(inflation_indexed=True)` needs only the CPI path,
    which this model emits, and it carries no duration risk because it is never marked. What is
    missing is buying a rung MID-HORIZON, which requires the real yield prevailing in that month,
    and this model emits no yield of any kind (it emits prices and distributions; see gap 8 for
    the curve it does carry internally).

    That bites harder than it sounds, because of a fact about the instrument rather than the
    model: **TIPS are issued in 5-, 10- and 30-year terms only**, so roughly 30 years is the
    longest real floor anyone can contract for. Any study whose horizon exceeds that is exactly
    the case where the ladder must be rolled at an unknown future real yield — and here it never
    is. The direction of the resulting bias is worth stating plainly: a model that buys a ladder
    once and holds it never samples a bad roll, so it **understates the risk of deferring**
    purchases and is biased TOWARD shallow ladders.

    Closing it needs two things, neither of which requires a refit: the real curve from gap 8,
    which is where the per-tenor real yield comes from and is shared with bond mark-to-market;
    and a mid-horizon bond purchase on the sim side, whose trigger belongs in an actor policy
    rather than a schedule. Note the ordering — the curve is the smaller piece and comes first,
    since without it a 30-year rung is indistinguishable from a 10-year one and the study's long
    end is mispriced whether or not the ladder rolls.

11. **Nothing is regime-switching.** Rates mean-revert around one level with one volatility.
    The 2009–2021 ZIRP era and 1981 are the same process here, differing only by draw.
12. **No housing, no private equity, no crypto.** By design — compose with another provider
    through `CompositeModel`. Listed so the absence is a statement rather than an oversight.

## Not fittable here

The provider implements `Sampler` only — not `Fittable`, not `Scorable`. Its config defaults
are produced offline by `bb run //finance/augur/fit:train -- --model structural_macro` into
the checked-in `fit/calibrated/trained_structural_macro.yaml` (fitted values plus the window/
sample-count provenance in the table above, as data rather than a comment), deliberately: the
fit does not go through the joint fit's single aligned window, and that separation is what
buys the long histories in the table above.
