# Numba vs Polars Simulator Parity Risks

This tracks simulator behavior differences noticed while reviewing the Numba
backend before making it the default engine. Series-indexed amount validation
and duplicate liquidity policies are handled separately by the shared simulator
input checks.

## Same-month scheduled asset sales

Polars emits every scheduled sale for a month from the same pre-month lot state.
If two same-month sales target the same asset, both can size against the same
available units before `apply_events` consumes them. Numba mutates lot balances
between scheduled sales in the kernel, so later same-month sales see earlier
sales' consumption.

Pin this with a parity test containing two same-month sales over the same lot
stack, ideally with combined requested units greater than available units. Then
choose the intended all-at-once or sequential semantics and align the other
backend.

## FIFO lot tie-break order

Polars sorts candidate lots by `purchase_month_index` and lexical `lot_id`.
Numba breaks same-purchase-month ties by interned string code, which currently
follows scenario/input order. That can change consumed lots, cost basis, and tax
results.

Pin this with a parity test where scenario order is `z_lot`, then `a_lot`, with
the same purchase month. The consumed lot should be explicit.

## Out-of-horizon scheduled events

Polars ignores scheduled asset sales and property purchases whose month is
outside the simulated horizon because it filters by active month during the
monthly loop. Numba writes those months into fixed-size compile-time arrays, so
future months can raise `IndexError` during compilation and negative months can
write into Python's negative index slot.

Pin this with validation or parity tests for scheduled sales and property
purchases at `horizon + 1` and `-1`. Either reject them at scenario validation
or ignore them consistently.

## Transfer income-category fidelity

Polars preserves any configured transfer `income_category` in the event log.
Numba currently decodes `"ordinary"` only when the transfer maps to an ordinary
income profile and otherwise decodes `None`. Current taxation still only uses
`"ordinary"`, but event logs/API consumers can observe the difference.

Pin this with an event-log test using a non-ordinary category such as `"gift"`,
or narrow the scenario model to the intended literal set.

## Product-level Numba coverage

The simulator test file is run under both engines, but product projection
service tests currently rely on the default engine unless `AUGUR_SIM_ENGINE` is
set. Before switching the default, add a product projection service test target
that runs with the Numba engine so product API fan, rollout detail, tax, failure,
and event-table surfaces are covered through the same entrypoint the frontend
uses.
