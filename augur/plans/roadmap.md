# Augur Unified Plan

Last consolidated: 2026-05-27.

This plan consolidates the active Augur work from the public framework docs and
the private deployment notes. It is the priority ordering. `augur/TODO.md`
remains the detailed public backlog; private values, holdings, property data,
and deployment-specific composition stay in the downstream private repo.

## Sources

- `augur/SPEC.md`: product contract and simulator vocabulary.
- `augur/sim/README.md`: sim purpose, boundaries, invariants, and rollout
  failure semantics.
- `augur/sim/REQUIREMENTS.md` + `augur/sim/DESIGN.md`: simulator capability
  surface and structural decisions.
- `augur/sim/docs/tensorized_simulator.md`: rollout-axis tensorization design
  and invariants.
- `augur/sim/docs/tax_engine_evaluation.md`: tax engine build-vs-adopt
  evaluation.
- `augur/docs/rental_and_lifecycle.md`: current owned-property rental,
  primary-residence, lifecycle, rent-semantics, and known-gap notes.
- `augur/docs/prior_art_audit.md`: external architecture lessons for path
  identity, governance, policy projection, and accounting traces.
- `augur/sim/TODO.md`: forward-looking sim/product follow-ups.
- `augur/TODO.md`: public generic backlog.
- `gaffer-private/TODO.md`: private personal-finance modeling follow-ups.
- `gaffer-private/x/augur/SPEC.md`: private deployment boundary and image
  privacy contract.

## North Star

Augur simulates one product `ScenarioKey` (and, on the roadmap, a small set of
paired `ScenarioKey`s) across sampled exogenous paths and returns a distribution
over trajectories. A selected rollout seed is an inspection aid, not a separate
deterministic product. The UI, app state, and result APIs should make that
distinction impossible to miss.

The core model should stay structured around agents, accounts, assets,
liabilities, external series, typed policies, obligations, lifecycle events,
event frames, and state snapshots. The app may provide friendly controls, but
it should not use a flat browser-side "scenario row" as the source of truth and
then expand it back into typed backend objects.

The intended production backend path is `augur/model -> augur/sim -> augur/api`:
model providers sample exogenous levels/events with provenance, `augur/sim`
deterministically evaluates typed scenarios over those paths, and `augur/api`
serves compact projection/read models. The product metric-fan endpoint already
runs NumPy-direct from `DenseSimulationResult.buffers`
(`monthly_metric_arrays`). The rollout-detail endpoint still calls
`dense.decode()` on the selected R=1 dense result for event rows while emitting
monthly metric arrays directly. The next cutover is exposing native
`ProjectionRun` read models (`augur/sim/projections.py`) over product scenarios
instead of depending on long-form `SimulationRun` dataframe materialization.
Tracked under "Architecture / cutover" in `augur/sim/TODO.md`.

Standing guidance during continued cutover:

- **W-2 / non-rental ordinary-income translation is deferred (low
  priority, 2026-05-24).** Today's product scenarios are post-earning
  retirement projections rather than active wage-income scenarios. Rental
  income already flows through ordinary-income tax; add a W-2/product income
  knob only when a scenario requires earning during the horizon.
- **Path-indexed amounts.** Use sim `AmountSpec` / `SeriesIndexedAmount` for
  recurring dollars that should follow an exogenous level instead of adding
  more one-off product flags. Outside rent, landlord tenant rent,
  management/leasing fees, inflation-indexed spend, and inflation-indexed
  funding-policy buffers already use this shape. Product rent for owned
  property is full-property market rent before rented-fraction and vacancy
  scaling; see `augur/docs/rental_and_lifecycle.md`.
- **Nominal dollars through sim.** Backend/sim accounting stays in nominal
  dollars. Any inflation-adjusted display belongs in a postprocessing/read-model
  layer.
- **YAML-derived defaults.** Continue migrating bootstrap/UI defaults away from
  frontend literals and into deployment YAML; hide UI toggles for facts that
  should remain config-only (especially initial positions).

## Prior-Art Shape For Core Cleanup

The prior-art audit points to a conservative target shape:

- Exogenous path generation and household projection stay separate.
  `ExogenousSamplingRequest` plus `SampledExogenousBundle` is the durable
  economic scenario-generator boundary; the simulator is deterministic once it
  receives a typed scenario and sampled exogenous paths.
- Trajectory identity includes scenario input, exogenous model identity,
  evidence/calibration identity, generator implementation/version, seed, path
  index, and any non-exogenous event streams. `rollout_index` remains a
  convenient selector, not a reproducibility key by itself.
- Keep the runtime typed and phase-oriented until richer decisions justify a
  generic policy-program surface. New behavior should land as explicit scenario
  data, typed policies, typed events, and cause IDs rather than as ad-hoc
  browser-only state or untraceable balance mutation.
