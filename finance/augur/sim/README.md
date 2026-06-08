# augur/sim

Deterministic, vectorized trajectory evaluator over typed scenarios and
sampled exogenous bundles. `sim` validates simulation inputs, applies
policies/events over materialized trajectories, records accounting truth, and
returns typed distribution results.

## Purpose

The natural public unit is a `ScenarioKey` simulated over an exogenous
trajectory bundle. A selected one-rollout path is useful for UI inspection,
but it is one sampled trajectory from a distribution, not a separate
deterministic product API.

## Boundaries

- `augur/sim/`: validates typed simulation inputs, applies policies/events
  over materialized trajectories, records accounting truth, and returns
  typed distribution results. The durable simulation backend.
- `augur/model/`: evidence ingestion, calibration, fitting, and
  market-provider construction. Manifold/source-data shapes belong here as
  evidence that feeds fitting, not in app state or the simulator contract.
- `augur/api/`: catalog/default composition, request parsing, and
  app-specific validation. Adapts user-facing forms into model + simulation
  inputs and calls the simulator.
- `augur/frontend/`: browser UI bundle (React app, styles, lib).

## Invariants

- No `_enabled_policy_of()`-style singleton behavior execution. An actor
  policy program is an ordered sequence.
- Market paths and exogenous opportunities are observations, not policy
  decisions.
- Scheduled user events are explicit scenario transitions. The horizon
  itself is not an implicit property sale.
- Every cash/asset/liability/ownership/tax state change has a cause:
  `policy_id`, `event_id`, market opportunity, or system accounting process.
- Public result arrays reconcile to ledger/snapshot detail in e2e tests.
- Rollout health is machine-readable via `RolloutStatusType.status`.
  Structured details may point at failed obligations or first negative-cash
  months later, but do not add enum-like `status_reason` strings.

## Rollout Status And Failure Semantics

The unified obligation pipeline runs for every required cash demand (annual
tax, quarterly estimated tax, mortgage, property tax, HOA, insurance,
maintenance, outside rent, special assessment). A required obligation that
cannot be settled — even after the actor's funding policies have tried —
fires `FailureEvent` and flips the rollout to `RolloutStatusType.FAILED`.
The matching `test_e2e.py` `FAILED`-on-shortfall tests cover each
obligation type. After the failure boundary, state-backed balances,
holdings, liabilities, and net-worth metrics for that rollout freeze at
zero; the failure month remains on the rollout status so product/API
callers can distinguish failed trajectories from solvent zero-value
trajectories.

`cash_negative` remains a warning, not a terminal failure. It surfaces a
cash trajectory dip that wasn't caught by any obligation accrual. The open
design question is whether negative cash should be allowed at all in the
absence of explicit borrowing — see the Borrowing facilities entry in
`augur/TODO.md`. If/when added, a borrowing facility slots in as another
`FundingDecisionType` and a paired liability.

## See also

- <REQUIREMENTS.md>: simulator capability surface.
- <DESIGN.md>: structural decisions.
- <docs/tensorized_simulator.md>: rollout-axis tensorization design.
- <docs/tax_engine_evaluation.md>: tax engine build-vs-adopt evaluation.
- <TODO.md>: forward-looking sim/product follow-ups.
