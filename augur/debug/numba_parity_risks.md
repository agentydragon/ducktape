# Numba vs Polars Simulator Parity Risks

This tracks simulator behavior differences noticed while reviewing the Numba
backend before making it the default engine. Series-indexed amount validation,
duplicate liquidity policies, and scheduled sales/property purchases outside the
scenario horizon are handled separately by the shared simulator input checks.
Transfer `income_category` is narrowed to `"ordinary"` or `None` at the scenario
model boundary. Initial tax-lot `purchase_month_index` values must be distinct
within each `(agent_id, asset_id)` FIFO pool, so sale ordering never depends on a
backend-specific same-month tie-break.

## Product-level Numba coverage

The simulator test file is run under both engines, but product projection
service tests currently rely on the default engine unless `AUGUR_SIM_ENGINE` is
set. Before switching the default, add a product projection service test target
that runs with the Numba engine so product API fan, rollout detail, tax, failure,
and event-table surfaces are covered through the same entrypoint the frontend
uses.
