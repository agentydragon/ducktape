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

The runnable allocation-sensitivity experiment uses this seam to compare bond
constructions on the same markets. Its tax-free recurrence and uncertainty
approximations remain limitations; it is not the taxable household experiment.

Offline checks:

```bash
bbr test //finance/augur/model:test_market_paths //finance/augur/model:test_product_paths
```
