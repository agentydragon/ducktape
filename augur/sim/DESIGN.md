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
   their policies (funding chain order, sale rules, spending),
   their scheduled events (property purchases, sales, occupancy
   switches), and the market-bundle reference. Validated at
   construction time.

3. **Market bundle** (external, lazy / sampled). Per-rollout
   per-month paths for every market driver the scenario consumes:
   asset unit-price multipliers, property-value multipliers per
   location, sellability masks for constrained-liquidity
   positions, spending-variance paths, rental-income variance
   paths, tender-opportunity masks. Same input-output contract as
   the existing augur market bundle, adapted to template-id-keyed
   lookups.

4. **Working state — the canonical state-over-time long-form
   frames** (polars). Six frames, all keyed by `(rollout_index,
   month_index)` plus the entity-id columns for each kind:
   - `cash_balances` — `(rollout, month, agent_id, account_id,
     balance_usd)`
   - `asset_lots` — `(rollout, month, agent_id, position_id,
     lot_id, template_id, units, basis_usd, acquired_month,
     cost_basis_method, sellability_mask_ref)`
   - `liabilities` — `(rollout, month, agent_id, liability_id,
     template_id, counterparty_agent_id, principal_usd,
     interest_accrued_this_month_usd, principal_paid_this_month_
     usd, deductibility_flag, …)`
   - `property_state` — `(rollout, month, property_id, location_
     id, occupancy_mode, current_market_value_usd, adjusted_
     basis_usd, cumulative_depreciation_usd, primary_residence_
     use_months_within_window, owned_since_month, …)`
   - `property_stakes` — `(rollout, month, agent_id, property_id,
     ownership_pct, contribution_used_usd, equity_ledger_usd)`
     — single-row-per-property for single-owner; multi-row for
     partner-equity stretch.
   - `rollout_status` — `(rollout, month, status, failure_event_
     id, failure_month)`

   These frames **grow forward** as the loop advances: at month M
   the frames have rows for months `0..M`. The state at month M is
   `frame.filter(month_index == M)`; the per-rollout net worth
   trajectory at agent A is `frame.filter(agent_id == A).group_by
   (rollout_index, month_index).agg(...)`. The state-over-time IS
   the output — there is no separate "snapshot matrices" layer
   built post-hoc.

