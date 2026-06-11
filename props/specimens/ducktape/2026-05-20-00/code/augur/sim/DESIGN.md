# Augur sim — design

Companion to <REQUIREMENTS.md>. This doc translates the requirements
into a concrete shape — the layers of state, the forward loop, the
transaction taxonomy, how templates + jurisdictions + locations are
expressed, and the file layout — without committing to particular
function signatures yet. Read it as the **load-bearing structural
decisions**, not the API.

The goal: design once, in shape that grows from S1.1 (Alice gives
Bob \$5) up through the full housing + multi-property + constrained-
liquidity + multi-jurisdiction-tax spec without retrofitting. If a
later layer requires moving primitives around, that's a design bug
to catch here.

## Package Boundaries

`augur/sim` is the durable pathwise evaluator, not the owner of the
market model and not the owner of API presentation. The intended
runtime seam is:

```
augur/model  -> sampled exogenous trajectory bundle
augur/sim    -> deterministic evaluation of scenario set over paths
augur/api    -> request parsing, catalog/config composition, response shaping
augur/frontend -> product UI over the API response
```

`augur/model` owns evidence ingestion, calibration, fitted-model
identity, and stochastic sampling. `augur/sim` consumes already-
materialized exogenous paths: asset prices, property values, rates,
rent/spend variance, sellability masks, tender opportunities, and
similar rollout/month streams. A seeded deterministic or GBM path
helper can live in `sim` temporarily as test/bench scaffolding, but
production stochastic path generation belongs in `augur/model`.

The seams are allowed to change while this engine becomes the main
simulation backend. The goal is a clean `model -> sim -> api` contract,
not compatibility with old names or awkward legacy response shapes.

## Canonical log; derived state

**The event log is the source of truth.** State at month M is, by
definition, `apply_events(initial_state, all events at months ≤ M)`.
The simulator maintains a per-month state representation as an
incremental cache (so each step doesn't re-aggregate the full log
from month 0), but that cache is by construction equal to what
re-deriving from the log would produce. There is no "state value
the engine tracks independently of any logged event".

Practical consequences of this discipline:

- Every state transition that's the result of "something happened"
  is logged as an event. State doesn't move without an event.
- Two kinds of state attributes get treated differently:
  - **Event-sourced attributes** — anything that's the cumulative
    result of events: cash balances, asset lot units + basis,
    liability principal, occupancy mode, cumulative depreciation,
    ownership tenure, primary-residence-use months, rollout status.
    These are aggregations over the event log.
  - **Trajectory-derived attributes** — anything that's "what's the
    current price / current value / current availability": asset
    unit prices, property current market values, sellability
    masks. These are per-month reads from the exogenous trajectory
    bundle, not on the log. They're recomputed each month rather
    than stored long-term.
- The `apply_events(state, events) → state'` function is the only
  state-mutation primitive. It's called once per step and is the
  single testable site where "this event changes state this way"
  is enforced.
- The replay invariant is a property: `state_at(M) ==
apply_events(initial_state, log.filter(month ≤ M))` for every M.
  Easy to assert in tests; opt-in `--check-replay` flag asserts it
  per-month at runtime. If it ever fails, the bug is in
  `apply_events` and the fix is in one place.

The legacy engine had state mutation interleaved with transaction
recording in a way that made the two get out of sync over time —
mortgage payments updated some matrices but not others, sale
recorders wrote one shape and tax allocation read another, and the
"what's actually happened" answer was scattered across five
overlapping representations. This design says: there's one answer
to "what happened" (the log), and state is its incremental view.

## Five data layers

State / configuration in the simulator lives at five distinct
lifetimes. Each layer is read by a specific set of consumers; muddy
boundaries between them are the smell that produced the legacy
engine's "five overlapping representations" problem.

1. **Static reference data** (YAML, repo-checked-in). Federal + CA
   tax brackets, NIIT thresholds, SALT cap, qualified-residence
   interest cap, LTCG thresholds, depreciation schedules, §1250
   rate, §121 exclusion amount. Per-location property-tax rates,
   transfer-tax schedules, applicable-jurisdiction lists. Loaded
   once at startup; validated by Pydantic models; consumed by
   templates as parameters.

