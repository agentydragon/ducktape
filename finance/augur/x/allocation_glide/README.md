# Callable allocation on shared paths

```bash
bb run //finance/augur/x/allocation_glide:compare_bin -- --output-dir /tmp/augur-allocation-example
bbr test //finance/augur/x/allocation_glide:test_compare
```

Compiles one scenario and three stipulated price/CPI paths, then runs native Rust
functions choosing constant 50/50 weights or an annual 50/50 → 70/30 glide over
five years. This is not a forecast, a bond model, or a named-study reproduction.
No evidence download is needed.

Each arm starts with $10,000 cash and $50,000 in each of two test securities at
$100/unit. The growth security stays flat or moves to $80/$120 at month 12;
the other stays at $100. CPI increases 2.5% at each annual reset. Consumption is
$6,000 in opening purchasing power, paid at months 0, 12, 24, 36 and 48. Taxes,
payouts, fees, housing and borrowing are explicitly absent. These finite paths
do not estimate a probability or establish which policy is preferable.

`policy.rs` is the call-site example: the factory creates a target function for
each selected input row. Only current month, funding-account cash and sleeve
values are exposed; the fixed sleeve order is growth, steady. Source-account
bindings, the $0/$10,000 cash band, purchase permission and zero-tolerance
quiet-band drift rule come from the prepared scenario.

The Python shell and Rust entrypoint use the shared
[native invocation helpers](../../rust/docs/execution_boundary.md#native-experiment-invocation)
for prepared-input and output I/O. Policy parameters and analysis remain here.

The glide changes **targets**, not holdings directly. Cash-band raising or
investment suppresses drift rebalancing in that month. Cashflow-only execution
would move toward the target only as cash moves; a target update is not a promise
to rebalance immediately. Consumption and tax demands are funded before any
surplus purchase. Zero targets keep the sleeve sellable, receive no deposits and
permit full exits under the quiet-band rule; at least one target must be positive.

The output directory contains `execution-input.json`, `experiment.json` and both
forensic populations (`constant.json`, `glide.json`). Consumption comes from actual
settled `cash_spend` receipts, not sale proceeds; monthly cash and tax lots retain
the realized holdings, units, basis and sale/purchase events. The runner prints
paid consumption and failure month per path. This small example deliberately
uses full capture, not a high-N compact workflow.
