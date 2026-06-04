# Tax-loss harvesting (TLH) in Augur

Status: 2026-06-04. **Pieces 1 & 2 shipped.** Piece 1 (capital-loss netting +
carryforward, ducktape #1846). Piece 2 (reduced-form harvest, ducktape #1881):
yield core `augur/sim/tlh_harvest.py` → `reduced_form_tlh` config (`tlh_model`) →
`Scenario.harvest_policies` → compiled table (`augur/sim/compiler/tlh_harvest.py`) →
engine phase `_apply_tlh_harvest` + sale-time basis give-back in `_record_capital_gains`
(`augur/sim/engine/phases.py`), threaded through the product path; the per-(policy,
rollout) `tlh_cumulative_harvest` scalar lives in `augur/sim/buffers.py`. The basis-reset
design fork below was resolved as option **(B)** (scalar, not extra lot slots). The live
Wealthfront sleeve is wired in gaffer-private (#244). Remaining follow-ups now tracked in
<../sim/TODO.md>.

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

**The account was opened in 2025, so TY2025 is its first year and the only
1099-B that exists** — there are no prior-year forms, and none ever will be.
This matters for calibration: a first-year direct-indexing account harvests at
or near its **maximum** rate (every lot was bought recently, so dispersion below
basis is widest and embedded gains are smallest). The ~5%/yr all-short-term
figure is therefore a _first-year peak_, not a steady-state rate, and we have no
in-account history to observe how it decays as the sleeve matures.

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
   surface of _unobservable_ parameters to reproduce a quantity we can measure
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
  (drawdowns harvest more) and the position's **embedded-gain fraction**
  `e = clip((MV − basis)/MV, 0, 1)`. A fresh cash-funded account has `e ≈ 0`
  (basis = market value) and harvests near its peak; as winners appreciate and
  losers get reset away, the position is increasingly dominated by low-basis
  winners, `e → 1`, and yield decays toward a floor ("ossification",
  [VANGUARD-2024]). 2–3 params; optionally realized volatility.
- **Form:** `gross_harvest_t ≈ MV_t · g(return_t, e_t)`, with `g` rising in
  drawdowns and decaying toward a floor as `e` grows. Split output into ST/LT
  (mostly ST early; seed from the holding-period buckets).
- **Basis feedback (the one real state element):** harvesting realizes a loss
  _and_ resets that slice's basis to current market (sell-underwater + rebuy).
  So the process (a) books the loss into Piece 1, and (b) lowers the position's
  tracked cost basis, which raises the embedded-gain fraction `e` and **decays
  future yield**. A scalar basis update per position — no per-stock state.
- **No wash-sale gate needed:** TY2025 had zero wash sales (replacement-buying
  works), so gross index exposure stays continuous and the realized-loss output
  is unaffected.

### Calibration

- Level + ST/LT mix anchored to the TY2025 1099-B (~5%/yr net, all ST), read as
  the **first-year peak** (see Motivation).
- The **decay rate** (yield vs account maturity) is the one genuinely unknown
  parameter, and **cannot be fit from this account**: there are no prior-year
  1099-Bs (the account opened in 2025), and TY2025 is a single point at the
  high end of the curve. A flat 5%/yr extrapolated 10 years would massively
  overstate the benefit. Until we have a second point:
  - Anchor the curve's _shape_ to external direct-indexing TLH research
    ([VANGUARD-2024], [CBL-2020] — see References). The benefit is **front-loaded**:
    a cash-funded account starts with cost basis = market value (maximal harvest),
    and as winners appreciate, dispersion increasingly shows up as _unrealized
    gains_ rather than harvestable losses ("ossification"), so yield decays toward
    a low floor. Mark the fitted decay `[HEURISTIC]` and keep it conservative.
  - Note that Piece 2's basis-feedback mechanism already produces decay
    _endogenously_ (harvesting resets basis → embedded-gain ratio rises → yield
    falls); the decay **rate** param only sets how fast. So the structural
    shape is right even before the rate is pinned.
  - **Re-fit annually.** Each new tax year (TY2026, TY2027, …) adds one maturity
    point; the first year-over-year decline is the first directly observed
    decay signal. Once ≥2 forms exist, replace the heuristic with a fitted rate.
- Generic process + calibration _schema_ live in ducktape; the account's fitted
  numbers live in `gaffer-private/gaffer_augur/wealthfront/`.

### Implementation proposal

How Piece 2 lands in the sim, grounded in the current engine. Built in two steps
so the calibratable math lands and is unit-tested before any engine surgery.

**Step 2a — the harvest-yield core (this PR).** A pure, vectorized
`augur/sim/tlh_harvest.py`: a Pydantic `HarvestYieldParams` (peak/floor annual
yield, maturity-decay exponent, drawdown sensitivity) and
`monthly_harvest_fraction(period_return, embedded_gain_fraction, params) -> (R,)`
plus an ST/LT splitter seeded from holding-period buckets. It encodes the
[VANGUARD-2024] front-loaded shape: peak at `e = 0`, decaying as `(1 − e)^γ`
toward a floor, scaled up in drawdowns. All params `[HEURISTIC]` with the
external anchor cited in-module. No engine dependency; fully unit-tested for the
monotonicity/bound/decay invariants. The function returns a _fraction of MV_; the
caller clamps it to the loss actually available below basis.

**Step 2b — engine integration (follow-up PR).** Wire the core into the sim:

- **Config:** add `harvest: HarvestProcessConfig | None = None` to
  `PlaidSp500ProxyGroupConfig` (`augur/api/portfolio_source_config.py`) — the
  proxy sleeve already carries `holding_period_buckets`, the natural ST/LT seed.
- **Plan:** compile a `harvest_policies` table + `holding → policy` map in
  `augur/sim/compiler/plan.py` (mirror the `pe_policies` / `pe_issuers` tables).
- **Phase:** add `_apply_harvest` to the monthly step in
  `augur/sim/engine/__init__.py`, slotted **before** `_apply_pe_tenders`. It reads
  the index return from `plan.external_values[series_index, :, month]`, computes
  per-rollout MV/basis → `e`, calls the Step-2a core, clamps to available loss,
  injects the realized loss into `current.capital_gain_ytd[profile, ST|LT, :]`
  (Piece 1's netting then handles it unchanged), and applies the **basis reset**.
  Model the whole phase on the existing `_apply_pe_tenders` template.
- **Basis-reset mechanism — the one open design fork.** Lots are immutable in the
  compiled plan (`lot_cost_basis_per_unit`, `lot_purchase_month` are fixed
  arrays), but harvesting must lower a slice's basis to current market. Two
  options: **(A)** pre-allocate spare "rebuy" lot slots per harvesting holding and
  activate them at harvest time (basis = current price, purchase_month = now);
  **(B)** keep a separate per-(holding, rollout) scalar `harvested_basis_delta`
  outside the lot array and fold it into MV/basis and final-sale gain math.
  Recommend **(B)**: no plan-size blowup, no dynamic lot allocation in the
  vectorized loop, and it matches the plan's "scalar basis update per position —
  no per-stock state" intent. (A) is closer to literal sell+rebuy but bloats the
  lot dimension `L` for every harvesting account.
- **Event logging:** add a `harvest` disposition kind alongside the scheduled /
  liquidity / pe dispositions for audit + frontend.

## Open inputs

- An external prior for the **decay-curve shape** (direct-indexing TLH
  whitepapers / academic studies). No prior-year 1099-Bs exist — the account
  opened in 2025 — so the decay term starts `[HEURISTIC]`, not fitted.
- Future years' 1099-Bs (TY2026+) to fit decay empirically as they arrive; each
  adds one maturity point, and decay stays `[HEURISTIC]` until ≥2 exist.
- Confirmation the sleeve tracks the S&P 500 and dividend-reinvestment handling.

## References

- **[CBL-2020]** Chaudhuri, S. E., Burnham, T. C., & Lo, A. W. (2020). "An
  Empirical Evaluation of Tax-Loss-Harvesting Alpha." _Financial Analysts
  Journal_, 76(3), 99–108. <https://doi.org/10.1080/0015198X.2020.1760064>
  (open copy: <https://dspace.mit.edu/handle/1721.1/135992>). Establishes the
  magnitude and existence of TLH alpha: ~108 bps/yr before transaction costs over
  1926–2018 on the 500 largest-cap names, falling to **82 bps/yr under the
  wash-sale rule** and ~95 bps after costs. Our account had **zero wash sales**,
  so it sits near the unconstrained end; the figure also bounds the plausible
  steady-state benefit (well below the ~5%/yr first-year harvest _rate_, which is
  a gross-loss figure, not after-tax alpha).
- **[VANGUARD-2024]** Vanguard (July 2024). "Tax-loss harvesting: Why a
  personalized approach is important."
  <https://corporate.vanguard.com/content/dam/corp/research/pdf/tax_loss_harvesting_why_a_personalized_approach_is_important.pdf>
  Documents the **front-loaded** profile this model anchors to: a cash-funded
  account starts with cost basis = market value (maximal early harvest), and as
  holdings appreciate, dispersion increasingly surfaces as _unrealized gains_
  rather than harvestable losses ("ossification"), so harvest yield decays over
  the account's life. This is the source for the `(1 − e)^γ` decay shape.
