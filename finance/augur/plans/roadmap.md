# Augur Roadmap

Last trimmed: 2026-06-12.

This is the priority map for active Augur work. Detailed public backlog items
live in `../TODO.md` and `../sim/TODO.md`; private values, holdings, property
data, deployment composition, and personal modeling assumptions stay downstream.

## North Star

Augur is a structured financial simulator:

- `augur/model` samples exogenous worlds with provenance.
- `augur/sim` deterministically evaluates typed scenarios over those worlds.
- `augur/api` serves compact read models for product views.

The app should not use a flat browser-side scenario row as the source of truth.
Frontend controls may be friendly, but they should compile into typed backend
objects: agents, accounts, assets, liabilities, obligations, lifecycle events,
policies, sampled exogenous paths, dense state buffers, event frames, and read
models.

## Standing Decisions

- **Simulation is nominal.** Keep accounting in nominal dollars; inflation-
  adjusted displays belong in read-model/postprocessing layers.
- **Path-indexed amounts are the recurring-dollar pattern.** Use `AmountSpec` /
  `SeriesIndexedAmount` for dollars that should follow an exogenous level.
- **Policy behavior is explicit typed data.** Add new policy families as typed
  simulator surfaces with tests; do not revive the old browser/backend
  actor-policy path.
- **State/event traces are the accounting source of truth.** Monthly arrays are
  chart/report views, not a parallel semantic model.
- **Private inputs stay private.** Public image layers contain generic Augur
  code and public-safe inputs only.

## Active Lanes

1. **ProjectionRun product cutover.** Replace rollout-detail `dense.decode()` /
   `SimulationRun` event extraction with native `ProjectionRun` or direct
   dense-buffer read models, then expand `//augur/api:server_test` assertions
   over event streams.
2. **Decision trace rows.** Surface no-op/rejected policy decisions such as
   "no PE sale because no opportunity" or "rejected because below floor" so
   trajectory inspection explains why nothing happened.
3. **Outside-rent timeline events.** Outside rent is user housing-cost state,
   not owned-property lifecycle state; changing or ending it should be explicit
   scenario state.
4. **Mid-horizon property purchase.** Product support for buying property after
   month 0.
5. **Tax surface expansion.** Next gaps are qualified dividends, capital losses
   and carryforward, passive-loss limitation/release, NIIT, filing statuses
   beyond single, section 121 nonqualified-use / reuse limits, SALT AGI
   phase-out, and sales-tax election.
6. **Property asset storage contract.** Replace YAML plus private nginx media
   sidecar with durable object/database-backed property assets while keeping
   large private media out of ConfigMaps and public images.
7. **UI cleanup.** Continue Mantine migration, normalize result labels
   (`liquid_net_worth`, `net_worth`, tender eligibility, selected-rollout
   percentiles), rework mortgage controls around standard products plus custom
   override mode, and add controls/inspection for modeled PE regime/event paths.
8. **Mortgage-rate path sampling.** Model mortgage-offer rates as sampled paths
   rather than one PMMS survey value at scenario time.
9. **Borrowing facilities.** Add overdraft, margin, or credit line as explicit
   funding sources in the obligation pipeline instead of immediately failing
   otherwise-unfunded hard demands.
10. **Model-governance artifacts.** Persist evidence, calibration, validation,
    generator/version, and path identity behind model outputs.
11. **Prediction-market calibration.** Keep using `augur/calibration`,
    `/api/calibration`, and the calibration tab to compare structured model
    marginals to markets. Current design/backlog lives in
    `prediction_market_calibration.md` and `whole_model_calibration.md`.
12. **Typed series / asset identity.** Finish deleting magic-prefix `wire_id`
    parsing by following `typed_series_config.md`.
13. **Partner/co-owner agreements.** Reintroduce "agent X pays agent Y this
    amount for this share/claim" only as a tested `augur/sim` agreement model,
    not as a scenario-wide enum.

## Guardrails

- Keep exogenous evidence config Pydantic-parsed at load time
  (`augur/fit/evidence_config.py`) and reject stale sim knobs at the file
  boundary.
- YAML-derived defaults should continue moving out of frontend literals and into
  deployment config.
- Refresh `augur/SPEC.md` after policy execution, tax timing, and result-view
  contracts stabilize.

## Verification Loop

Product API/frontend slices:

```bash
bbr test //augur/product:service_test //augur/api:server_test //augur:browser_shell_test
bbr test //augur:visual_test  # when rendering changed
```

Sim runtime/tax/property slices:

```bash
bbr test //augur/sim:simulate_test //augur/sim:test_rental_lifecycle_e2e //augur/sim:projections_test
```

Broader Augur spiral:

```bash
bbr test //augur/...
bbr build //augur/...
```

Private deployment slices also need the private Augur browser/backend tests and
live deployment verification after the public framework commit is repinned
downstream.
