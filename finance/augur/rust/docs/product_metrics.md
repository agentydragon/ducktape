# Product metrics from the engine

The product API renders every projection from ten metric series. Seven are **base** series
the simulator emits directly; three are sums of those. This document covers what the engine
owes the product read model, and two behaviours it keeps deliberately rather than corrects.

## The split

The engine emits only the seven base series plus the per-rollout failure month. Everything
above that — the derived metrics, the terminal reduction, the percentile brackets, the
interpolation — is `product/metric_composition.py` and `product/quantiles.py`, reached
through the backend-neutral `Engine` contract rather than from this package.

The split is what the contract is for: an engine owes integers, and the read model owes
every reduction over them. Adding a base metric means touching `BASE_METRIC_NAMES` and
`rust/product.rs`. Adding a _derived_ metric means touching `compose_metric` alone.

## Why the metrics are not read out of dense output

`simulate_product_metrics` runs under `CaptureMode::Summary`: no monthly snapshot, no
journal, no event trace. The percentile fan is the 100,000-rollout workload, and it needs
`snapshots × rollouts` integers per metric, not a dense output tree.

## Failed rollouts

A frozen rollout zeroes its dollar-valued state, so cash, holdings, private equity and
mortgage principal all go to zero from the failure month on. Bonds are zeroed explicitly,
because a bond's face is a static input the freeze never touches.

**Property value is not zeroed.** A property's metric value is
`purchase_price × home_value[now] / home_value[purchase_month]` — both terms are static or
exogenous, and the property's active flag survives the freeze, so a failed rollout reports
its property value while every other term reads zero. `net_worth_quanta` for a failed
rollout is therefore that property value rather than zero.

This looks like an oversight rather than an intended rule: the bond term carries a comment
explaining that it is zeroed "so a failed rollout's net worth is zero like every other
term", which is exactly what the property term then breaks. It is recorded rather than
fixed because fixing it changes what the product reports for failed rollouts, which is a
product decision and not a cleanup.

## Two base months for one property

The product metric escalates a property's price from the home-value level **at its purchase
month** (`product.rs`). The property _sale_ path escalates from the level at **month 0**
(`engine/property.rs`). They are two different answers to "what is this property worth",
and a property bought mid-horizon is valued on one basis in the metric series and another
at sale.

Nothing here reconciles them. It is written down because the two sites are far apart and
each reads correct on its own.

## What the encoder has to preserve

`rust/backend.py` takes an integer fixture, and `rust/fixture_encoder.py` builds one from a
`Scenario` and its `CompiledSimulation`. The fixture's series are exact integers while the
sampled series are float64, so the encoder's whole job is where those floats are read:

- money series (security prices, distributions, home values, PE marks) are already integer
  quanta in `CompiledSimulation.external_money_values`, so they transfer exactly;
- index series (inflation, rent) arrive as float64 and are quantized to parts per billion
  before they ever multiply money, so the engine divides an exact rational rather than
  rounding a product of floats.

No engine arithmetic reads an index level raw. Rates follow the same rule, with two that
reach the fixture by a different route and are checked rather than assumed: a bond's
coupon, which the compiler reads as the exact rational `Fraction(str(rate))`, and a
property sale's closing cost, which the fixture spells in basis points. Both refuse a value
whose two routes disagree.
