# Product projections

`action_projection.metric_arrays` reduces finished `ActionSession` outcomes into
the arrays consumed by `sim.product_metrics` fan/terminal reducers. It owns no
session or policy. On a dense/forensic result, `rust.event_log.decode_event_log`
reads that same rollout's `trace`; `projection.project_product_rollout` combines
those events with the arrays. Pass the original `rollout_id` separately from its
selected array column.

Compact public cash/securities histories suffice for population metrics; absent
detail is `trace=None`. Property, private-equity and held-bond historical values
are not inferred from an ending book. Unsupported histories raise explicitly.
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
consumption rejection and selected/reordered replay:

```bash
bbr test //finance/augur/product:test_action_projection
```
