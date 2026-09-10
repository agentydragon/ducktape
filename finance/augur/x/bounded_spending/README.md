# Executable bounded spending

Python loads paths, compiles a portfolio, and owns the monthly action loop.
The annual rule chooses spending at months 0, 12, … as a percentage of current
cash plus public holdings, bounded by cut/raise limits around the previous
withdrawal adjusted for CPI. Zero cut/raise limits hold real spending constant.
Each CPI reset and limit calculation rounds half away from zero in currency quanta.

The rule observes **after scheduled cashflows and due-claim assembly**. Its policy
calls the shared sleeve helper for overweight-first FIFO sale proposals, then
submits due-claim payments and chosen consumption in that order. No purchases,
quiet-month drift rebalancing, automatic tax gross-up or retry occur. A rejected
action stops its path; earlier successful sales/payments remain and later actions
are unattempted. Execution owns admission, cash movements, lot basis and taxes.

## Historical comparison

The shell uses the Trinity experiment's overlapping historical windows:
Ken French equity and a synthetic 20-year Moody's Aaa constant-maturity bond fund,
with 1926–1995 record intent and actual coverage recorded in `paths.json`.
See <../../study/trinity/README.md> for construction/source gaps.
The portfolio is **tax-free**, sales-only, with no fees beyond those in the supplied
series. Coupons remain cash until needed. This is not a taxable personal plan,
a published flexible-spending reproduction, or an independent Monte Carlo sample.

```bash
bb run //finance/augur/x/bounded_spending:compare_bin -- \
  --evidence-dir /path/to/evidence --output-dir /tmp/spending-comparison \
  --equity-share 0.60 --rate-bps 400 --max-cut-bps 1000 --max-raise-bps 500 \
  --trace-rollout 0
```

For a self-contained runnable example with generated placeholder financial paths:

```bash
bb run //finance/augur/x/bounded_spending:compare_bin -- \
  --synthetic --output-dir /tmp/spending-synthetic \
  --equity-share 1 --rate-bps 400 --max-cut-bps 1000 --max-raise-bps 500 \
  --trace-rollout 2
```

Use a new directory. It retains `execution-input.json`, `policies.json`,
`paths.json`, and compact `fixed_real.json` / `bounded.json` actor results.
Repeat `--trace-rollout` for selected `*.trace-N.json` actor results with detailed
traces; replay uses fresh policy memory and original path IDs.

Every result has a summary containing observed account cash/public marks, exact
ending books, canonical tax/payment records and precise stop reasons. No post-stop
padding is financial data. Detailed capture adds `trace` without changing the summary.

`*.consumption.json` reports nominal currency-quantum percentiles for the
`annual_consumption` component. It excludes taxes and other claims. Live omitted
zero requests are zero; post-stop months are absent. If an earlier rejection
prevents consumption, the unattempted request is absent but actual paid consumption
is zero for that observed month. Requested percentiles use known requests
(`consumption_requested_path_count`); paid percentiles use every observed path
(`observed_path_count`), including these zeros. No supporting observations yields null.
Overlapping historical windows have no independent-sampling error bars.

## Editable Python policy

`SpendingPolicy` returns the common keyed batch of ordered actions.
`BatchPolicy` authors the annual amount rule over NumPy object arrays, preserving
wide exact integer intermediates and path-local memory. `ScalarAdapter` provides
an authoring comparison through the same batch interface, not another engine API.

```python
import json

from finance.augur.x.bounded_spending.python_policy import (
    BatchPolicy, Parameters, SpendingPolicy, run,
)
from finance.augur.x.bounded_spending.stress_paths import prepare

prepared = prepare(rollout_count=3, horizon_months=36)
policy = SpendingPolicy(BatchPolicy(Parameters(400, 1000, 500), 3), {("brokerage", "STOCKS"): 1})
output = run(json.dumps(prepared.execution_input), policy, [0, 1, 2])
```

The caller owns input paths; policies receive only current actor observations.
Fresh policy instances are required for new runs/replay. Chunking and reordering
affect authoring only: all active responses are submitted together exactly once
per month. Python policies must not use neighboring rows as economic information.

## Profiling and CI

```bash
bbr run -c opt //finance/augur/x/bounded_spending:profile_bin -- \
  --rollouts 1000 --horizon-months 60 --native-threads 4 \
  --authoring batch --capture summary \
  --output-dir /home/buildbuddy/workspace/artifacts/command-0/bounded-batch
```

Compare `scalar` and `batch` with identical capture/dimensions.
The real cProfile records construction, Python policy authoring, action/observation
transfer, native steps and final JSON decoding. Input compilation is outside the
profile; process memory includes preparation. These are not isolated native
compute or heap measurements, and no cost budget or language verdict is inferred.
Historical native-control measurements remain pinned to their measured commit in
the [profiling investigation](../../../../debug/augur_python_policy_batches_20260909.md).

`compare_test` and `python_policy_test` run the actual study/profile CLIs with
synthetic data, compare scalar/batch and compact/forensic outcomes, and exercise
CPI resets, original-ID replay, depletion, live zeros and ordered failure prefixes.
