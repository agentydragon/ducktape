# augur/sim

Deterministic, vectorized trajectory evaluator over typed scenarios and
sampled exogenous bundles. `sim` validates simulation inputs, applies
policies/events over materialized trajectories, records accounting truth, and
returns typed distribution results.

## Purpose

The library input is a `Scenario` evaluated over supplied exogenous paths;
`ScenarioKey` is the product/API's configuration language. `compile_run` in
<backend.py> takes the scenario, materialized paths, rollout count, jurisdiction
rules and locations, and returns the `CompiledRun` consumed by `RustEngine`.
It neither samples paths nor loads rules. Experiment callers can reuse one path
population across scenarios; the product service uses the same compilation entry
point. The result contains the execution input itself, not a dense plan plus the
original objects for a backend to reinterpret.

A selected one-rollout path is useful for UI inspection,
but it is one sampled trajectory from a distribution, not a separate
deterministic product API.

## Boundaries

- Shared asset identities live in `model/asset_key.py`. Financial metric
  composition and exact currency percentiles live in <metric_composition.py>
  and <quantiles.py>. Simulation/model definitions and the Rust adapter do not
  depend on product or HTTP modules; app projections consume these definitions.
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
obligation type. The actual stopped book is retained, including cash,
positions and liabilities. A failure in event month `f` ends with post-event
snapshot `f+1`, marked at the already-observed month `f`; no later books,
events or financial decisions are emitted. A stopped book is not
horizon-terminal wealth. Compact observation masks and aggregate population
bases are described in <../rust/docs/product_metrics.md>.

`cash_negative` remains a warning, not a terminal failure. It surfaces a
cash trajectory dip that wasn't caught by any obligation accrual. The open
design question is whether negative cash should be allowed at all in the
absence of explicit borrowing — see the Borrowing facilities entry in
`augur/TODO.md`. If/when added, a borrowing facility slots in as another
`FundingDecisionType` and a paired liability.

## See also

- <REQUIREMENTS.md>: simulator capability surface.
- <DESIGN.md>: current implementation architecture and invariants.
- <docs/tax_engine_evaluation.md>: tax engine build-vs-adopt evaluation.
- <TODO.md>: forward-looking sim/product follow-ups.
