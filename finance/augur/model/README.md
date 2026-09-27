# Market paths and product construction

Load/select historical windows or sample structural paths, then choose products:

```python
from finance.augur.model.bond_fund import BondFundSpec
from finance.augur.model.product_paths import construct_products

market = historical.market_paths(window_starts=chosen_dates, horizon_months=360)
# Or: market = structural.sample_market(request)
for maturity in (2.0, 8.0):
    products = construct_products(
        market,
        equity=None,
        instruments=(BondFundSpec(symbol="test_fund", maturity_years=maturity),),
    )
```

`market_paths.py` defines the concrete rate/spread, CPI and optional equity-index
inputs used here. Corporate yields are explicit available observations, not a
Treasury-spread fallback. `product_paths.py` constructs the existing bond-fund and
total-return equity proxies without loading, sampling or tax settlement. This is
not a universal product/model interface. The configured providers still compose
both steps for callers wanting their configured product bundle.

Preserving existing outputs isolates this boundary change; it does not make the
current approximations a correctness oracle or a compatibility requirement.
Financial corrections can land independently with independently justified tests.

## Plausibility gate

Check market paths against sourced bands before any answer is built on them:

```python
from finance.augur.model.plausibility import BandFile, evaluate, require_plausible

result = evaluate(market, BandFile.from_yaml(band_path))
print(result)  # every band: value, limits, verdict, breach direction
require_plausible(result)  # RefusalError unless every REFUSE band was checked and held
```

`plausibility.py` defines the band-file schema and the verdicts. Sample at least
`BandFile.horizon_months`. An `Override` naming refused bands, with a reason, lets
`require_plausible` pass and logs a warning for each band it accepts.

Fitted VECM and state-space inputs carry dated `ExogenousObservedPoint` records
with actual source units. Evidence loaders choose each factor's anchor explicitly;
runtime providers never search source-name fallbacks. Auxiliary observations and
return/calibration provenance are separate from those anchors. Monthly observations
use the month's first day as their existing period label, not a claimed daily quote.
State-space conditioning must use the fitted factor's units; an index-point anchor
is not silently replaced by a dollar value. These input records do not change the
models' statistical dynamics or resolve product price/payout semantics.

Offline checks:

```bash
bbr test //finance/augur/model:test_market_paths //finance/augur/model:test_product_paths
```

The [PE model conventions](../docs/private_equity_model.md) describe the existing
valuation/issuance modes and their limits.
