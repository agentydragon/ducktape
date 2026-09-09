# Product metrics from the engine

The product API renders every projection from ten metric series. Seven are **base** series
the simulator emits directly; three are sums of those. This document covers what the engine
owes the product read model, including the observation boundary when execution stops.

## The split

The engine emits only the seven base series plus the per-rollout failure month. Everything
above that — the derived metrics, the terminal reduction, the percentile brackets, the
interpolation — is `sim/metric_composition.py` and `sim/quantiles.py`, reached
through the backend-neutral `Engine` contract rather than from this package.

The split is what the contract is for: an engine owes integers, and the read model owes
every reduction over them. Adding a base metric means touching `BASE_METRIC_NAMES` and
`rust/product.rs`. Adding a _derived_ metric means touching `compose_metric` alone.

## Why the metrics are not read out of dense output

`simulate_product_metrics` runs under `CaptureMode::Summary`: no monthly snapshot, no
journal, no event trace. The percentile fan is the 100,000-rollout workload, and it needs
`snapshots × rollouts` integers per metric, not a dense output tree.

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
  retaining 1,000 cash. Another source-account group can still pay its consumption.

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

`sim/compiler/execution.py` prepares the integer execution input directly from the
scenario and supplied paths. The backend only transports it:

- security prices, home values and PE marks cross as integer currency quanta;
- distribution rates retain sub-quantum precision until multiplied by holdings;
- index series (inflation, rent) arrive as float64 and are quantized to parts per billion
  before they ever multiply money, so the engine divides an exact rational rather than
  rounding a product of floats.

No engine arithmetic reads an index level raw. Coupon and closing-cost rates likewise
cross as integer parts per billion before multiplying money.
