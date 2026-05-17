# Plan: Augur Simulator E2E Redesign

## Purpose

Augur's core simulator should be easier to operate and harder to misuse. We are
expanding e2e coverage in spirals: write a small core scenario, discover the
clunky/dead/weird API surface, fix that surface, then make the next spiral
larger.

The natural public unit is a `ScenarioSet` simulated over sampled market
trajectories. A selected one-rollout path is useful for UI inspection, but it is
one sampled trajectory from a distribution, not a separate deterministic product
API.

## Boundaries

- `augur/core/`: validates typed scenarios, applies policies/events over
  sampled rollouts, records accounting truth, and returns typed distribution
  results.
- `augur/model/`: evidence ingestion, calibration, fitting, and market-provider
  construction. Manifold/source-data shapes belong here as evidence that feeds
  fitting, not in app state or the simulator contract.
- `augur/api/`: catalog/default composition, request parsing, and
  app-specific validation. It adapts user-facing forms into core scenarios and
  calls core.
- `augur/frontend/`: browser UI bundle (React app, styles, lib).

## Target Runtime Shape

1. A scenario declares actors, initial accounts/assets/liabilities, scheduled
   events, market request, and each actor's ordered policy program.
2. The engine initializes vectorized rollout state: cash/accounts, asset
   units/value/basis, liabilities, ownership ledgers, tax state, property use,
   and policy memory.
3. Each month, policies receive typed context: actor, month, current state,
   relevant scheduled events, market observations, and available opportunities.
4. Policies emit typed decisions/instructions. They do not directly mutate
   cash, holdings, basis, liabilities, ownership, taxes, or result arrays.
5. Cash-demanding processes emit first-class obligations before mutating cash:
   actor, amount, due month/date, cause, creditor/counterparty, and relevant
   source rows. Actor policy gets a chance to fund each obligation by using
   cash, selling assets, borrowing, or explicitly declining/being unable to act.
6. Accounting appliers validate and apply instructions, update state, record
   ledger entries and balance snapshots, and record any shortfall/rejection.
   Obligation settlement then records paid, partially paid, unpaid, or failed.
7. Reporting arrays are derived from state/ledger or reconciled against them.
   Arrays can stay for chart performance, but they are not the source of truth.

## Invariants

- No `_enabled_policy_of()`-style singleton behavior execution. An actor policy
  program is an ordered sequence.
- Market paths and exogenous opportunities are observations, not policy
  decisions.
- Scheduled user events are explicit scenario transitions. The horizon itself
  is not an implicit property sale.
- Every cash/asset/liability/ownership/tax state change has a cause:
  `policy_id`, `event_id`, market opportunity, or system accounting process.
- Public result arrays reconcile to ledger/snapshot detail in e2e tests.
- Rollout health is machine-readable via `RolloutStatusType.status`. Structured
  details may point at failed obligations or first negative-cash months later,
  but do not add enum-like `status_reason` strings.

## Rollout Status And Failure Semantics

The unified obligation pipeline now runs for every required cash demand
(annual tax, quarterly estimated tax, mortgage, property tax, HOA, insurance,
maintenance, outside rent, partner contribution, special assessment). A
required obligation that cannot be settled — even after the actor's funding
policies have tried — fires `FailureEvent` and flips the rollout to
`RolloutStatusType.FAILED`. The matching `test_e2e.py` `FAILED`-on-shortfall
tests cover each obligation type.

`cash_negative` remains a warning, not a terminal failure. It surfaces a
cash trajectory dip that wasn't caught by any obligation accrual. The open
design question is whether negative cash should be allowed at all in the
absence of explicit borrowing — see the Borrowing facilities entry in
`augur/TODO.md`. If/when added, a borrowing facility slots in as another
`FundingDecisionType` and a paired liability.

## Active Step 7: Arrays Reconcile To Ledger

Goal: monthly columns remain charts, not truth. Keep shrinking bespoke
explanatory array math without changing monthly-column semantics.

Next slices:

1. Keep true state snapshots, such as cash, public asset value, private-equity
   mark value, tender-eligible private-equity value, property value, mortgage
   balance, home-equity claims, ownership percentage, and net-worth metrics,
   sourced from state snapshots rather than transaction ledger rows.
2. Derive remaining transaction-flow arrays from ledger rows where practical.
   The likely next targets are purchase-closing costs, property depreciation,
   partner house-cost/share explanation columns, and tax payment timing once the
   tax ledger/liability shape exists.
3. Move remaining explanatory arrays toward typed accounting detail once their
   semantics are explicit enough. These arrays explain calculations; they should
   not pretend to be cash movement unless there is a corresponding ledger row.
