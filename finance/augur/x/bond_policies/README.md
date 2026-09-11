# Bond policies on supplied curves

A runnable mechanics experiment: identical stipulated discount-curve paths drive
dated-bond investment constructions and the existing constant-maturity proxy.
Each construction is crossed with annual household spending, funded by a Python
batch policy through Augur's canonical action session. These are named stress paths, not forecasts or a sample
from which to estimate probabilities. One stress drops the curve to exactly zero
at month 6, exercising zero-yield pricing without a positive-rate floor.

```bash
bb run //finance/augur/x/bond_policies:run_bin -- \
  --output-dir /tmp/augur-bond-comparison \
  --annual-spending 0 5000 10000 --trace-rollouts 4 0
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
- `run.py` gives those paths to `compile_run`, then drives `ActionSession` from
  Python. The shared <../../policy/funding.py> batch policy receives current
  observations, proposes sales to cover due claims, then full claim payments.
  Coupons already received in checking reduce the required sale; no purchases,
  rebalancing, tax gross-up or retry occurs. Surplus coupons accumulate in
  non-interest-bearing checking cash. Claims retain this experiment's month-12
  start, distinct from Trinity's opening-month withdrawal convention.
- The dated constructions retain bond sale proceeds and maturity principal as
  idle cash **inside the investment**, or use proceeds to buy replacement bonds
  when rolling. Principal is not labeled coupon income.

This is a unitized investment model, not tradable `BondHolding` support or
a model of an actual ETF. Redeeming units before maturity liquidates a proportional
part of the investment exposure. Only the zero-withdrawal `hold` control literally
retains every initial bond to maturity; the spending cells test that construction
with withdrawals. The month-18 sale is a precommitted sale of the remaining bond
exposure, not an adaptive spending policy.

Coupon payments precede same-month unit sales. Dated-bond prices include accrued
value between coupon dates and exclude the coupon paid at the current date.
Construction traces are per original investment unit; household quantities and
cashflows belong to action-session results, not to those traces.

## Inspecting a run

`config.json` identifies path order and household policy; `discount_curves.npz`
contains the supplied discount factors. Each construction directory contains
`construction.npz`: dated positions expose bond value, retained cash, coupons,
sales, purchases, and redemption. The proxy exposes only prices and coupons;
its unobserved internal flows are not reported as zero events.

Each spending cell saves its complete `execution_input.json`, canonical compact
results in `rollouts.json`, and selected forensic replays in `traces.json`.
Both result files use `sim.results.Finished`; read them with
`Finished.model_validate_json(path.read_text())` and access `.rollouts`.
`--trace-rollouts` selects original zero-based path IDs, in the supplied order;
omit its values for no detailed replays or trace file. All cells use the same five paths, and
replaying a subset does not renumber them.

`summary.json` links each path/cell to observed requested/paid spending, attempted
payment shortfalls, unpaid claims, tax paid (zero in this tax-free setup), and the
explicit stop result. A failed payment is not partially paid: any preceding unit
sale remains effective, that payment changes no books, and only that path stops.
No later events are invented. Requested/shortfall totals cover attempted payments,
not unobserved future scheduled withdrawals.
Each cell's numerical report is its typed `measurements` record.

Terminal wealth is cash plus marked units only for completed horizons; stopped
assets have their own `ending_mark_month` and are not horizon outcomes. Amounts
are nominal integer USD cents. `stop: null` means completion. Detailed selected
traces contain actual sale proceeds, basis, distributions and balanced journals;
compact capture does not pretend to retain those event details. These outputs
replace the former Parquet frames/`metrics.npz`; no parallel configured run is
performed. Compare named paths directly, without probability weights or rankings
inferred from their counts.

```bash
bbr test //finance/augur/x/bond_policies:test_run
```

Tests run the actual offline CLI and check funding sales, coupon-before-sale
ordering, terminal coupon timing, zero withdrawals, internal redemption versus
household income, fractional units/basis, failed-payment prefixes, and selected
replay/summary agreement.
Pricing and construction tests check the underlying bond mechanics separately:

```bash
bbr test //finance/augur/model:test_nominal_bond //finance/augur/x/bond_policies:test_construction
```

The shared valuation functions accept supplied discount factors, including ones
corresponding to zero or negative rates. The proxy can price through zero, but
negative par yields would imply negative-coupon issuance under its construction;
that remains explicitly unsupported. This experiment does not floor input yields.
It neither fits a yield-curve model nor resolves issues
[#5834](https://github.com/agentydragon/ducktape/issues/5834) and
[#5835](https://github.com/agentydragon/ducktape/issues/5835).
