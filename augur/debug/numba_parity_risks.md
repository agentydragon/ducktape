# Numba vs Polars Simulator Parity Risks

This tracks simulator behavior differences noticed while reviewing the Numba
backend before making it the default engine. Series-indexed amount validation,
duplicate liquidity policies, and scheduled sales/property purchases outside the
scenario horizon are handled separately by the shared simulator input checks.
Transfer `income_category` is narrowed to `"ordinary"` or `None` at the scenario
model boundary. Initial tax-lot `purchase_month_index` values must be distinct
within each `(agent_id, asset_id)` FIFO pool, so sale ordering never depends on a
backend-specific same-month tie-break.

## Product-level Numba coverage resolved

The simulator test file and product projection service test file both run under
both engines:

- `//augur/sim:simulate_test`
- `//augur/sim:simulate_numba_test`
- `//augur/product:projection_service_test`
- `//augur/product:projection_service_numba_test`

This covers the product API fan, selected-rollout detail, tax, failure, and
event-table surfaces through the same entrypoint the frontend uses. No remaining
known behavior divergence is tracked here.