4. Generalize the ledger-derived matrix helper only when the next family needs
   multiple categories, actor filters, property filters, or balance snapshots.
5. Keep existing monthly columns stable and keep reconciliation tests as
   guardrails while the implementation source changes.
6. Add any missing causes/IDs needed by derivation. Do not add ad hoc string
   parsing to recover meaning from categories.

### Step 7 Array Inventory

This inventory records the intended source of truth for existing monthly result
arrays. Compatibility aliases can remain while app/UI callers still expect
them, but their source should be another state/ledger-backed metric.

- State snapshots: `cash_usd`, `generic_sp500_value_usd`,
  `private_equity_value_usd`, `private_equity_sale_opportunity_value_usd`,
  `property_value_usd`, `mortgage_balance_usd`, `home_equity_usd`,
  `owner_home_equity_claim_usd`, `partner_home_equity_claim_usd`,
  `partner_equity_ledger_usd`, `owner_equity_ledger_usd`,
  `partner_ownership_pct`, `liquid_net_worth_usd`, `net_worth_usd`,
  `partner_present`. `private_equity_sale_opportunity_value_usd` is a
  tender-eligibility/opportunity snapshot, not a liquid asset; current
  `liquid_net_worth_usd` is cash plus public-liquid securities and must not
  include tender-eligible private marks.
- Market-observation compatibility fields: `private_equity_sale_opportunity_event`.
  Prefer row-level market observations for detailed inspection.
- Ledger-backed transaction flows: `monthly_spend_usd`,
  `generic_sp500_sale_usd`, `generic_sp500_sale_basis_usd`,
  `generic_sp500_sale_tax_usd`, `private_equity_sale_usd`,
  `private_equity_sale_basis_usd`, `private_equity_sale_tax_usd`,
  `mortgage_interest_usd`, `mortgage_principal_usd`,
  `mortgage_payment_usd`, `property_tax_usd`, `hoa_usd`, `insurance_usd`,
  `maintenance_usd`, `rental_income_usd`,
  `rental_management_fee_usd`, `rental_leasing_fee_usd`,
  `sale_closing_cost_usd`, `property_sale_gross_usd`,
  `property_sale_net_proceeds_usd`, `property_sale_tax_usd`,
  `property_sale_debt_payoff_usd`, `partner_contribution_usd`,
  `partner_contribution_used_usd`, `partner_unallocated_excess_usd`,
  `partner_principal_credit_usd`.
- Ledger-backed rollups and aliases: `generic_sp500_sale_gain_usd`,
  `checking_floor_action_usd`, `property_carrying_cost_usd`,
  `net_property_cash_flow_usd`, `net_property_sale_cash_flow_usd`.
- Accounting-detail-backed explanatory calculations:
  `federal_income_tax_usd`, `california_income_tax_usd`,
  `total_income_tax_usd`,
  `property_sale_adjusted_basis_usd`, `realized_property_gain_usd`,
  `property_sale_capital_gain_usd`,
  `property_sale_capital_gain_exclusion_usd`,
  `taxable_property_capital_gain_usd`, `taxable_property_gain_usd`,
  `depreciation_recapture_usd`.
- Explanatory calculations that still need typed accounting detail:
  `purchase_closing_cost_usd`, `property_depreciation_usd`,
  `cumulative_property_depreciation_usd`, `partner_house_costs_usd`,
  `owner_principal_credit_usd`, `partner_house_cost_share`.
- Policy/result compatibility fields: `checking_floor_shortfall_usd`. Keep
  policy-decision rows as the detailed source for why a shortfall occurred.

## Open Design Follow-Ups

Current open follow-ups live in `augur/TODO.md`. Keep this plan focused on the
Step 7 array-source inventory, the app-state spiral below, and the e2e
verification loop.

## App-State Schema-Driven Boundary (achieved guardrail)

The OpenAPI/Zod pipeline is wired and live: `client.js` validates every API
response, `decodeScenarioSetUrlState` validates URL state against the overrides
schema, and `serializableScenarioSetInput` projects through
`zBrowserScenarioSetInputOverridesInput` (#1581) so adding a new
Pydantic field propagates to the browser without a hand-maintained field
list. **Guardrail**: do not reintroduce hand-maintained schema lists or
ad hoc boundary checks. Pydantic stays the single source of truth.

## Verification

After each behavioral slice:

```bash
bbr test //augur/core:test_e2e
```

Before handing off a finished spiral:

```bash
bbr test //augur/core:all
bbr build //augur/...
```

If app-facing request or state conversion changed, also run the relevant
`augur/frontend` JavaScript and `augur/api` backend tests.
