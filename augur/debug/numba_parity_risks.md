# Numba vs Polars Simulator Parity Risks

This tracks simulator behavior differences noticed while reviewing the Numba
backend before making it the default engine. Series-indexed amount validation,
duplicate liquidity policies, and scheduled sales/property purchases outside the
scenario horizon are handled separately by the shared simulator input checks.
Transfer `income_category` is narrowed to `"ordinary"` or `None` at the scenario
model boundary.

## FIFO lot tie-break order

Polars sorts candidate lots by `purchase_month_index` and lexical `lot_id`.
Numba breaks same-purchase-month ties by interned string code, which currently
follows scenario/input order. That can change consumed lots, cost basis, and tax
results.

Pin this with a parity test where scenario order is `z_lot`, then `a_lot`, with
the same purchase month. The consumed lot should be explicit.

## Product-level Numba coverage

The simulator test file is run under both engines, but product projection
service tests currently rely on the default engine unless `AUGUR_SIM_ENGINE` is
set. Before switching the default, add a product projection service test target
that runs with the Numba engine so product API fan, rollout detail, tax, failure,
and event-table surfaces are covered through the same entrypoint the frontend
uses.
