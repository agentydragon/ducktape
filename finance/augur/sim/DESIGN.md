# Augur simulator implementation

The current code has one canonical set of financial mechanics and two remaining
Python orchestration surfaces: the common monthly action session and the configured
runner used by the app and legacy acceptance readers.
They share financial steps but differ in policy control and supported domains.

## Preparation and dependencies

`Scenario` describes actors, accounts, holdings, contracts and path bindings.
`compile_run` in <compiler/execution.py> resolves those inputs into the typed `CompiledRun`
defined in <prepared.py>.
It does not fetch market evidence, fit a model or load tax law independently.
Prepared records own exact monetary terms, quantized market paths and variable-length
resolved tax rules. Sessions and metadata readers consume these facts directly;
the file boundary privately serializes them without retaining a parallel
document or original authoring objects. File decoding returns the same typed records.

`sim/` owns declarations, execution, and common books/results. Preparation does not depend on the executor.
`model/` and `fit/` sample and fit; `policy/` contains proposal helpers.
Neither financial settlement nor preparation depends on the app's `product/`
or HTTP modules.

## Common experiment session

```text
supplied paths (+ rules)
    -> World(MarketPath(series, rollout_id, ...), horizon_months=...)
    -> world.declare_account / declare_pool / hold / declare_portfolio
    -> world.track(agent | mortgage | biller | tax_authority); world.start()
    -> world.step()  # open: statements and dues to the agent; MonthOpened -> ordered actions; close
    -> world.finished; the experiment read what it measures between steps
```

An authored `Scenario` reaches the same world through the import adapter,
`compile_run` then `World.from_run(run, rollout_id)`, which declares and tracks
what the prepared scenario describes and attaches the configured-only tables.

The batch form drives one such world per selected path:

```text
    -> ActionSession.start()
    -> current scoped observations
    -> Python batch policy
    -> ActionSession.advance(ordered actions)
    -> next observations or typed Finished
```

The caller owns the time loop; a tracked agent owns its memory, a batch policy's
memory belongs to the caller. Everything tracked is an `Actor[In, Out]` (<actor.py>):
it receives the typed messages addressed to it and returns the messages it emits.
When a month opens the counterparties act first: each `Biller`, `Mortgage`,
`PropertyTaxAuthority` and `TaxAuthority` is posted the statement it reads
(`PropertyStatement`, `ServicingStatement`, `TaxLiabilityStatement`) and
`MonthOpened`, and the demand it returns is registered as this month's claim on its
payer, in that tier order. Then the world posts each agent's mail — every emitter's
statement (`MarketStatement`, `AccountStatement`, `PositionStatement`,
`BondStatement`, `TlhStatement`, defined beside the component that issues it), the
typed dues (`BillDue`, `AssessmentDue`, `InstallmentDue`, `PropertyTaxDue`) and last
month's `Receipt`s — and `step` delivers `MonthOpened`, whose reply is the
household's ordered actions. The world builds no view on anyone's behalf: `EconomicAgent` assembles the
`Observation` its `decide` reads from the mail it kept, and the batch session assembles
the same view for its `Decision`s from its delegate's mail. `World` has no capture
mode, no named subject and no history: component outcome lists (`accounting.journal`, `holdings.dispositions`, …)
hold the current month and are cleared when the next month opens, so a caller that
wants a history copies them between steps. `ActionSession` records the summary and
trace it returns; the configured runner records `WorldResult` through
`capture.FinancialCapture`; an experiment records only what it measures. Each path is stateful;
parallel paths do not make future months independent. Policies see current
actor-scoped facts, not future sampled market trajectories. `World` owns the
month/phase sequencing, receipts and fatal-stop lifecycle of one path; `ActionSession`
owns which paths it selected and the shared clock across them. Python financial
operations own books, transaction validation, settlement, liabilities and tax
consequences.

`mortgage.py` owns loan terms, the fixed installment, active servicing state and
paid-interest YTD. Outstanding principal remains authoritative in the liability
ledger: at open the world posts each contract a `ServicingStatement` carrying it,
the contract's `MonthOpened` reply is the installment quote the ledger registers
as the borrower's `InstallmentDue`, and a settled installment comes back as
`InstallmentPaid`. A contract that exists at month zero is tracked with its
outstanding balance, which opens the ledger against `equity:opening`; a
configured purchase's loan is originated by the purchase entry and then serviced
the same way. The world supplies immutable payment and year-end facts to Python
accounting; capture DTOs do not maintain another mutable mortgage. Configured purchase/sale
timing is documented in <../docs/rental_and_lifecycle.md>.

`sim/world.py::World` owns one path's books, TLH portfolios, mortgages,
receipts and stop state, and opens, acts and closes its months; `sim/session.py`
drives one world per selected path under the batch routing envelope. `actions.py` owns exact requests;
`observations.py` owns frozen current facts and private claim authority. There is
no second public native action/observation representation or native session driver.
Configured allocators, housing and PE behavior are not silently enabled through
this API. A rejected action stops only its rollout with the successful prefix
intact; an unpaid due claim is a different stop reason. There is no retry callback
within the month.

## Books and capture

Money and quantities use their declared fixed-point scales. Canonical state is
not reconstructed by replaying event descriptions. `books.py` and `results.py`
define typed books, receipts, stops and completed results; receipts reuse the
request definitions in `actions.py`. `events.py` defines
the columnar event frames. Compact and dense/forensic capture are choices of the
recorder outside the world, over the same financial execution.

Opening snapshot zero precedes events. A stopped event month `f` has an ending
book at snapshot `f + 1`, marked at the already observed month `f`; no future
marks or decisions are invented. Reporting must retain this distinction when
comparing stopped books with completed horizons.

## Remaining configured consumers

`ProductService` calls <configured.py> for compact product arrays or dense event
frames. Selected detail executes once and projects both metrics and events from
that completed capture before applying <../product/projection.py>. In-process
event projection consumes captured rows directly, without a JSON export/decode
round trip. Explicit JSON exports, prepared-input file serialization and
the legacy acceptance result adapter remain separate boundaries.

The configured runner owns full-horizon loops and
implicit allocation/grouped-funding behavior. `policy/configured_allocation.py`
proposes trades through shared Python helpers; Python financial operations settle them.
The app's projections do not define
the financial capabilities or output shape required by every experiment.

`sim/testing/simulation_result.py` and `sim/testing/configured_result.py` are the separate legacy
acceptance adapter, not the common public result contract. Existing tests on
those adapters remain until equivalent supported-domain controls move to the
action session. Keep independent expected financial facts, rather than retaining
an obsolete runner just to compare implementations.
