# Product metrics from the Rust engine

The product API renders every projection from ten metric series. Seven are **base** series
the simulator emits directly; three are sums of those. This document covers what the Rust
engine has to reproduce for the product read model to accept it, and the two places where
matching JAX meant matching something questionable.

## The split

Rust emits only the seven base series plus the per-rollout failure month. Everything above
that — the derived metrics, the terminal reduction, the percentile brackets, the
interpolation — is `product/metric_composition.py` and `product/quantiles.py`, which both
backends call. A fan produced through `rust/backend.py` is therefore identical to a JAX fan
because it is the same reduction over the same integers, not because two implementations
were checked against each other.

Adding a base metric means touching `BASE_METRIC_NAMES`, the JAX reducer, and
`rust/product.rs`. Adding a _derived_ metric means touching `compose_metric` alone.

## Why the metrics are not read out of dense output

`simulate_product_metrics` runs under `CaptureMode::Summary`: no monthly snapshot, no
journal, no event trace. The percentile fan is the 100,000-rollout workload, and it needs
`snapshots × rollouts` integers per metric, not a dense output tree. This mirrors JAX's own
`emit_dense=False` product path.

## Failed rollouts

A frozen rollout zeroes its dollar-valued state, so cash, holdings, private equity and
mortgage principal all go to zero from the failure month on. Bonds are zeroed explicitly,
because a bond's face is a static input the freeze never touches.

**Property value is not zeroed**, and the Rust engine reproduces that. A property's metric
value is `purchase_price × home_value[now] / home_value[purchase_month]` — both terms are
static or exogenous, and the property's active flag survives the freeze, so a failed
rollout reports its property value while every other term reads zero. `net_worth_quanta`
for a failed rollout is therefore that property value rather than zero.

This is JAX's behavior, and the Rust engine matches it deliberately so the two are
substitutable. It looks like an oversight in the JAX reducer rather than an intended rule —
the bond term carries a comment explaining that it is zeroed "so a failed rollout's net
worth is zero like every other term", which is exactly what the property term then breaks.
Fixing it means changing both engines together, and changing what the product reports for
failed rollouts. `test_rust_and_jax_match_product_metrics_across_a_rollout_failure` pins the
current behavior either way.

## Two base months for one property

The product metric escalates a property's price from the home-value level **at its purchase
month**. The property _sale_ path escalates from the level at **month 0**. Both engines
agree on both, because Rust matched each path separately — but they are two different
answers to "what is this property worth", and a property bought mid-horizon is valued on
one basis in the metric series and another at sale.

Nothing here changes that; it is recorded because the differential suite would otherwise
look like it had blessed the pair as consistent.

## What the encoder has to preserve

`rust/backend.py` takes an integer fixture, and `rust/fixture_encoder.py` builds one from a
`Scenario` and its `CompiledSimulation`. The fixture's series are exact integers while the
product's sampled series are float64, so the encoder's whole job is where those floats are
read:

- money series (security prices, distributions, home values, PE marks) are already integer
  quanta in `CompiledSimulation.external_money_values`, so they transfer exactly;
- index series (inflation, rent) arrive as float64, and every site that turns one into money
  quantizes it to parts per billion first (`_scale_money_by_float_ratio`, `_scale_money`),
  so pre-quantizing to PPB in the encoder hands Rust the integers JAX would have formed.

No engine arithmetic reads an index level raw: the TLH harvest curve was the last one, and it
now evaluates in integers on both sides (`sim/tlh_harvest.py`, `rust/engine/tlh.rs`).

Rates are the same story with one wrinkle. Where JAX also quantizes with
`_round_int64(rate * 1e9)` the encoder's PPB integer is that same number by construction. Two
rates reach their engine by a different route and are checked rather than assumed: a bond's
coupon, which the compiler reads as the exact rational `Fraction(str(rate))`, and a property
sale's closing cost, which the fixture spells in basis points. Both refuse a value whose two
routes disagree.

## Test cost

The differential suites' whole wall clock is XLA compilation: JAX bakes the plan structure
into the compiled program, and for the product cases the selected agent too, so each case
compiles the 60-month scenario afresh. The persistent JAX cache is opt-in via
`AUGUR_JAX_COMPILATION_CACHE_DIR`, which a hermetic Bazel test never sets, so nothing is
reused across runs either.

As one target the suite ran ~400s and then began exhausting the runner's memory, because
every compiled executable stays resident in the one process. It is split by domain instead
— one target per policy family, and separately for each product read-model concern — which
both parallelizes the compiles and caps each process's resident set. Measured cold: 24-140s
for every target except `product_metrics_differential_test` at 308s, which compiles once per
agent in `PRODUCT_METRIC_AGENTS`.

Bazel's own `shard_count` would be the better lever, and `pytest_bazel` already translates
Bazel's shard environment into `--shard-id`/`--num-shards`. Those flags come from the
`pytest-shard` plugin, which this repo's dependency set does not carry, so sharding fails
collection outright. Adding it would speed up every slow `py_test` here, not just these.
