# `stationary_bootstrap` — resampling the replay's record

`StationaryBootstrapModel` (<stationary_bootstrap.py>) resamples the `MacroHistory` record that core historical replay
(<../../model/historical_windows.py>) replays whole. The module docstring states the conventions a result depends on.

## What it promises

The record is resampled in blocks of consecutive months, drawn jointly across every series, at a caller-chosen mean
block length and any horizon, including one longer than the record. Every month after the opening is one record month's
rates and index growth, so the support is the record's: a path can recombine its worst episodes but never exceed its
worst month. At a block seam every rate moves between two historical levels within a month, and bond funds price that
move. A rollout depends only on its seed, not on the batch or the horizon, and provenance names the record's span, the
mean block length and the seeds.

## Use

```python
from finance.augur.model.bond_fund import BondFundSpec
from finance.augur.model.product_paths import construct_products
from finance.augur.x.models.stationary_bootstrap import StationaryBootstrapModel

# 120-month mean blocks, as in Anarkulova, Cederburg, O'Doherty & Sias (JPEF 24(3), 2025):
bootstrap = StationaryBootstrapModel(history=historical.history, mean_block_months=120.0)
market = bootstrap.sample_market(request)
products = construct_products(
    market, equity=None, instruments=(BondFundSpec(symbol="test_fund", maturity_years=8.0),)
)
```
