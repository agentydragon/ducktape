# Product projections

`action_projection.metric_arrays` reduces finished `ActionSession` outcomes into
the arrays consumed by `sim.product_metrics` fan/terminal reducers. It owns no
session or policy. On a dense/forensic result, `trace.events` carries the same
rollout's columnar event log; `projection.project_product_rollout` combines
those events with the arrays using only the original `rollout_id`. Metric arrays
retain their ordered IDs; `select(ids)` subsets/reorders those IDs with their
columns. Event logs retain owning IDs even for eventless paths. A projection
rejects an ID absent from either input; array-column positions are internal.

Compact public cash/securities and held-bond principal histories suffice for population
metrics; absent detail is `trace=None`. Bond carrying value uses each declared bond's
captured principal, including redemption and stopped-event marks, not a sale quote or
an inferred history from its ending book. Missing/duplicate bond histories reject.
Property and private-equity histories remain unsupported and raise explicitly.
`ProductService` still uses its configured execution path for the existing app
capabilities; these projection functions do not route between engines.

`shortfall_quanta` sums unpaid due claims and valid attempted consumption gaps,
not additional cash needed to fund payment or newly incurred debt. Malformed
actions and unattempted consumption add no monetary shortfall; the canonical
`stop` and attempted receipt prefix retain nonmonetary failure reasons. Wealth
fan observations exclude the stop closing's unobserved next market mark;
selected detail retains the actual stop book. Post-stop padding is unobserved.

The real-session integration control covers both captured modes, summary-only
population reductions, a taxable sale/year crossing, ordered payment failure,
consumption rejection, bond redemption/stopped-CPI marks and selected/reordered replay:

```bash
bbr test //finance/augur/product:test_action_projection
```