2. **Scenario configuration** (Pydantic, constructed by users).
   The agents, their initial balance sheets, their tax profiles
   (filing status, prior-year tax), their positions (one per
   capital-gains-eligible holding configured), their liabilities
   (mortgage origination events with terms), their properties
   (with location id, occupancy timeline, mortgage reference),
   their policies (liquidity-sale order, buffer rules, spending),
   their scheduled events (property purchases, sales, occupancy
   switches), and references to the exogenous paths they consume.
   Validated at construction time.

3. **Exogenous trajectory bundle** (produced outside `sim`,
   usually by `augur/model`). Per-rollout per-month paths for every
   market driver the scenario consumes: asset unit-price
   multipliers, property-value multipliers per location,
   sellability masks for constrained-liquidity positions,
   spending-variance paths, rental-income variance paths,
   tender-opportunity masks, rates, and any other market-derived
   streams. `sim` treats this bundle as input data. It does not fit
   models, calibrate distributions, or sample production stochastic
   paths.

4. **Event log — the canonical record of what happened** (polars,
   append-only). Every state-changing happening in the simulation
   appears here:
   - `events_log` — every event chronologically: cash transfers,
     asset purchases / sales, lot consumptions, mortgage payments
     (with P+I split), obligation accruals + settlements, tax
     accruals + payments, income arrivals, depreciation accruals,
     property purchases / sales (composite, multiple atomic rows
     sharing a `cause_id`), occupancy-mode changes, ownership
     starts / ends, failures. See [Event taxonomy](#event-taxonomy)
     below for the kinds.
   - Per-event-kind projections that fall out at output time:
     `transactions_log` (cash-bearing events), `lot_dispositions_log`
     (one row per consumed lot per sale, projected from
     asset-sale events), `obligations_lifecycle_log` (accrual +
     settlement pairs grouped by obligation), `failure_events_log`
     (failure events), `policy_decisions_log` (every policy
     evaluation, including no-fire), `tax_year_breakdown_log`
     (year-end snapshots of the year-tax computation per
     jurisdiction).

   All event-sourced state at month M is `apply_events(initial_
state, events_log.filter(month ≤ M))`. The log is what survives
   if working-state-layer 5 is dropped.

5. **Working state — the incremental materialization of layer 4**
   (polars, six frames). The simulator's current view of the
   world. Six long-form frames keyed by `(rollout_index,
month_index)` plus entity-id columns:
   - `cash_balances` — `(rollout, month, agent_id, account_id,
balance_usd)`. **Event-sourced** — cumulative cash deltas.
   - `asset_lots` — `(rollout, month, agent_id, position_id,
lot_id, template_id, units, basis_usd, acquired_month,
cost_basis_method, sellability_mask_ref, current_unit_price_
usd, current_market_value_usd)`. **Mixed**: units + basis +
     acquired_month are event-sourced; current_unit_price_usd and
     current_market_value_usd are trajectory-derived (refreshed
     each month from the exogenous trajectory bundle).
   - `liabilities` — `(rollout, month, agent_id, liability_id,
template_id, counterparty_agent_id, principal_usd,
interest_accrued_this_month_usd, principal_paid_this_month_
usd, deductibility_flag, …)`. Event-sourced.
   - `property_state` — `(rollout, month, property_id, location_
id, occupancy_mode, adjusted_basis_usd, cumulative_
depreciation_usd, owned_since_month, current_market_value_
usd, …)`. Mixed: occupancy_mode + adjusted_basis_usd +
     cumulative_depreciation_usd + owned_since_month are
     event-sourced; current_market_value_usd is market-derived.
     §121 use-clock + ownership-tenure-clock are derived on demand
     from the occupancy-change event history (not stored).
   - `property_stakes` — `(rollout, month, agent_id, property_id,
ownership_pct, contribution_used_usd, equity_ledger_usd)`.
     Event-sourced. Single-row-per-property for single-owner;
     multi-row for partner-equity stretch.
   - `rollout_status` — `(rollout, month, status, failure_event_
id, failure_month)`. Event-sourced.

   These frames **grow forward** as the loop advances; at month M
   they hold rows for months `0..M`. The state at month M is
   `frame.filter(month_index == M)`. Per-rollout / per-agent
   queries are polars filters / group-bys.

   **The state-over-time IS an output of the simulation, but it
   is the materialization of the event log + trajectory reads**,
   not a separately-maintained truth. If we ever needed to compress
   the simulation's persistence footprint, the event log, initial
   state, and exogenous trajectory bundle are the complete record;
   everything else is derivable.

The boundary discipline: layer 1 is law-and-place data, edited
when reality changes. Layer 2 is what the user wants to simulate.
Layer 3 is exogenous path input supplied by the market/modeling
package. **Layer 4 is what happened; layer 5 is its per-month view
plus market-derived attributes.** The forward loop appends to layer
4 and incrementally maintains layer 5; the replay invariant
guarantees they stay aligned.

## The forward loop

```
state_t = StateCrossSection.initial(scenario, market, rollout_count)
append(state_t → state_frames at month 0)

for month in range(horizon_months):
    # Step produces events for this month. Pure: no state mutation
    # happens inside step(); state_t is read-only.
    events_t = step_emit_events(
        state=state_t,
        market=market.at(month),
        jurisdictions=jurisdiction_set,
        scenario=scenario,
        month=month,
    )

    # Apply events to advance event-sourced state. Single mutation
    # point. apply_events is the ONLY function that writes the
    # event-sourced columns of state.
    state_t_event_sourced = apply_events(state_t.event_sourced, events_t)

    # Refresh market-derived attributes from the next month's
    # market reads. Not part of apply_events because these aren't
    # caused by events — they're per-month market lookups.
    state_t = compose_state(
        event_sourced=state_t_event_sourced,
        market_derived=market.derive_at(state_t_event_sourced, month + 1),
    )

    append(state_t → state_frames at month+1)
    extend(events_t → events_log)

return SimulationRun(
    state_frames=concat_state_frames(...),
    events_log=concat(events_log),
    # Per-event-kind projections (lot dispositions, obligations
    # lifecycle, failures, policy decisions, tax breakdowns) are
    # filters over events_log assembled at output time.
)
```

The simulator does not re-derive any prior month. Each `state_t`
is the polars cross-section (no `month_index` column inside the
step body — `state_t` is "the rollouts at one fixed month"); the
loop tags it with `month_index = M` when appending to the
forward-growing frames.

**Replay invariant** (asserted in tests; opt-in `--check-replay`
flag asserts it per-month at runtime):

```
state_t.event_sourced ==
    apply_events(
        initial_state.event_sourced,
        events_log.filter(month_index ≤ t),
    )
```

If this ever fails, the bug is in `apply_events` (the only place
that writes event-sourced state) and the fix is in one place.

## The step function — five phases of event emission

`step_emit_events(state, market, jurisdictions, scenario, month) →
events_for_month`. The step is a **pure function**: it reads
`state` (the per-month cross-section, including market-derived
attributes refreshed at the prior iteration's end) and returns a
batch of events for this month. It does not mutate state. The
loop's `apply_events(state, events)` step is the single mutation
point.

Phases run in this fixed order. Each phase is one or more polars
expressions over the rollout column; no Python iteration over
rollouts; each phase **appends rows to the within-step event
buffer**. Phases later in the order can read events emitted by
earlier phases via that buffer + a virtual "what state would be
if we applied buffered events so far" projection (a thin wrapper
over `apply_events` that doesn't commit). This lets phase 3 see
the obligation accruals from phase 2 without phases having to
share state mutation.

1. **Scheduled events.** Apply scenario-scheduled events whose
   month equals this month. Each emits one or more event rows:
   - Property purchase event → emits a property-purchase composite
     event (cash debit for down + buy closing, property creation,
     mortgage origination).
   - Property sale event → emits a property-sale composite event
     (proceeds calc, mortgage payoff, net to seller, property
     retirement, lot dispositions).
   - Occupancy-mode switch → emits an occupancy-change event.
   - Income arrival → emits an income event.
   - Recurring-obligation accruals due this month → each emits an
     obligation-accrual event (property tax, HOA, insurance,
     maintenance, special assessment if due, mortgage payment,
     monthly spend, outside rent).
   - Tax accruals at year-end → each emits a tax-accrual event per
     (agent, jurisdiction). Estimated-tax cash transfers fire at
     Apr 15 / Jun 15 / Sep 15 / Jan 15 markers; the January marker
     also emits the true-up and the liability-side settlement.

2. **Emit liquidity policy decisions.** Required obligations are
   immediate due-now cash demands in the month they fire. The
   liquidity policy sees those demands plus current state and emits
   any sale dispositions needed to fund them. This is the point where
   hard-demand funding and discretionary cash-buffer sales meet:
   buffer rules evaluate against the post-hard-demand cash view. If a
   policy emits no sale orders, settlement will fail a demand that
   cash cannot already cover, even if the agent owns sellable assets.

3. **Settle required obligations.** Settlement is mechanical: cash
   plus policy-emitted sale proceeds either covers every hard demand
   for an account in full or pays none of them and emits failure rows.
   The current model does not carry partially-paid obligations across
   months and does not model delinquency balances, grace periods, or
   underpayment penalties. Optional cash-buffer targets are not hard
   demands and cannot fail a rollout by themselves. A plausible future
   shape is to make policy own all agent behavior for the month,
   including the checking-cash transfers that satisfy obligations; in
   that model settlement would validate policy output instead of
   emitting required-payment transfers itself.

4. **Other discretionary policies.** Reinvest-excess, optional buys,
   and other non-obligation actions evaluate after hard-demand
   liquidity/settlement once those policies exist.

5. **End-of-month accruals.** Emit non-cash state-changing
   events:
   - Depreciation accrual on every property in `rental`
     occupancy mode → emits a depreciation-accrual event
     `(rollout, month, property_id, monthly_depreciation_amount)`.
     The jurisdiction's residential-rental schedule supplies the
     denominator.
   - Mortgage interest accrual / amortization tick is already
     captured by the mortgage-payment event at phase 1; no
     additional event needed.
   - Clock-style state (§121 use clock, ownership tenure, asset
     lot age) is **not** stored on state and **not** emitted as
     events — it's derived on demand at sale time from the
     occupancy-change event history (for §121) or from
     `acquired_month` vs current month (for lot age). This
     drops a category of "tick" events from the log and
     simplifies state.

6. **Rollout-status check.** If any required obligation in this
   month went unfunded, the settlement phase emits a failure event
   and marks the rollout failed. Current scope has sticky failure:
   later-month inflows do not recover the rollout, and there is no
   partial-payment or delinquency lifecycle state.

The returned `events_for_month` is the union of all phase event
buffers, in emission order. Each event row carries `(rollout,
month, kind, cause_id, ...kind-specific columns)`. The loop's
`apply_events(state_t, events_for_month)` consumes this and
produces the event-sourced part of `state_{t+1}`.

Per-event-kind projections (transactions, lot dispositions,
obligation lifecycle, failures, policy decisions, tax
breakdowns) are filters / joins / group-bys over `events_log`
assembled at output time. They are not separately tracked
alongside the log.

## Event taxonomy

Every state-changing happening in the simulation appears as one
or more events on the `events_log`. Discriminated union; each row
carries `kind` + `cause_id` + kind-specific columns.

The single per-step buffer is the union of all events emitted
this month. `apply_events(state, events)` is the only function
that mutates event-sourced state; it dispatches on `kind` to
update the relevant frame.

**Cash-side balance invariant**: for events that move cash,
`Σ(agent_cash_delta_usd) == 0` across the agents the event
touches. For events that aren't cash transactions (occupancy
mode change, depreciation accrual, rollout status change), no
cash invariant applies — they update non-cash state.

Event kinds (the full set; not every kind is needed at every
layer):

**Cash-bearing (transactions):**

- **Transfer** — cash moves between two agents' accounts. The
  primitive that S1.1 uses. May carry an income classification
  on the receiving side.
- **Income arrival** — recurring cash inflow with a
  tax-classification label (W-2 ordinary, rental income, etc.).
  Practically a Transfer from a market-sink agent.
- **Asset purchase** — cash leaves an agent's account, a new lot
  appears on the asset_lots frame. Counterparty is the market
  sink (for market buys) or another agent (for inter-agent
  transfers of an asset, if we ever model that).
- **Asset sale** — units of one or more lots are consumed (per
  the position's cost-basis method); cash arrives. The lot
  consumption is part of the event; the realized gain falls
  out of (proceeds − basis_consumed).
- **Mortgage payment** — composite: borrower's cash out (P+I);
  lender's cash in; borrower's mortgage liability principal
  decreases by P. The I portion is captured as a column on the
  event for the borrower's qualified-residence-interest tally
  and the lender's interest-income tally (lender is
  not-tax-paying per scope; the row exists for symmetry).
- **Obligation settlement** — cash out from the obligated agent;
  cash in to either a counterparty agent (intra-sim) or a sink
  (taxing authority, HOA, insurance company); marks the
  obligation as paid. Current scope has no partial-payment state:
  if the settlement cannot be funded immediately, the rollout
  fails.
- **Tax payment** — special-case obligation settlement against
  the tax-payable liability. Quarterly estimated or year-end
  true-up. Cash out is recorded as a transfer; the January
  liability-side settlement is recorded separately so the
  tax_payable balance decreases when the bill is due.
- **Property purchase / sale (composite)** — produces multiple
  atomic events sharing a `cause_id`: a cash debit for down +
  buy closing, an asset-purchase-equivalent (the property
  appears on `property_state`), a mortgage-origination event
  for the liability side. Property sale: closing cost out,
  mortgage payoff (a Mortgage payment event that fully pays the
  remaining balance), net proceeds to seller (a Transfer to the
  seller's cash), and a property retirement.

**Non-cash state mutations:**

- **Obligation accrual** — an obligation comes into being for
  this month (a property tax bill is due, a tax-payment marker
  fires, the mortgage's monthly payment is due). Adds a row to
  the (open obligations view, derivable from log) and feeds
  phase 2's settlement.
- **Tax accrual** — at year-end, a TAX_PAYABLE liability appears
  on the agent's books for the year's actual tax by jurisdiction.
  Subsequent tax-settlement events reduce it.
- **Depreciation accrual** — property's `cumulative_
depreciation_usd` increases by `monthly_depreciation_amount`.
  Year-end tax computation reads the year's sum.
- **Occupancy-mode change** — property's `occupancy_mode` flips
  from one value to another at this month. Drives whether
  depreciation accrues, whether rental-income arrives, whether
  the §121 use clock is ticking. The §121 use clock and ownership
  tenure are derived on demand from the chronological sequence
  of these events — they're not stored on state.
- **Mortgage origination** — a new amortizing-loan liability
  comes into being for a borrower with a counterparty lender.
  Cash side (loan principal → borrower cash) is recorded as part
  of the property-purchase composite or as a standalone Transfer
  if origination is standalone.
- **Failure event** — a required obligation went unfunded after
  liquidity policy sale decisions. Row carries `(rollout, month,
obligation_id, obligation_type, amount_due, amount_paid,
shortfall, attempted_funding_sources)`.
- **Rollout-status change** — `rollout_status` flips from
  `active` to `failed`. Event-sourced; the `rollout_status` frame
  is the per-month materialization. Recovery from failed back to
  active is future scope, not part of the current contract.
- **Policy decision (diagnostic)** — every policy evaluation
  for every rollout, including no-fires. Doesn't change
  event-sourced state directly (the asset-sale / asset-purchase
  events the policy emitted do that), but is on the log for
  diagnostics. The `policy_decisions_log` projection filters
  for these.
- **Tax-year breakdown (diagnostic)** — at each year-end, a
  snapshot of the year-tax computation: per `(rollout, agent,
year, jurisdiction)` the income totals, deduction amounts,
  bracket walks, NIIT, totals. Doesn't itself drive state
  (the tax accrual + tax payment events do that), but is a
  first-class diagnostic on the log.

Composite events (Property purchase / sale, Mortgage origination

- down-payment in one logical purchase, etc.) emit multiple
  atomic event rows sharing a `cause_id`. Each atomic row is
  balance-checked on its own. The materialize step can
  `group_by(cause_id)` to produce a coarse-grained "one mortgage
  payment with P+I split" view when the user wants that.

The `apply_events` function dispatches on event `kind` to update
the right frame:

- Cash-bearing events → update `cash_balances`.
- Asset purchase / sale → update `asset_lots`.
- Mortgage payment / origination → update `liabilities`.
- Property purchase / sale → update `property_state` (+ the
  cash + asset + liability rows that fall out).
- Occupancy-mode change / depreciation accrual → update
  `property_state`.
- Rollout-status change → update `rollout_status`.
- Tax accrual → update `liabilities` (tax_payable row).
- Tax settlement → reduce `liabilities` (tax_payable row).
- Failure event / policy decision / tax-year breakdown →
  diagnostic only, no state mutation.

## Templates, jurisdictions, locations as data

The asset templates listed in REQUIREMENTS.md are not Python
classes. They are **discriminator values on the appropriate frame**
plus a **per-template rule function** that the engine applies to
the rows tagged with that template.

Concretely:

- `asset_lots.template_id` is a string column. Values:
  - `"capital_gains_eligible_holding"` for stock-like / crypto-
    like / PE-like positions.
  - (Possibly future: `"interest_bearing_account"` for savings,
    `"non_taxable_holding"` for tax-advantaged accounts, etc.)

- `property_state.template_id` is a string column. Values:
  - `"depreciable_real_property"` for any property.

- `liabilities.template_id` is a string column. Values:
  - `"amortizing_loan"` for mortgages and any other fixed-payment
    debt between two agents.
  - `"tax_payable"` for accrued-but-unpaid tax.

Per-template rules live in code as functions over polars frames:

- `apply_mark_to_market_capital_gains_eligible_holding(frames,
market)` — filters asset_lots by `template_id =
"capital_gains_eligible_holding"`, joins market prices, returns
  updated rows.
- `apply_depreciation_accrual_depreciable_real_property(frames,
jurisdiction)` — filters property_state by `template_id =
"depreciable_real_property"` + `occupancy_mode = "rental"`,
  applies the jurisdiction's residential-rental schedule, returns
  updated rows.
- `apply_amortization_amortizing_loan(frames, month)` — filters
  liabilities by `template_id = "amortizing_loan"`, computes
  this month's P+I split.
- `compute_year_tax_tax_payable(frames, jurisdiction, year)` —
  filters liabilities by `template_id = "tax_payable"`, computes
  the year's accrual based on aggregated income + gains.

The engine's per-month step calls one function per (rule,
template). Adding a new template = new discriminator value + new
filter functions; the rest of the engine doesn't change.

**Jurisdictions** are Pydantic records loaded from YAML at
startup:

- `JurisdictionId` is a string (`"federal_us"`, `"california"`).
- A `Jurisdiction` record carries: brackets per filing status,
  standard deduction per filing status, NIIT params, SALT cap,
  qualified-residence-interest cap, LTCG threshold table,
  depreciation schedule, §1250 rate, §121 exclusion amount,
  ordinary-vs-LTCG treatment flag for capital gains.
- The `JurisdictionSet` is the loaded collection — a frozen dict
  `jurisdiction_id → Jurisdiction`. Loaded once at simulate()
  entry.

**Locations** are Pydantic records loaded from YAML at startup:

- `LocationId` is a string (`"san_francisco"`, `"palo_alto"`).
- A `Location` record carries: applicable jurisdiction ids (list,
  ordered), property tax rate / formula, transfer tax schedule,
  any other per-location parameters.
- The `LocationSet` is the loaded collection. Loaded once.

Property rows reference a `location_id`; agent residence rows
reference a `location_id`. Tax computation iterates over the
relevant jurisdictions for each row and applies their parameters.

## Output assembly

At end of `simulate(...)`:

- The per-month state cross-sections (one per month, accumulated
  during the loop) are concatenated into the long-form
  state-over-time frames with `month_index` injected as a column.
- The per-month log rows (transactions, obligations, lot
  dispositions, failures, policy decisions, tax breakdowns) are
  concatenated into the long-form append-only log frames.
- `ProjectionRun` read models are computed on demand from the
  state-over-time and event-log frames:
  - Net-worth time series per `(rollout, month, agent)`, split into
    cash, liquid asset market value, asset book value, property book
    value, liability principal, liquid net worth, and book net worth.
    Property value is adjusted basis until `augur/model` supplies
    market-valued real-estate paths.
  - Account-balance rows for cash and liabilities.
  - Transaction/audit rows from cash transfers, lot dispositions,
    obligation settlements, and tax-liability settlements.
  - Tax breakdown, obligation lifecycle, failure, and rollout-summary
    rows for API/frontend validation.

The returned `SimulationRun` wraps the state-over-time frames, log
frames, accepted scenario-set identity, and exogenous trajectory
provenance. `ProjectionRun` is a read model over that output; `augur/api`
adapts it into the frontend response shape. During migration the API may
expose a compatibility adapter for old consumers, but the durable seam
should be the clean simulation-run/projection contract, not the old
compatibility-table layout.

## Failure modes

A rollout that fails on month M continues running for months >M;
its `rollout_status[rollout=R].status` carries `"failed"` from
month M onward. Operations in subsequent months may still
materialize state and trajectory-derived values, but decision/event
emission filters on active rollouts where required. Failed rollouts
aren't structurally removed. Materialized outputs distinguish
"across all rollouts" from "across rollouts active at month X" at
projection time.

Recovery from failed back to active is intentionally out of scope
for the current simulator. A shortfall that is covered in the same
month by liquidity-policy sale proceeds never becomes a failure; an
unfunded required obligation fails the rollout.

## File and module layout

Flat over nested; one library per concern. Initial layout:

```
augur/sim/
  REQUIREMENTS.md                  — the spec
  DESIGN.md                        — this doc
  BUILD.bazel
  data/                            — checked-in YAML
    jurisdictions/{federal_us,california}.yaml
    locations/{san_francisco,palo_alto,san_mateo,oakland,sunnyvale}.yaml

  loader.py                        — load YAML → Pydantic, validate
  jurisdictions.py                 — Jurisdiction model + JurisdictionSet
  locations.py                     — Location model + LocationSet

  scenario.py                      — Scenario Pydantic model (+ Agent,
                                     Position, Liability, Property,
                                     Policy, ScheduledEvent submodels)
  market.py                        — consumer-side exogenous path
                                     interface; production sampling
                                     lives in augur/model

  state.py                         — StateCrossSection + schemas for
                                     the six working-state frames
  transactions.py                  — Transaction discriminated union +
                                     schemas for the six log frames

  templates_capital_gains.py       — per-template rules for
                                     capital-gains-eligible-holding
  templates_real_property.py       — per-template rules for
                                     depreciable-real-property
  templates_amortizing_loan.py     — per-template rules for
                                     amortizing-loan
  templates_tax_liability.py       — per-template rules for the
                                     tax-liability instrument
  templates_obligation.py          — recurring-obligation template
                                     + income-stream template
                                     (small enough to share a file)

  rules_capital_gains_class.py     — short-term vs long-term
                                     classification (one function;
                                     consumes any LTCG-eligible row)
  liquidity.py                     — policy sale decisions for hard
                                     demands and cash buffers
  settlement.py                    — all-or-none due-now settlement
  rules_year_tax.py                — per-jurisdiction year-tax
                                     computation (one function;
                                     parameterized by jurisdiction)

  step.py                          — the per-month step()
  simulate.py                      — the forward loop + outputs
                                     assembly

  projections.py                   — API/frontend read models over
                                     SimulationRun

  *_test.py                        — tests adjacent to each module
```

Each `templates_*.py` and `rules_*.py` is small (~50-200 LOC) and
imports only the frames + schemas it consumes. The step function
in `step.py` orchestrates them but does not contain rule logic.

## Implementation order

Each step lands a code commit + tests. Numbering matches the
REQUIREMENTS.md scenario layer it satisfies. Earlier steps
intentionally don't build infrastructure later steps need —
the design is sized so that L4's lot model doesn't require
refactoring L1's transfer.

1. **L1.1 + L1.2 + L1.3** — `cash_balances` frame, `Transfer`
   transaction, `Scenario` carrying initial cash, `simulate()`
   stub running the forward loop with no policies / market. One
   rollout, one month. Alice and Bob exist; Bob → Alice $5 ;
   state at month 1 shows the new balances. Tests assert balance
   math and the conservation invariant.
2. **L2** — multi-month loop, recurring transfer / income.
3. **L3** — rollout dimension goes from 1 to N. Same test in 2
   rollouts; same test in 100 rollouts; assert linear scaling.
4. **L4** — `asset_lots` frame with the lot model + FIFO lot
   selection. `AssetSale` transaction with lot consumption +
   `lot_dispositions_log`. The capital-gains-eligible-holding
   template + its rules (mark-to-market, sale, classification).
   HIFO, specific-id, and average-cost selection are later
   extensions.
5. **L5** — exogenous trajectory-bundle consumer interface;
   state-dependent decisions via polars expressions. Stochastic
   market sampling remains outside `sim`.
6. **L6** — quarterly + year-end tax obligations + safe harbor;
   the tax-liability-instrument template; per-jurisdiction
   year-tax computation; capital-gains classification rule.
   `tax_year_breakdown_log`.
7. **L7** — ordinary-income aggregation; SALT-cap interaction;
   itemized vs standard deduction; filing status (single only;
   HoH / MFJ / MFS deferred).
8. **L8** — `liabilities` frame; `amortizing_loan` template;
   mortgage origination / payment / payoff transactions; Bank Bob
   as non-tax-paying agent.
9. **L9** — agent policies (liquidity sale, asset preference
   chain, reinvest, monthly spend, variable spend
   from market). The `policy_decisions_log`.
10. **L10** — market-driven divergent rollouts from externally
    supplied paths; sellability masks (preparing for L14).
11. **L11** — `failure_events_log`; sticky rollout-failure status.
12. **L12** — `property_state` frame; `depreciable_real_property`
    template; property purchase / carrying costs / sale; §121
    exclusion; SALT cap interaction; qualified-residence interest
    cap.
13. **L13** — occupancy modes; depreciation; rental income +
    expenses; §1250 recapture; multi-property at multiple Bay
    Area locations.
14. **L14** — constrained sellability via sellability_mask
    (already wired in L10, scenarios exercising tender-only +
    mixed-liquidity).

The "Implementation order" map is a checklist, not a calendar.
Each step's deliverable is the smallest commit that passes that
layer's scenarios + adds the necessary code.

## What's not in this design

- **Concrete function signatures.** The step function and rule
  functions are described as "they exist and have this
  responsibility"; their argument lists firm up as the first
  commits land.
- **Concrete polars schema dtypes for every column.** The schemas
  are listed at the conceptual level; the production code's
  `pl.DataType` declarations land with the first frame.
- **Performance characterization.** The forward loop is
  vectorized by construction (per the requirements doc) but
  benchmarking against the legacy engine is deferred to after
  L4 / L8 land.
- **Wire compatibility with existing frontend graph tables.** A compatibility
  adapter can exist during migration, but
  the target is a better `model -> sim -> api` contract. Do not
  contort `sim` internals to match legacy response names.

These get decided when the code lands. The structural shape
above is what stays load-bearing through that work.