- Dense state/event buffers, decoded event frames, state snapshots, lots,
  liabilities, and typed cause IDs are the source of truth. Monthly arrays are
  chart/report views over that state, not a parallel semantic model. A
  double-entry ledger read model is future work, not the current contract.
- Model governance is part of the model output. Sampled bundles now carry first
  typed model/evidence/calibration/generator/path identities; the next cleanup
  is to persist real artifacts and validation results behind those IDs.

## Policy Runtime And Result Typing

The simulator compiles `Scenario` into dense phase tables and writes typed event
frames (`transfers`, `lot_dispositions`, `tax_accruals`, `tax_settlements`,
`obligation_accruals`, `obligation_settlements`, lifecycle markers, …) plus
state snapshots as the trace surface. Current policy-like inputs are explicit
typed data (`LiquidityPolicy`, `PrivateEquityTenderPolicy`,
`MortgageInterestDeductionPolicy`, `FederalSaltDeductionPolicy`, property-tax
policies, etc.), not a generic actor-policy interpreter.

Remaining work:

- Trace rows for **decisions that produced no event** — today the event frames
  record what actually happened, but a policy that decided "no sale because no
  opportunity" / "rejected because below floor" produces no row. Trajectory
  inspection needs those decisions visible.
- If a richer ordered policy-program surface becomes necessary, add it as a
  typed simulator surface with tests rather than reviving the deleted
  browser/backend actor-policy path.
- Keep exogenous paths and opportunities as observations, not policy decisions.

Private downstream work:

- Populate real cost bases for private holdings and taxable brokerage positions
  in the private repo.
- Model managed direct-index/tax-loss-harvesting behavior as a private
  deployment input once the generic position/tax hooks exist.

Acceptance criteria:

- Policy/phase order is explicit and testable.
- No policy family mutates balances without a typed event, state snapshot, or
  explicit accounting trace.
- Policy decisions (including no-op / rejected) are visible in trajectory
  inspection.

## Owned-Property Rental And Lifecycle

The old rental/lifecycle implementation plan is mostly shipped. The current
behavior doc is `augur/docs/rental_and_lifecycle.md`.

Current product nomenclature:

- `PropertyPurchase.initial_rental` starts landlord rental at purchase.
- `RentalIncomePlan.full_property_monthly_rent_usd` is full-property market
  rent before `fraction_rented`, vacancy, and management-fee calculations.
- `RentalManagement` wires management and leasing-fee cashflows.
- `PropertyPurchase.lifecycle_events` carries `set_rented_fraction`,
  `set_primary_residence`, `capital_improvement`, and `property_sale` events.

Current sim nomenclature:

- `ScheduledPropertyPurchase.rented_fraction` is only the initial rented share.
- `Scenario.initial_primary_residences` and
  `Scenario.primary_residence_events` are agent-scoped main-home assignments.
- `Scenario.property_lifecycle_events` handles rented-fraction changes, capital
  improvements, and property sales.

Shipped:

- Month-0 purchase, cash/mortgage financing, mortgage payments, property tax,
  HOA, insurance, maintenance, MID, SALT, and landlord tenant rent.
- Ordinary-income rental taxation, Schedule E deductions, §168 depreciation,
  runtime rented-fraction splits for Schedule E / MID / SALT, and §1250
  recapture on sale.
- Dynamic tenant-rent and agency-fee streams for mid-horizon
  `set_rented_fraction`.
- Primary-residence assignment and clear events for §121 eligibility.
- Property sale with market-value proceeds, mortgage payoff, closing costs,
  §1250 recapture, §121 exclusion, and remaining LTCG routing.

Remaining work:

- Product-level outside-rent timeline events. Outside rent is user
  housing-cost state, not owned-property lifecycle state, so changing or ending
  it should be explicit rather than inferred solely from primary-home
  assignment.
- Product-level mid-horizon property purchase.
- §121 nonqualified-use proration, one-sale-per-24-months tracking, and filing
  statuses beyond single.
- Stochastic tenant/vacancy/turnover modeling once the smoothed deterministic
  vacancy assumption becomes limiting.

## Property-Asset Storage Contract

`PropertySourceConfig` + `PropertyAssetConfig` cover the YAML side
(`properties_path` for the shortlist, `property_assets` for stable image URLs).
What's missing is a durable backing store:

- Durable property-asset storage backed by object storage or a database-like
  asset table — not just YAML + nginx sidecar.
- Keep large private media out of ConfigMaps. The current private nginx image
  is the expedient until the generic asset contract exists.
- Keep the generic Augur OCI image free of private config, property records,
  and private media.

Acceptance criteria:

- Public image layers contain only generic Augur code and public-safe inputs.
- Private deployments can supply config, property records, and media through
  runtime inputs without forking app logic.

## UI Cleanup

These should follow the result-shape and state-shape work so they don't polish
the wrong structure.

- Do not reopen the old `rentItOut` / ambiguous rental-rent surface. The
  current product control is `rentalFullPropertyMonthlyUsd`: full-property rent
  before fraction/vacancy/management scaling.
