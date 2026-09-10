# Callable allocation on shared paths

```bash
bb run //finance/augur/x/allocation_glide:compare_bin -- --output-dir /tmp/augur-allocation-example
bbr test //finance/augur/x/allocation_glide:test_compare
```

Compiles one scenario and three stipulated price/CPI paths, then runs Python batch
functions choosing constant 50/50 weights or an annual 50/50 → 70/30 glide over
five years. This is not a forecast, a bond model, or a named-study reproduction.
No evidence download is needed.

Each arm starts with $10,000 cash and $50,000 in each of two test securities at
$100/unit. The growth security stays flat or moves to $80/$120 at month 12;
the other stays at $100. CPI increases 2.5% at each annual reset. Consumption is
$6,000 in opening purchasing power, paid at months 0, 12, 24, 36 and 48. Taxes,
payouts, fees, housing and borrowing are explicitly absent. These finite paths
do not estimate a probability or establish which policy is preferable.

`policy.py` owns target weights, the $0/$10,000 cash band and zero-tolerance
quiet-band drift. The scenario declares accounts, holdings and indexed claims,
not an engine allocation policy. `compare.py` owns the monthly `ActionSession`
loop and supplies the same selected paths to each arm.

The policy reads current account cash, scoped lots/quotes and already-assembled
claims. It calls <../../policy/sleeves.py> for optional `withdraw`, `deposit` or
`rebalance` action proposals over explicit `(account_id, asset_id)` pools.
Withdrawal is additional gross cash to raise, not total spending. FIFO orders
each pool's lots by acquisition month, then lot ID; each lot uses its own quantity
scale. Helpers do not calculate taxes, settle trades or reserve resources across
independent calls on the same observation.

The glide changes **targets**, not holdings directly. Cash-band raising or
investment suppresses drift rebalancing in that month. Cashflow-only execution
would move toward the target only as cash moves; a target update is not a promise
to rebalance immediately. Claim payments precede purchases; a funding raise
precedes payments. Purchases exclude cash reserved for current claims. Drift
proposals budget only unreserved cash plus their own per-lot quoted sale proceeds.
Zero targets keep the sleeve sellable, receive no deposits and
permit full exits under the quiet-band rule; at least one target must be positive.

The output directory contains `execution-input.json`, `experiment.json`, compact
populations (`constant.json`, `glide.json`) and selected forensic replays in
`constant-traces.json` / `glide-traces.json`. Consumption comes from actual paid
`cash_spend` receipts, not sale proceeds. Summaries retain observed cash/holdings,
payment results and ending books; traces retain lot, basis and trade details.

Timing is after cashflows/claim assembly, not opening review. Funding exhaustion
proposes the available scoped sales; an unpayable action stops that path with its
successful prefix intact, without partial payment, another policy call or rescue.
Gross partial sales round quantity upward; purchases round down. Indivisible
units can therefore overshoot a cash raise, and leftover cash is retained.
