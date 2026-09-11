# Monthly actor actions

```bash
bb run //finance/augur/x/monthly_actions:run_bin -- --output-dir /tmp/augur-monthly-actions
bbr test //finance/augur/x/monthly_actions:test_run
```

One household has no cash, two public-security shares with $40/unit basis, and a
$150 bill due in month 0. Two stipulated paths keep the price at $100 or $50 for
13 months. These are not sampled forecasts or probability estimates. The shares
were acquired 24 months before the opening; there are no payouts, fees, inflation,
housing contracts or borrowing. The creditor and tax authority are scripted sinks.

The synthetic jurisdiction charges 10% on long-term gains and 20% on ordinary
income, with no deductions or capital-loss offset. No prior-year tax means no
estimated payments; the month-11 assessment becomes a month-12 due claim. This is
not a statutory tax model or a personal planning recommendation.

`policy.py` directly authors the single batch function: observe current cash, lots
and due claims; if cash cannot cover the claims, request liquidation of every
public lot; then request each claim payment. This deliberately blunt rule does
not calculate a minimal sale or retry an unaffordable bill. Empty intervening
months still receive a batch decision with no actions. The engine supplies current
prices and applies the shared lot, payment, tax and stopping operations.

The rule calls `policy.cash_band.cash_band` on cash minus already-due claims, with
`floor=ceiling=0`. Its `Raise` proposal triggers this rule's full liquidation;
`Invest` is used only at a cash-only opening, and later investment proposals are
ignored. `Hold` creates no trade. The helper returns a budget; the policy chooses
actions and the executor checks them.

At $100, the sale realizes $200 proceeds and $120 long-term gain. The bill uses
$150, and the later $12 tax claim leaves $38. At $50, the sale realizes $100; the
bill payment rejects and stops that path. Its sale receipt, realized gain and
$100 cash remain. The other path continues; no callback runs again for the stopped
path. No action sorting or automatic funding occurs in execution.

The new output directory retains `execution-input.json` with all assumptions and
`outcomes.json` with each original path's compact summary, stop reason and optional
detailed `trace`. The default `--capture forensic` retains ordered action receipts
and canonical financial output. `--capture summary` retains account/pool observed
numeric series, payment request/results, canonical tax records, exact ending book
and final attempted action prefix without historical trade/journal/monthly books.
`--capture dense` retains detailed output without the journal. Amounts are USD cents. Add `--rollout 1`
for selected replay, or `--rollout 1 --rollout 0` to reorder the same paths.
The tests invoke the documented CLI and check its emitted financial outcomes,
then compare reordered and selected replay through the same authoring function.
All inputs are generated locally; the tests need no live service or evidence data.

`run.py` owns the Python monthly loop over the in-process
[action session](../../sim/docs/financial_engine.md#scoped-household-action-batches).
Policies are ordinary Python functions; editing one requires no Rust rebuild.
Prepared input is written once for reproduction, while current observations and
ordered actions cross the existing extension in memory. There is no native
example binary, per-month subprocess or second financial implementation.

## Cash-only opening

```bash
bb run //finance/augur/x/monthly_actions:run_bin -- --output-dir /tmp/augur-cash-only --cash-only-start
```

This variation starts with $200 cash, no lots and an explicitly declared empty
brokerage pool. The same authored policy invests the opening cash using the pool's
current observed price and the exact quantity helper in `sim/fixed_point.py`.
It floors fractional quantity to the declared scale within the cash budget.
Any bill already due is reserved first: with $200 cash and a $150 opening bill,
the policy buys only $50 of the stock and then pays the bill. An unfundable bill
still fails; a proposal does not create cash or trigger hidden funding.
No allocation policy or dummy holding declares the asset.
The bill arrives in month 1; the two stipulated prices rise from $100/$50 to
$120/$60 then remain fixed. This is a synthetic control, not a market forecast.

The paths buy two/four shares, each with $200 total basis. Their later sales each
produce $240 and $40 short-term gain. After the $150 bill and the synthetic 20%
short-term tax ($8), each retains $82. The offline CLI test verifies these actual
purchase, sale and payment effects; native tests also show that omitting the buy
leaves an empty pool and unchanged cash.

## Population capture and profiling

`--rollouts N --horizon-months H` repeats the same two stipulated paths over the
chosen horizon (at least 13 months). This tests population handling, not a sampling
model or success-probability estimate. Odd original IDs fail in month 0; even IDs
continue. After the bill and month-12 tax payment, the authored rule has no further
spending. With a larger horizon, later tax assessments still follow engine rules.

```bash
bb run //finance/augur/x/monthly_actions:run_bin -- --output-dir /tmp/augur-action-population --rollouts 1000 --horizon-months 60 --capture summary
bb run //finance/augur/x/monthly_actions:run_bin -- --output-dir /tmp/augur-action-replay --rollouts 1000 --horizon-months 60 --rollout 999 --rollout 12 --capture forensic
```

The complete generated execution input is identical for population and selected
replay with the same `--rollouts`/`--horizon-months`; selection never renumbers it.

Profile one capture choice per fresh process, keeping all other arguments equal:

```bash
bbr run -c opt //finance/augur/x/monthly_actions:profile_bin -- --rollouts 1000 --horizon-months 60 --native-threads 4 --capture summary --output-dir /home/buildbuddy/workspace/artifacts/command-0/actor-summary
```

Repeat with `--capture dense` or `forensic` and a new output directory. `report.json`
records input and compact-result hashes, observed (not padded) path-months, wire
bytes and one process RSS high-water mark. `execution.prof` is a real
cProfile recording of the same Python-owned session loop: prepared-input reading,
monthly policy/binding/native work, terminal JSON decoding and output writing,
not isolated native evaluation. Path preparation is excluded from the profile but
included in process RSS, alongside Python and retained native allocations. There
is no native child executor. No performance
threshold or executor-language comparison is implied. The CI tests execute both
documented entrypoints with small generated inputs and check capture/replay parity.