- Property lifecycle controls are now present for rented %, primary-home
  assignment, capital improvements, and sale. The next product gap is
  outside-rent timeline semantics, not adding more parallel boolean toggles.
- Continue the Mantine migration. `MantineProvider` wraps the product shell;
  controls are mixed Tailwind + Mantine. Standard controls (selects, number
  inputs, buttons, prefix/suffix adornments) should move to Mantine unless
  there's a documented reason not to.
- Add browser controls for `PrivateEquity` `LiquidityRegime` variants
  (`LiquidityEventOnly`, `PublicMarket`, `Acquisition`) instead of exposing only
  the tender-event PE shape.
- Normalize result labels around `liquid_net_worth`, `net_worth`, tender
  eligibility, and selected-rollout percentiles; avoid generic "liquidity"
  wording where it means PE sale opportunity or tender eligibility.
- Rework mortgage controls around standard mortgage products and explicit custom
  override mode.
- Refresh `augur/SPEC.md` after policy execution, tax timing, and result-view
  contracts stabilize.

## Next Lanes (parallelism + sequencing)

- **ProjectionRun product cutover** — replace rollout-detail `dense.decode()` /
  `SimulationRun` event extraction with native `ProjectionRun` or direct
  dense-buffer read models, then expand `//augur/api:server_test` assertions
  over event streams.
- **Outside-rent timeline events** — add product events for changing, starting,
  or ending outside rent so housing-cost cashflows are explicit scenario state.
- **Multi-scenario comparison** — reintroduce product comparison as a set of
  paired `ScenarioKey`s sharing one sampled exogenous bundle, with matched
  percentile fans and per-scenario controls.
- **Tax surface beyond the current ordinary/LTCG/STCG/MID/SALT/rental/property
  sale model** — qualified dividends, capital losses + carryforward, passive
  loss limitation/release, NIIT, filing statuses beyond single, §121
  nonqualified-use / reuse limits, SALT AGI phase-out, and sales-tax election.
- **`RegimeChange` mid-rollout events** — IPO converts `LiquidityEventOnly` →
  `PublicMarket`. The discriminated-union shape already supports static
  regimes; runtime needs to sample the event month and flip the variant instead
  of requiring the position's regime to be fixed for the whole horizon.
- **Mortgage-rate path sampling** — today the mortgage rate is a single PMMS
  survey number at scenario time; required-series introspection doesn't cover a
  `mortgage30:*` path. Adding it would let "what if rates fall to 5% in 18
  months" scenarios work.
- **Underpayment penalty on quarterly estimates** — IRS interest rate + 3% on
  shortfalls. Quarterly estimated payments and year-end true-up are in place;
  penalty math on underpaid quarters is not.
- **Borrowing facilities** — overdraft, margin, credit line as explicit funding
  sources in the obligation pipeline. Today unfunded hard demands mark the
  rollout failed; explicit borrowing would turn selected would-be failures into
  accounting-tracked liabilities paired with funding sources.
- **Persist model-governance artifacts** — durable evidence / calibration /
  validation-report storage for market providers. `augur/model/`. Self-contained,
  can run in parallel with anything.
- **Prediction-market calibration + model adjustments** — the structured exogenous
  model is scored against Manifold via `augur/calibration` + `/api/calibration` + the
  frontend calibration tab. The model-adjustment roadmap (M1 empirical IPO prior and M2
  valuation/dilution mark-coupling shipped; M2.2 stochastic dilution + evidence fit — staged
  A→D, M2.2-A per-rollout rate next — then M3 probabilistic IPO lockup and M4 calibration loop)
  lives in [`augur/plans/prediction_market_calibration.md`](prediction_market_calibration.md).
- **Reintroduce partner/co-owner agreements** after sim has a tested agreement
  model. "Agent X pays agent Y this amount over this period for this share/claim"
  should come back as a tested agreement model in `augur/sim`, not as a
  scenario-wide enum.

## Guardrail: Evidence Configuration Stays Typed At The Boundary

Keep the exogenous evidence config Pydantic-parsed at load time
(`augur/fit/evidence_config.py`), with `evidence_config_test` as the review
point when adding new source-data fields or deployment-supplied config. Reject
stale simulation knobs at the file boundary — `ExogenousSamplingRequest` owns
horizon, rollout seeds, and required series; the market config should not keep a
second inert copy.

## Verification Loop

For product API/frontend slices:

```bash
bbr test //augur/product:service_test //augur/api:server_test //augur:browser_shell_test
bbr test //augur:visual_test  # when rendering changed
```

For sim runtime/tax/property slices:

```bash
bbr test //augur/sim:simulate_test //augur/sim:test_rental_lifecycle_e2e //augur/sim:projections_test
```

Before handing off a broader spiral:

```bash
bbr test //augur/...
bbr build //augur/...
```

For private deployment slices, also run the private Augur browser/backend tests
and verify the live deployment only after the public framework commit is
repinned downstream.
