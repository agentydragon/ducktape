# Bond policies on supplied curves

A runnable mechanics experiment: identical stipulated discount-curve paths drive
dated-bond investment constructions and the existing constant-maturity proxy.
Each construction is crossed with annual household spending, funded by Augur's
canonical Rust engine. These are named stress paths, not forecasts or a sample
from which to estimate probabilities.

```bash
bb run //finance/augur/x/bond_policies:run_bin -- \
  --output-dir /tmp/augur-bond-comparison \
  --annual-spending 0 5000 10000
```

The output directory must not exist. Defaults use $100,000 initial wealth and
$100 investment units. Annual nominal withdrawals occur at months 12 through 72;
the event horizon is 73 months and input point 73 is valuation-only. Taxes,
transaction costs, defaults, inflation, and interest on cash are absent.

## Composition

- `construction.py` supplies curves and constructs unit prices and coupons. The
  dated arms hold the initial bond, sell it at month 18, or roll annually. The
  bonds have three-year terms and fixed 4% annual coupons; replacements can trade
  away from par. Pricing uses `model/nominal_bond.py` with the actual remaining
  payment dates from `sim/bonds.py`.
- The proxy receives three-year annual par yields derived from the same curves.
  It retains same-maturity repricing and monthly payouts; its opening payout is
  set to zero to match the experiment's start-before-first-coupon convention.
  Comparison with a dated arm therefore changes coupon timing and rollover
  frequency as well as valuation. It does not isolate one source of disagreement.
- `run.py` gives those paths to `compile_run` and `RustEngine`. Coupons enter
  checking; a cashflow-only, sales-only policy sells investment units to cover
  withdrawals, with no cash buffer and no purchases. Surplus coupons accumulate
  in non-interest-bearing checking cash.
- The dated constructions retain bond sale proceeds and maturity principal as
  idle cash **inside the investment**, or use proceeds to buy replacement bonds
  when rolling. Principal is not labeled coupon income.

This is a unitized investment model, not native tradable `BondHolding` support or
a model of an actual ETF. Redeeming units before maturity liquidates a proportional
part of the investment exposure. Only the zero-withdrawal `hold` control literally
retains every initial bond to maturity; the spending cells test that construction
with withdrawals. The month-18 sale is a precommitted sale of the remaining bond
exposure, not an adaptive spending policy.

Coupon payments precede same-month unit sales. Dated-bond prices include accrued
value between coupon dates and exclude the coupon paid at the current date.
Construction traces are per original investment unit; household quantities and
cashflows belong to the engine's event frames, not to those traces.

## Inspecting a run

`config.json` identifies path order and household policy; `discount_curves.npz`
contains the supplied discount factors. Each construction directory contains
`construction.npz`: dated positions expose bond value, retained cash, coupons,
sales, purchases, and redemption. The proxy exposes only prices and coupons;
its unobserved internal flows are not reported as zero events.

Each spending cell saves its complete `execution_input.json`, canonical event
frames as Parquet, and monthly product metrics in `metrics.npz`. `summary.json`
links each path/cell to terminal wealth, spending paid, sale proceeds, and failure
month. Money in these outputs is in the reported currency quantum; `-1` means no
failure. Compare named paths directly, without probability weights or rankings
inferred from their counts.

```bash
bbr test //finance/augur/x/bond_policies:test_run
```

Tests check actual Rust funding sales, coupon-before-sale ordering, terminal
coupon timing, the zero-withdrawal control, and saved event/summary agreement.
Pricing and construction tests check the underlying bond mechanics separately:

```bash
bbr test //finance/augur/model:test_nominal_bond //finance/augur/x/bond_policies:test_construction
```

The shared valuation functions accept supplied discount factors, including ones
corresponding to zero or negative rates. The old proxy's positive-yield requirement
is checked explicitly; this experiment does not silently floor its input yields.
It neither fits a yield-curve model nor resolves issues
[#5834](https://github.com/agentydragon/ducktape/issues/5834) and
[#5835](https://github.com/agentydragon/ducktape/issues/5835).
