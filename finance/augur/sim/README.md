# augur/sim

Typed financial declarations, execution preparation, fixed-point helpers and
shared result types. Financial settlement executes in the Python <world.py> (see <docs/financial_engine.md>);
experiment policies and their outer time loops are Python code.

## Experiment path

An experiment composes a `World` (<world.py>) per path on a `MarketPath`: it
declares the accounts, pools, lots, bonds, TLH portfolios, housing and standing
cashflows that exist at month zero, tracks an `EconomicAgent` subclass (<agent.py>) and any `Mortgage`
(<mortgage.py>), `Biller` (<bills.py>) or `TaxAuthority` (<tax_authority.py>)
that exists then, and loops over `world.step()`. Alternatively it starts the
common `ActionSession` and submits
one batch of ordered actions per decision month. Both read typed results from
<results.py> and books from <books.py>; a caller wanting a detailed history
records it between steps with `FinancialCapture` (<capture.py>). A domain the
world does not have is `None` in both, not empty. Exact requests are defined in
<actions.py>; the statements and dues an actor is posted when a month opens are
defined beside their emitters (`accounting.AccountStatement`,
`holdings.PositionStatement`, `claims.BillDue`, `mortgage.InstallmentDue`, …) and
the flat view a policy reads, assembled from them, in <observations.py>. Sampling,
fitting, policy choice and report definitions belong to the caller.

See <../x/joint_spending_allocation/README.md> for a tracked-agent
spending/allocation comparison and <../x/monthly_actions/README.md> for explicit
batch actions.
Shared proposal helpers live in <../policy/>; they do not settle trades or taxes.

<prepared.py> holds the typed resolved facts a world declares: exact integer money,
quantities, tax rules and supplied paths. The compiler's per-table pieces in <compiler/>
lower the authored records of <scenario.py> into them; the app lowers its request through
them and tracks its household (<../policy/configured_household.py>) on each world it
composes.

## Outcomes and failure

A rejected action stops that path, preserves successful earlier actions, and
executes no later actions or policy calls on it. Unpaid due claims are a distinct
stop reason. Invalid routing/input and simulator bugs are errors, not modeled
investment outcomes. Independent paths continue.

Results preserve original rollout IDs, the observed prefix, ending books and
mark time. A stop book is not completed-horizon wealth; unobserved months are not
zero-valued observations. Policy intentions, attempted requests and actual paid
consumption are distinct. Tax and contractual liabilities are not inferred from
a generic spending shortfall.

## References

- <DESIGN.md>: current preparation, execution and output boundaries.
- <REQUIREMENTS.md>: capability requirements and limitations.
- <docs/tax_engine_evaluation.md>: tax engine build-versus-adopt evaluation.
