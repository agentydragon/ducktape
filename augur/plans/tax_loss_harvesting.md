# Tax-loss harvesting (TLH) in Augur

Status: 2026-06-04. Piece 1 (capital-loss netting + carryforward) in progress;
Piece 2 (harvest process) designed, not started.

## Motivation

A tax-loss-harvesting direct-indexing account (the deployment's Wealthfront
"S&P 500 Direct Portfolio") tracks an index on a pre-tax basis but continually
realizes capital losses by selling individual constituents that have fallen
below basis and rebuying correlated replacements. Those realized losses offset
gains elsewhere, shelter up to $3,000/yr of ordinary income, and carry forward.
This "tax alpha" is the entire economic reason to hold direct indexing over an
index ETF, and Augur currently models **none** of it.

Ground-truth evidence (private, see `gaffer-private/gaffer_augur/wealthfront/`):
TY2025 1099-B for the live account shows ~$73k net realized loss, ~5%/yr of
portfolio value, **essentially all short-term**, with **zero wash sales**.

## What already shipped

`PlaidSp500ProxyGroupConfig.holding_period_buckets` (ducktape #1839) splits the
Plaid-fed aggregate sleeve into holding-period bands with independent value and
basis fractions. This fixed the **static** picture — liquidation/sale tax now
realizes a realistic short-/long-term mix instead of treating the whole sleeve
as one long-term lot. It does **not** model ongoing harvesting.

## The modeling spectrum (and what to avoid)

Generating harvestable losses requires cross-sectional dispersion among the
underlying names. A single SP500 series has none, so options range from cheap to
expensive:

1. **Single index series (today).** Zero dispersion → zero harvest. Wrong.
2. **Reduced-form harvest yield (chosen).** Do not simulate constituents. Emit a
   realized loss each period as a calibrated function of the index path Augur
   already samples. One series in, a loss number out.
3. **Few-sleeve lightweight dispersion.** ~5–10 representative sleeves =
   index factor + scaled idiosyncratic noise; run real FIFO harvesting on them.
   Emergent harvesting, still inside the existing lot machinery.
4. **Full factor model.** Hundreds of names with a covariance/factor structure,
   per-name idiosyncratic vol, rebalancing, wash-sale tracking. **Avoid.** This
   is whole-market modeling: it forces a correlation structure into Augur's
   exogenous engine (which samples series independently today) and a large
   surface of *unobservable* parameters to reproduce a quantity we can measure
   directly from the 1099-B. Mechanistic fidelity we cannot calibrate is worse
   than a simple form we can.

Decision: build #2 now. Keep #3 as the documented upgrade path if a decision
ever turns on harvesting behavior in a regime unlike the calibration window.
Never build #4.

## Piece 1 — capital-loss netting + carryforward (prerequisite, not TLH-specific)

Today the bracket walks (`augur/sim/tax.py`) clamp negative taxable amounts to
zero and no cross-year capital state exists, so any realized loss simply
evaporates. Standard §1211/§1212 mechanics are required before harvesting can
mean anything — and they also fix correctness for existing OpenAI/IBKR sales:

- Net realized losses against realized gains within ST and LT, then across the
  two categories (net ST loss offsets net LT gain and vice versa).
- Apply up to **$3,000/yr** of net capital loss against ordinary income.
- Carry the remainder forward as a per-`(rollout, agent)` scalar that reduces
  next years' net capital gain (and, while positive, keeps feeding the $3k
  ordinary offset).

This is bookkeeping over the realized-gain frames the sim already produces —
vectorized scalars per `(rollout, year)`, no market modeling. It is independently
useful and lands first.

## Piece 2 — reduced-form harvest process (the actual TLH)

Attach an optional harvest process to a holding. Each tax period:

- **Driver:** harvest yield is a function of the period's index return
  (drawdowns harvest more) and the position's **embedded-gain ratio**
  (basis/MV — as it rises, fewer lots sit below basis, so yield decays). 2–3
  params; optionally realized volatility.
- **Form:** `gross_harvest_t ≈ MV_t · g(return_t, embedded_gain_ratio_t)`, with
  `g` rising in drawdowns and decaying toward a floor as embedded gain grows.
  Split output into ST/LT (mostly ST early; seed from the holding-period
  buckets).
- **Basis feedback (the one real state element):** harvesting realizes a loss
  *and* resets that slice's basis to current market (sell-underwater + rebuy).
  So the process (a) books the loss into Piece 1, and (b) lowers the position's
  tracked cost basis, which raises the embedded-gain ratio and **decays future
  yield**. A scalar basis update per position — no per-stock state.
- **No wash-sale gate needed:** TY2025 had zero wash sales (replacement-buying
  works), so gross index exposure stays continuous and the realized-loss output
  is unaffected.

### Calibration

- Level + ST/LT mix anchored to the TY2025 1099-B (~5%/yr net, all ST).
- The **decay rate** (yield vs account maturity) is the one genuinely unknown
  parameter. Needs prior-year 1099-Bs (TY2022–24) to fit. Until then, mark it
  `[HEURISTIC]` and keep it conservative — a flat 5%/yr extrapolated 10 years
  would massively overstate the benefit.
- Generic process + calibration *schema* live in ducktape; the account's fitted
  numbers live in `gaffer-private/gaffer_augur/wealthfront/`.

## Open inputs

- Prior-year 1099-Bs (TY2022–24) for the decay term.
- Confirmation the sleeve tracks the S&P 500 and dividend-reinvestment handling.