5. **Append-only logs** (polars, accumulated). The ledger of what
   happened:
   - `transactions_log` — every transaction (transfer, sale, tax
     payment, etc.) chronologically with cause-id linking back to
     the policy or obligation that produced it.
   - `obligations_lifecycle_log` — every obligation accrual +
     settlement with amount_due, amount_paid, status.
   - `lot_dispositions_log` — one row per consumed lot per sale:
     `(rollout, month, agent_id, position_id, lot_id, units_sold,
     proceeds_usd, basis_consumed_usd, realized_gain_usd, holding_
     period_days, tax_classification)`.
   - `failure_events_log` — every unfunded required obligation.
   - `policy_decisions_log` — every decision a policy made or
     considered (including "did not fire because condition not
     met"), for diagnostics.
   - `tax_year_breakdown_log` — per `(rollout, agent, year,
     jurisdiction)` the income totals, deduction amounts, bracket
     walks, NIIT calc, totals.

The boundary discipline: layer 1 is law-and-place data, edited
when reality changes. Layer 2 is what the user wants to simulate.
Layer 3 is exogenous market input. Layer 4 IS the simulation —
state evolves forward. Layer 5 is the audit trail of how state
got from its prior value to its current value. Nothing in layer 4
is computed from layer 5 post-hoc (that's the legacy engine's
smell); nothing in layer 5 is needed to compute layer 4 (forward
loop only reads state).

## The forward loop

```
state_t = StateCrossSection.initial(scenario, market, rollout_count)
append(state_t → state_frames at month 0)

for month in range(horizon_months):
    step_result = step(
        state=state_t,
        market=market.at(month),
        jurisdictions=jurisdiction_set,
        scenario=scenario,
        month=month,
    )
    state_t = step_result.next_state          # state at month+1
    append(state_t → state_frames at month+1)
    extend(step_result.transactions → transactions_log)
    extend(step_result.obligations → obligations_lifecycle_log)
    extend(step_result.lot_dispositions → lot_dispositions_log)
    extend(step_result.failure_events → failure_events_log)
    extend(step_result.policy_decisions → policy_decisions_log)
    extend(step_result.tax_year_breakdown → tax_year_breakdown_log)

return SimulationRun(
    state_frames=concat_state_frames(...),
    logs=concat_logs(...),
)
```

The simulator does not re-derive any prior month. Each `state_t`
is the polars cross-section (no `month_index` column inside the
step body — `state_t` is "the rollouts at one fixed month"); the
loop tags it with `month_index = M` when appending to the
forward-growing frames.

## The step function — six phases

`step(state, market, jurisdictions, scenario, month) → StepResult`.
Phases run in this fixed order. Each phase is one or more polars
expressions over the rollout column; no Python iteration over
rollouts; each phase produces transaction rows + an updated state
cross-section.

1. **Mark-to-market.** Refresh asset unit prices and property
   values from this month's market path. Pure read; no
   transactions. Updates `asset_lots.unit_price_usd` (derived
   column) and `property_state.current_market_value_usd`. Also
   refreshes `asset_lots.sellability_at_this_month` from the
   per-position sellability_mask_ref.

2. **Scheduled events.** Apply scenario-scheduled events whose
   month equals this month:
   - Property purchase event: instantiate a property_state row,
     transfer down-payment + buy-closing-costs from agent cash,
     originate the associated mortgage liability (a new
     liabilities row).
   - Property sale event: compute proceeds (current value − sale
     closing costs), pay off any outstanding secured mortgage to
     the counterparty agent's cash, transfer net proceeds to
     seller, retire the property_state and liability rows (mark
     `occupancy_mode = sold`), record the realized gain on the
     lot-dispositions log.
   - Occupancy-mode switch (primary → rental, rental → primary,
     etc.): flip the property_state.occupancy_mode flag at this
     month, which starts / stops depreciation accrual in phase 5
     and starts / stops the rental-income stream in phase 3.
   - Income arrival: W-2 paychecks, rental rent (per the
     property's rental policy), classified for tax purposes.
   - Recurring-obligation accruals due this month: property tax,
     HOA, insurance, maintenance, special assessment if due,
     mortgage payment, monthly spend, outside rent. Each accrues
     an obligations_lifecycle row with status `accrued`.
   - Tax-obligation accruals at marker months: quarterly
     estimated tax (Apr 15, Jun 15, Sep 15, Jan 15 of next year)
     based on safe-harbor of prior-year tax; year-end true-up at
     Dec of each tax year based on the year's actual tax minus
     estimated paid so far. Per-agent per-jurisdiction.

3. **Settle required obligations.** For every obligation row with
   status `accrued` and required `true` (mortgage, property tax,
   HOA, insurance, maintenance, tax — recurring and one-off —
   special assessment, outside rent, partner contribution if in
   scope), settle:
   1. Pay from the agent's checking cash if sufficient.
   2. If short, walk the agent's funding chain (sell from
      configured asset-position preference order). Each sale
      consumes lots per the position's cost-basis method, emits
      lot-disposition rows, updates asset_lots units/basis, and
      credits the agent's cash. Stop selling as soon as the
      obligation is funded.
   3. If still short after the chain, emit a failure_events row
      and flip the rollout's status to `failed` for this month
      (which persists for subsequent months until a configured
      recovery, see S11.3 — phase 3 of a later month succeeds
      and the status flips back to `active`).

   Settlement order within phase 3 is fixed per agent: tax →
   mortgage → property carrying costs → outside rent → partner
   contribution → monthly spend. This is the existing engine's
   priority and not currently parameterized; document it.

4. **Discretionary policies.** Each agent's configured policies
   evaluate against the post-settlement state:
   - Floor-triggered sale ("if checking < $X, sell $Y of position
     Z"): a polars expression that masks the rollouts where the
     condition holds and produces sale transactions for the masked
     subset.
   - Reinvest-excess ("if checking > $X, buy $Y of position Z").
   - Any other configured policy.

   Each policy emits a policy_decisions row for every rollout
   (whether or not it fired); the rows that fired also produce
   transactions.

5. **End-of-month accruals.** State-only updates that don't move
   cash:
   - Depreciation tick on every property in `rental` occupancy
     mode: `cumulative_depreciation_usd += monthly_depreciation
     = building_portion_of_basis / (27.5 * 12)` per the
     jurisdiction's residential-rental schedule.
   - Mortgage liability principal-balance roll: next-month's
     principal balance computed from this month's amortization
     (already captured in the mortgage-payment transaction at
     phase 3; this is just the forward-projection bookkeeping if
     anything is needed).
   - §121 use clock tick: `primary_residence_use_months_within_
     window` increments by 1 for properties currently in
     `primary_residence` mode; decrements the oldest month
     falling off the 60-month window. (Or computed on demand at
     sale; see decision below.)
   - Asset lot ages increment by 1 (or derived on demand from
     `acquired_month` vs current month at sale time — same
     answer, derived-on-demand is simpler).

6. **Construct next state cross-section.** Assemble all the
   updates above into the `state_{M+1}` cross-section. Append-
   ready for the loop's frame-extension step.

The returned `StepResult` carries:
- `next_state`: the new cross-section.
- `transactions`: all transactions produced this month (one row
  per transaction, schema below).
- `obligations`: every accrued / settled / unpaid obligation row
  for this month.
- `lot_dispositions`: rows for any sales this month.
- `failure_events`: rows for any unfunded required obligations.
- `policy_decisions`: rows for every policy evaluation this month.
- `tax_year_breakdown`: rows when the year-end tax computation
  fires (December of each year + scenario horizon end).

## Transaction taxonomy

Every state mutation goes through one of a small set of
transaction kinds. Discriminated union; balance-checked at
construction; each row on the `transactions_log` carries the kind
plus the kind-specific columns.

Cash-side balance invariant: every transaction's `Σ(agent_cash_
delta_usd) == 0` (money moves between agents and the sink-agent
representation of an external counterparty; see lender note in
REQUIREMENTS.md).

Transaction kinds (the full set; not every kind is needed at
every layer):

- **Transfer** — cash moves between two agents' accounts. The
  primitive that S1.1 uses. May carry an income classification on
  the receiving side.
- **Asset purchase** — cash leaves an agent's account, a new lot
  appears on the asset_lots frame for that agent. (For market
  purchases; the counterparty is the market sink.)
- **Asset sale** — units of one or more lots are consumed (per the
  position's cost-basis method); cash arrives. Realized gain is
  derived from the lot dispositions, not carried separately on
  the transaction itself.
- **Property purchase** — composite: cash leaves (down payment +
  buy closing), property_state row appears, mortgage liability
  originates (linked to a separate Transfer-like cash-flow between
  the lender's cash and the purchaser's cash for the loan principal,
  which immediately routes back out as part of the down + balance
  going to seller).
- **Property sale** — composite: closing cost out, mortgage payoff
  out, net proceeds to seller, property_state retires.
- **Mortgage payment** — composite: borrower's cash out (P+I);
  lender's cash in; borrower's mortgage liability principal
  decreases by P; the I portion is recorded for the borrower's
  qualified-residence-interest-paid tally and the lender's
  interest-income tally (lender is not-tax-paying per scope, but
  the row is recorded for symmetry).
- **Obligation settlement** — composite of "cash out + obligation
  status flips to `paid`". When the obligation is a recurring
  charge like property tax that has no specific counterparty
  agent, the cash exits to a designated sink representing the
  external counterparty (taxing authority, HOA, insurance
  company). The sink is not modeled beyond being the recipient of
  the funds.
- **Tax accrual** — non-cash: a TAX_PAYABLE liability appears on
  the agent's books at year-end; an offsetting tax-expense
  classification is recorded for that year. Net effect: the
  agent's net worth shows the tax liability accrued but not paid.
- **Tax payment** — quarterly estimated payment or year-end
  true-up. Cash out, tax_payable balance decreases.
- **Income arrival** — recurring cash inflow with a tax-
  classification label (W-2 ordinary, rental income, etc.).
- **Depreciation accrual** — non-cash: property's `cumulative_
  depreciation_usd` increases; the year's tally for the property's
  owner accumulates for net-rental-income computation. Not on the
  transactions log proper (it's not a cash event) but recorded
  for audit.

The "composite" transactions (Property purchase / sale, Mortgage
payment, etc.) produce multiple rows on the transactions_log —
one row per atomic balance-checked cash/asset movement, all
sharing a `cause_id` linking them as one logical event. The
materialize step can group by cause_id to produce a coarse-grained
view (e.g., "this is one mortgage payment with these P+I splits")
when the user wants that.

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
- A small set of **projections** are computed on demand from the
  state-over-time:
  - Net-worth time series per `(rollout, agent)`: cash + sum of
    asset market value − sum of liability principal.
  - Total income tax per `(rollout, year)`: sum across agents
    across applicable jurisdictions from the tax_year_breakdown
    log.
  - Realized vs unrealized capital gains per `(rollout, year)`:
    realized from lot dispositions, unrealized from state-over-
    time.

The returned `SimulationRun` is a Pydantic object wrapping the
state-over-time frames + the log frames + the projections. The
wire / API shape adapts these to the existing `ScenarioRunArrays`
contract — projections expose specific named columns from
state-over-time at materialize time.

## Failure modes

A rollout that fails on month M continues running for months >M;
its `rollout_status[rollout=R].status` carries `"failed"` from
month M onward (until recovery flips it back). Operations in
phases 2-5 in subsequent months continue to apply over all
rollouts — failed ones aren't structurally removed. Materialized
outputs distinguish "across all rollouts" from "across rollouts
active at month X" at projection time.

A rollout that recovers (a later month's funding chain succeeds
where a prior month's didn't) flips `status` back to `"active"`.
The failure_events log retains the original failure row; the
status frame tells the recovery story.

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
  market.py                        — MarketBundle interface

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
  rules_funding_chain.py           — obligation funding chain
                                     (one function; consumes any
                                     unfunded obligation)
  rules_year_tax.py                — per-jurisdiction year-tax
                                     computation (one function;
                                     parameterized by jurisdiction)

  step.py                          — the per-month step()
  simulate.py                      — the forward loop + outputs
                                     assembly

  net_worth.py                     — net-worth projection from state
  tax_breakdown.py                 — tax-year-breakdown projection

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
4. **L4** — `asset_lots` frame with the lot model + cost-basis
   methods. `AssetSale` transaction with lot consumption +
   `lot_dispositions_log`. The capital-gains-eligible-holding
   template + its rules (mark-to-market, sale, classification).
5. **L5** — `MarketBundle` interface; state-dependent decisions
   via polars expressions.
6. **L6** — quarterly + year-end tax obligations + safe harbor;
   the tax-liability-instrument template; per-jurisdiction
   year-tax computation; capital-gains classification rule.
   `tax_year_breakdown_log`.
7. **L7** — ordinary-income aggregation; SALT-cap interaction;
   itemized vs standard deduction; filing status (single + HoH).
8. **L8** — `liabilities` frame; `amortizing_loan` template;
   mortgage origination / payment / payoff transactions; Bank Bob
   as non-tax-paying agent.
9. **L9** — agent policies (floor-triggered sale, asset
   preference chain, reinvest, monthly spend, variable spend
   from market). The `policy_decisions_log`.
10. **L10** — market-driven divergent rollouts; sellability
    masks (preparing for L14).
11. **L11** — `failure_events_log`; rollout status flips;
    recovery semantics.
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
- **Wire compatibility with the existing `ScenarioRunArrays`
  schema.** A compatibility shim exists conceptually (projection
  layer at simulate()'s output), but the actual mapping is a
  late-stage concern when the sim engine is being swapped in.

These get decided when the code lands. The structural shape
above is what stays load-bearing through that work.
