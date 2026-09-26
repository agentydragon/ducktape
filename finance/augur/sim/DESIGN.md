# Augur simulator implementation

The current code has one canonical set of financial mechanics and two ways to
drive them: a tracked household on `World.step()`, which the app and every stepped
experiment use, and the common monthly action session, which batch callers use to
submit ordered actions for many paths at once.

## Preparation and dependencies

The authored records in <scenario.py> describe holdings, contracts, cashflows and
policies in exact decimals. The compiler's per-table pieces in <compiler/> lower them
into the records defined in <prepared.py>; preparation does not fetch market evidence,
fit a model or load tax law independently. Prepared records own exact monetary terms,
quantized market paths and variable-length resolved tax rules; a world declares them
directly and keeps no authoring objects.

`sim/` owns declarations, execution, and common books/results. Preparation does not depend on the executor.
`model/` and `fit/` sample and fit; `policy/` contains proposal helpers.
Neither financial settlement nor preparation depends on the app's `product/`
or HTTP modules.

## Common experiment session

```text
supplied paths (+ rules)
    -> World(MarketPath(series, rollout_id, ...), horizon_months=...)
    -> world.declare_account / declare_pool / hold / declare_portfolio / declare_distribution
    -> world.declare_housing / declare_flow / declare_deduction / declare_tender_policy
    -> world.track(agent | mortgage | biller | tax_authority); world.start()
    -> world.step()  # open: statements and dues to the agent; MonthOpened -> ordered actions; close
    -> world.finished; the experiment read what it measures between steps
```

Each declaration refuses what it cannot execute where it is declared: an unknown
account, a missing or unusable series, a cashflow outside the horizon, a lifecycle
event before its purchase. The compiler's per-table pieces (`compile_lots`,
`compile_housing`, `compile_recurring_obligation`, …) lower authored records into
the prepared records these declarations take.

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
Each actor's messages are a closed typed union defined beside it, not a generic bus,
subscription protocol or global event registry.
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
the same view for its `Decision`s from its delegate's mail. A domain nothing declared is absent from the world, not empty: `properties`,
`bonds`, `managed`, `private_equity` and `distributions` are `None` until a
declaration, holding, contract or attached table needs them, and every phase skips
an absent one. `World` has no capture
mode, no named subject and no history: component outcome lists (`accounting.journal`, `holdings.dispositions`, …)
hold the current month and are cleared when the next month opens, so a caller that
wants a history copies them between steps. `capture.FinancialCapture` is the
library's detailed record for a caller that wants one; `ActionSession` records the
summary and trace it returns through it, the app records its own `WorldResult` and
metric slab in <../product/>, and an experiment records only what it measures. A component field
stays only if a later month reads it; there is no observer class, collector protocol or
event bus, and a shared recording helper is extracted only from code that repeats. Each path is stateful;
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

`tax_authority.py` owns one taxpayer's tax: its declared itemized deductions, the
estimates and January true-up it demands, and the year close. Tracking the authority
is what enrolls the taxpayer. Settlement records the taxpayer's income, gains and
deduction facts in the tax book (<tax_year.py>) as it posts the money they describe.
Every successful month the world hands each authority the ledger, the tracked
mortgages and the jurisdiction vocabulary; at the year's last month the authority
assesses the book's facts, posts the assessment as expense against liability in one
journal group, records the liability its true-up later collects, and resets the year
to its capital-loss carryforward.

`sim/world.py::World` owns one path's books, TLH portfolios, mortgages,
receipts and stop state, and opens, acts and closes its months; `sim/session.py`
drives one world per selected path under the batch routing envelope. `actions.py` owns exact requests:
sell these units of these lots at the current price, withdraw or contribute this much,
buy these units, pay this claim. "Raise enough for at least this much cash" or "invest
whatever settlement leaves" is a policy's intent, which it realises as a combination of
exact orders sized from what it observes; no position, managed portfolio or ledger sizes
an order on a policy's behalf, and an order the account cannot fund is rejected, not
trimmed. A managed portfolio is denominated in money, so it takes or pays exactly the
amount ordered, with no unit grid (<tlh.py>); a funding policy's sleeve names it by its
portfolio id, never by the index it tracks, so it has no quote to size against, and
lots of that index are a sleeve of their own. `TlhStatement` says whether each portfolio
accepts a contribution at its current mark, which value and basis alone cannot. `observations.py` owns frozen current facts and private claim authority. There is
no second public native action/observation representation or native session driver.
Configured allocators are not silently enabled through this API. The issuer
protocol on a private holding (`sim/private_equity.py`) is a world phase: `close_month`
runs it after the month's settlement on a path that has not stopped, whichever
driver closes the month, and `declare_tender_policy` says how an owner answers a
sale opportunity. A rejected action stops only its rollout with the successful prefix
intact; an unpaid due claim is a different stop reason. There is no retry callback
within the month.

## Books and capture

Money and quantities use their declared fixed-point scales. Canonical state is
not reconstructed by replaying event descriptions. `books.py` and `results.py`
define typed books, receipts, stops and completed results; receipts reuse the
request definitions in `actions.py`. `events.py` defines
the columnar event frames. Compact and dense/forensic capture are choices of the
recorder outside the world, over the same financial execution. A domain the world
does not have is `None` in `Book` and `FinancialOutput`, not an empty channel;
`event_log` still emits every frame, empty for an absent domain.

Opening snapshot zero precedes events. A stopped event month `f` has an ending
book at snapshot `f + 1`, marked at the already observed month `f`; no future
marks or decisions are invented. Reporting must retain this distinction when
comparing stopped books with completed horizons.

## The app

`ProductService` lowers each request once into a `Situation` of prepared declarations
(<../product/scenarios.py>), samples the series it reads, and composes one world per
path with the app household (<../policy/configured_household.py>) tracked on it;
<../product/simulation.py> steps each to the horizon. The household consults `policy/configured_allocation.py` for its funding
sales, pays every due claim in full in observed order — a claim the cash those sales
leave cannot cover is rejected and stops the path — and sizes each exact purchase from
what those sales and payments leave. Selected detail
executes once and projects both metrics and events from that completed capture
before applying <../product/projection.py>; in-process event projection consumes
captured rows directly, without a JSON export/decode round trip. The app's
projections do not define the financial capabilities or output shape required by
every experiment.

## Rejected designs

- **Explicit guarded composition without a `World`.** Consistency checks across
  agents, contracts and tax treatment need one registration point; every sketch of
  the lighter form reinvented it.
- **A fixed global phase list.** It must be the union of every domain's phases and
  runs them even where the domain is absent.
- **"Re-ask every actor until all are quiet" as the drain rule.** Termination would
  depend on every agent's politeness, and batched policies would face ragged rounds
  even when nothing reactive happened.
- **A ledger that is itself an actor with a mailbox.** Atomic rejection and
  caller-ordered execution are transaction semantics against one ledger and would
  change meaning as asynchronous messages.
- **The tax book as each `TaxAuthority`'s own state.** Settlement paths (lot and
  property sales, distributions, bond coupons, managed-portfolio realizations, claim
  payments, transfers) record tax facts on a copy of the book they swap in only with
  their journal entries, so a rejection leaves both untouched. An authority-owned book
  splits that one transaction across two owners; the authority owns the close and its
  posting, and reads the facts from the ledger's book.
- **Arbitrary bookkeeping calls as the public API, or a universal component/plugin
  framework.** Experiments declare and track concrete domain objects.

A separable state-container/step-coordinator split is deferred until a consumer
needs it; forwarding alone does not justify it.
