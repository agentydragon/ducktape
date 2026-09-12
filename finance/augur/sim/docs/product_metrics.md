# Product metrics from the engine

The product API renders every projection from ten metric series. Seven are **base** series
the simulator emits directly; three are sums of those. This document covers what the engine
owes the product read model, including the observation boundary when execution stops.

## The split

The configured product adapter calls the Python `sim/configured.py` loop, whose
Python financial steps capture base series and the per-rollout failure month. The common
action result instead supplies scoped numeric histories to
`product/action_projection.py`. Both use `sim/metric_composition.py` and
`sim/quantiles.py` for derived metrics, terminal reductions and percentile interpolation.

Valuation remains canonical; neither adapter reconstructs trades, tax or basis.
Derived metrics use `compose_metric`; numeric histories describe only observations
actually reached by their original rollout IDs.

## Why the metrics are not read out of dense output

`sim/configured.py::simulate_product_metrics` requests summary capture: no monthly
book, journal or event trace. Common-action compact results likewise retain scoped
numeric histories without dense books. Selected dense/forensic captures retain
canonical events; only forensic capture includes the journal.

Opaque TLH value appears once in holdings wealth. Its contribution, redemption,
modeled-realization and distribution records are `tlh_financial_effects`, not
public-lot dispositions. They retain signed cash, ST/LT gains and basis changes
for the product timeline, including zero-cash liquidation. Tax consequences
still come from canonical household tax records.

## Failed rollouts

Failure stops execution, not ownership. Cash, lots, debts, tax balances and the other
books retain their actual values. A failure in event month `f` produces a final post-event
snapshot `f+1`, valued with the already-observed marks from month `f`. It has not reached
the next scheduled market observation. Forensic output ends at that snapshot.

Compact integer blocks remain rectangular. `failed_month == -1` means completed;
otherwise `ProductMetricArrays.observed` identifies snapshots through `f+1`, including the
stopped book. Later integer slots are transport padding, not observed zero money.
`scheduled_observed` includes snapshots only through `f`, so a wealth fan at scheduled
month `f+1` does not mix live marks with a stopped book marked at `f`. Historical monthly
quantiles use that month's observed population and report its count, not only eventual
survivors. No observations means a null quantile.

Aggregate outcomes declare their `OutcomeBasis`:

- `completed_horizon`: wealth is a horizon-terminal observation only for paths that
  completed the horizon, even if a stop snapshot's index equals the horizon. Stopped
  samples are null; the selected rollout instead exposes `ending_metrics` with its
  snapshot and failed event-month indices.
- `observed_through_stop`: shortfall sums recorded unpaid demands through stop or
  completion for every path. Monthly shortfall includes the failure-event amount at
  snapshot `f+1`. It is amount due minus actually paid, including tax and contract
  demands—not additional cash required to make a funding group payable, and not a
  projection of future shortfalls. An all-or-none group can leave 1,100 unpaid while
  retaining 1,000 cash. Another source-account group can still pay its claims.
  Common-action reporting also includes the valid attempted consumption gap;
  rejected discretionary consumption is not a new incurred liability.

This basis governs the aggregate population; each monthly fan has its own observation
count. Currency quantiles remain exact integers, with null absence at the product/API
boundary. The selected chart places a stopped book at event month `f`, retaining snapshot
`f+1` as book identity rather than pretending it observed another month of prices.

## Property valuation

`property::Valuation` supplies both the reported gross property value and the sale's
pre-cost market value: nominal purchase price multiplied by the home-value level at
the explicit valuation month, divided by its level at the purchase month. Pre-purchase
index changes do not appreciate a price agreed at acquisition. Stopped books use the
failure month's mark, not the next snapshot's mark.

Seller closing costs, mortgage payoff and tax basis are separate calculations. The
sale outcome's `gross_proceeds` is already **after seller closing costs**, before
mortgage payoff; it equals the same-time reported property value only when those costs
are zero. Buyer closing costs and later capital improvements affect book/tax basis
under their own rules, not this purchase-price-index valuation.

## What the encoder has to preserve

`sim/compiler/execution.py` prepares typed integer facts directly from the
scenario and supplied paths. Native serialization is private to the boundary:

- security prices, home values and PE marks cross as integer currency quanta;
- distribution rates retain sub-quantum precision until multiplied by holdings;
- index series (inflation, rent) arrive as float64 and are quantized to parts per billion
  before they ever multiply money, so the engine divides an exact rational rather than
  rounding a product of floats.

No engine arithmetic reads an index level raw. Coupon and closing-cost rates likewise
cross as integer parts per billion before multiplying money.
