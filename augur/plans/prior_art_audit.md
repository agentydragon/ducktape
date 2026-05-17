# Augur Prior Art Audit

## Executive Summary

Augur is closest to a dynamic household microsimulation and personal-finance
projection engine: it samples exogenous market paths, then projects typed
scenarios through deterministic actor policies, accounting applications, and
result views. In financial-risk terms, `MarketRequest` plus `MarketBundle` is
an economic scenario generator input/output, `ScenarioSet` is the portfolio of
household scenario variants, `rollout_index` selects one sampled path, and the
policy runtime is the pathwise deterministic projector.

The important gaps the rest of this audit explores:

- `rollout_index` is not a sufficient trajectory identity. Reproducibility needs
  IDs backed by persisted evidence/calibration artifacts, generator versions,
  scenario inputs, and any non-market event streams.
- Policy execution traces need richer coverage for no-op, rejected, instructed,
  and applied decisions as policy families grow.
- Cash-under-zero and failed-rollout semantics should generalize beyond the
  annual-tax obligation slice to mortgages, other cash demands, explicit
  credit/default state, and continued-vs-terminated projection behavior.
- Ledger and accounting detail are valuable but not yet accounting-grade.
  Postings are still stringly typed `domain`/`category` rows rather than
  balanced journal entries with typed accounts, lots, liabilities, cause ids,
  and reconciliation invariants.
- Model governance has a first typed surface but is not yet decision-grade:
  validation is placeholder-level and evidence/calibration artifacts are not
  persisted reviewed records.
- Calibration/evidence and projection boundaries should be explicit: evidence
  feeds model fitting; fitted scenario generators feed `MarketBundle`; the core
  projector should not know source-specific evidence.

## Local Architecture Grounding

This audit is grounded in the current Augur files:

- `augur/plans/roadmap.md` and `augur/TODO.md`: current priorities emphasize
  distribution-first results, trajectory inspection, typed result panels,
  ordered policy programs, ledger reconciliation, cash-under-zero semantics,
  trajectory identity, and evidence/model boundary cleanup.
- `augur/SPEC.md`: declares Augur as a probabilistic multi-agent economic
  simulator with scenarios, markets, policies, actions, and per-rollout state.
- `augur/core/scenario_set.py`: defines `ScenarioSet`, `Scenario`,
  `MarketRequest`, policies, actions, policy decisions, market observations,
  ledger entries, balance snapshots, accounting details, and result payloads.
- `augur/core/market_bundle.py`: defines `MarketBundle` and
  `MarketBundleMetadata` with sampled rollout/month arrays for inflation,
  SP500, home/rent paths, mortgage rates, private-equity values, and
  private-equity sale opportunities.
- `augur/core/api.py`: exposes `simulate_set()`, `SimulationRun`,
  `ScenarioRun`, and `RolloutDetail`, with path inspection helpers for series,
  actions, decisions, observations, ledgers, snapshots, and accounting details.
- `augur/core/policy_runtime.py`: defines `ActorPolicyProgram`, instruction
  batches, application results, ledger batches, and partner-ownership accrual.
- `augur/core/scenario_engine.py`: performs vectorized projection over a
  `MarketBundle`, records row-level trace data, derives many arrays from ledger
  and accounting details, and executes policies through ordered actor programs.
- `augur/model/`: contains evidence loading, market model protocols,
  historical series, simulated scenario arrays, and `MacroMarketBundleProvider`.

## Prior-Art Catalog

### QuantLib

Source: [official QuantLib docs](https://www.quantlib.org/docs.shtml),
[PathGenerator source](https://codebrowser.dev/quantlib/quantlib/ql/methods/montecarlo/pathgenerator.hpp.html),
[PathPricer docs](https://quantlib.js.org/docs/classes/_ql_methods_montecarlo_pathpricer_.pathpricer.html),
and [McSimulation docs](https://quantlib.js.org/docs/classes/_ql_pricingengines_mcsimulation_.mcsimulation.html).

QuantLib is a derivatives library, not a household simulator, but its Monte
Carlo architecture is relevant. The core pattern is clean separation of:

- stochastic process and path generation;
- a time grid and random sequence generator;
- path pricing or pathwise deterministic evaluation;
- sample accumulation and error/statistics reporting.

Augur should copy the separation, not the instrument domain. `MarketBundle`
should remain path generation output. The household policy/accounting projector
should be deterministic conditional on a selected path bundle.

### Open Source Risk Engine (ORE)

Sources: [ORE GitHub README](https://github.com/OpenSourceRisk/Engine),
[ORE documentation overview](https://opensourcerisk.org/documentation/), and
[ORE scenario reference](https://www.opensourcerisk.org/docs/orea/group__scenario.html).

ORE demonstrates how a production risk system splits trade/portfolio input,
market data, application configuration, simulation configuration, risk-factor
evolution, scenario generation, exposure simulation, stress scenarios,
sensitivity scenarios, historical scenarios, and generated reports. Its scenario
module names useful concepts for Augur: scenario generator, scenario path
generator, scenario data, scenario sim market, risk-factor key, stress scenario,
historical scenario, and aggregation scenario data.

Augur is not pricing derivatives, but ORE is useful prior art for path identity,
scenario metadata, market data provenance, stress/path replay, and auditability.

### OpenFisca

Sources: [OpenFisca key concepts](https://openfisca.org/doc/key-concepts/index.html),
[tax and benefit systems](https://openfisca.org/doc/key-concepts/tax_and_benefit_system.html),
[variables and formulas](https://openfisca.org/doc/key-concepts/variables.html),
[parameters](https://openfisca.org/doc/key-concepts/parameters.html),
[simulation](https://openfisca.org/doc/key-concepts/simulation.html), and
[reforms](https://openfisca.org/doc/key-concepts/reforms.html).

OpenFisca is static tax-benefit microsimulation, so it does not directly solve
Augur's pathwise dynamic projection. Its rule-engine vocabulary is still highly
relevant:

- entities such as person, household, or tax unit;
- variables with value type, entity, definition period, formulas, labels,
  references, units, and defaults;
- parameters as time-varying legal/economic data rather than hardcoded numbers;
- reforms as modifications to a reference system;
- simulations as caches of input data and computed results;
- calculation tracing.

Augur should use the OpenFisca pattern for tax/regulation/policy parameters and
calculation traceability, while keeping dynamic behavior and market paths
outside OpenFisca's static assumptions.

### PolicyEngine

Sources: [PolicyEngine Core introduction](https://policyengine.github.io/policyengine-core/intro.html)
and [PolicyEngine US docs](https://policyengine.github.io/policyengine-us/).

PolicyEngine, a fork of OpenFisca Core, highlights a clean split between the
generic microsimulation framework and country-specific logic, parameters, and
data. The framework calculates variables for periods and can trace computation
trees; the country packages define entities, variables, parameters, and data.

For Augur, the transferable pattern is not the tax-benefit scope. It is the
boundary between:

- framework/runtime code;
- domain rules and parameters;
- deployment-specific/private data;
- inspectable calculation traces.

### Tax-Calculator and PSL

Source: [Tax-Calculator docs](https://taxcalc.pslmodels.org/index.html),
[Data for Tax-Calculator](https://taxcalc.pslmodels.org/usage/data.html),
[CLI guide](https://taxcalc.pslmodels.org/guide/cli.html), and
[Parameters API](https://taxcalc.pslmodels.org/api/parameters.html).

Tax-Calculator is a public federal tax microsimulation model. Relevant patterns:

- `Records` carries filing-unit data;
- `Policy` carries parameterized law;
- `Calculator` combines records, policy, and assumptions;
- reforms and assumptions are serialized rather than coded into UI cells;
- prepared sample data and custom records are both supported;
- tests include unit, integration, and cross-model validation against TAXSIM;
- reports should cite release/version and replication materials.

For Augur, this points toward explicit scenario input records, policy parameter
sets, model/release identity, and reproducibility materials for any result that
will guide real financial decisions.

### Dynamic Microsimulation Literature

Sources: [A survey of dynamic microsimulation models](https://www.microsimulation.pub/articles/00082),
[Dynamic Microsimulation for Policy Analysis](https://microsimulation.pub/articles/00256),
and [Challenges and Opportunities of Dynamic Microsimulation Modelling](https://microsimulation.pub/articles/00280).

The microsimulation literature is closer to Augur than trading-risk systems.
Useful patterns and warnings:

- dynamic models update micro-unit attributes at each time step;
- base data, matching/imputation, transition equations, and calibration are
  major model-risk sources;
- alignment/calibration against aggregate targets is common;
- validation should cover data, coefficients, parameters, algorithms, modules,
  multi-module interaction, policy impact, and ex-post/historical behavior;
- behavioral/agent feedback is hard and data hungry;
- building too much complexity too early is a known failure mode.

Augur should stay spiral-driven: first make a small dynamic household model
traceable, reproducible, and auditable, then add richer behavior.

### Agent-Based and Discrete-Event Frameworks

Sources: [Mesa docs](https://mesa.readthedocs.io/) and
[SimPy API reference](https://simpy.readthedocs.io/en/3.0.8/api_reference/simpy.html).

Mesa and SimPy are not finance systems, but they show mature simulation
vocabulary:

- agents/entities;
- a model or environment;
- schedules/events/processes;
- resources/containers;
- data collection;
- repeated runs over the same model.

Augur should not turn into a general agent-based market simulator unless needed.
It can still borrow scheduling and data-collection vocabulary for policy
programs, exogenous opportunities, failure events, and trace records.

### Model Governance and Risk Data Governance

Sources: Federal Reserve/OCC
[SR 11-7 model risk guidance](https://www.federalreserve.gov/frrs/guidance/supervisory-guidance-on-model-risk-management.htm)
and Basel Committee
[BCBS 239](https://www.bis.org/publ/bcbs239.htm).

SR 11-7 is banking guidance, not a requirement for Augur, but it is the right
shape of mature model governance: intended use, conceptual soundness,
development documentation, data quality, testing over normal and stressed
conditions, validation, limitations, governance, issue tracking, and model
inventory.

BCBS 239 is likewise overkill for a personal simulator, but its risk-data
principles translate well: data should be accurate, complete, timely, adaptable,
and traceable enough that reports can be reproduced and reconciled.

## Proven Patterns Likely Relevant To Augur

### 1. Separate Scenario Generation From Projection

Pattern: economic scenario generation produces exogenous paths; deterministic
projection evaluates policies and accounting on those paths.

Current Augur alignment:

- `MarketRequest` requests model id, rollout count, horizon, and seed.
- `MarketBundleProvider` samples a `MarketBundle`.
- `simulate_set()` validates the `ScenarioSet`, samples or accepts a
  `MarketBundle`, then runs each `Scenario` through `run_scenario_vectorized()`.

Gap:

- The boundary is not yet named as a model artifact lifecycle:
  evidence -> calibration -> scenario generator run -> exogenous path set ->
  deterministic projection run.

Recommended vocabulary:

- `EvidenceSet`
- `CalibrationRun`
- `ScenarioGeneratorRun`
- `ExogenousPathSet`
- `ProjectionRun`

### 2. Make Path Identity First-Class

Pattern: a path identifier must be stable enough to reproduce and compare a
trajectory.

Current Augur alignment:

- `MarketBundleMetadata` carries market model id, seed, rollout count,
  horizon, event stream ids, notes, and source metadata.
- `rollout_index` is used in actions, policy decisions, observations, ledger
  entries, balance snapshots, accounting details, monthly columns, and selected
  trajectory UI.
- Shared exogenous paths make paired scenario comparisons meaningful.

Gap:

- `rollout_index` is only an array coordinate. It is not a globally meaningful
  path identity.
- Seed plus `rollout_index` is not enough without generator version,
  factor definitions, evidence/calibration identity, and path-set id.
- Future non-market randomness could collide with market-path randomness unless
  Augur separates random streams.

Recommended vocabulary:

- `ExogenousPathId`: stable id for one sampled exogenous world.
- `PathSetId`: stable id for the whole sampled bundle.
- `RiskFactorPath`: one factor's path within an exogenous path.
- `OpportunityStream`: event streams such as private-equity tender windows.
- `ProjectionTrajectoryId`: scenario id plus exogenous path id plus policy
  program version.

### 3. Policy Engines Need Ordered Programs And Traces

Pattern: policy/rule systems should separate rules, parameters, periods,
decisions, actions, and trace output.

Current Augur alignment:

- `Policy` is a discriminated union.
- `ActorPolicyProgram` groups enabled actor policies.
- Row-level `SimulationPolicyDecision` records decisions such as monthly spend,
  public-stock sale, private-equity sale, and partner contribution.
- The roadmap explicitly calls for ordered actor policy programs.

Gap:

- `run_scenario_vectorized()` still runs `MonthlySpendPolicy`,
  `PrivateEquitySalePolicy`, and `CheckingFloorSellPublicStockPolicy` by class
  family, not by a single actor-ordered program.
- The policy program has no policy-step id, priority, phase, or per-step input
  trace.
- Some policy types are schema-only.

Recommended vocabulary:

- `PolicyProgram`: ordered sequence for an actor.
- `PolicyStep`: one executable rule with id, order, phase, and parameters.
- `PolicyDecision`: trace of what the step decided and why.
- `Instruction`: intended mutation emitted by a decision.
- `PolicyExecutionTrace`: ordered per-month/per-rollout trace of step inputs,
  decision, emitted instructions, rejects, and applied actions.

### 4. Separate Events, Opportunities, Decisions, Actions, And Ledger

Pattern: exogenous events/opportunities are observations; policies make
decisions; accounting appliers validate and produce actions/postings.

Current Augur alignment:

- `MarketPathObservation` and `PrivateEquitySaleOpportunityObservation` are
  separate from policy decisions.
- `PrivateEquitySaleDecision`, `SellPrivateEquityAction`, and
  private-equity sale ledger rows are distinct.
- The roadmap already says private-equity tender availability should be an
  exogenous opportunity plus actor policy, not a manual sale request.

Gap:

- Cause linking is partial. Not every ledger entry/action has a strongly typed
  cause id that points back to observation, event, policy decision, accounting
  process, or validation failure.
- `event_id` is often optional, and market opportunities do not yet have stable
  event ids.

Recommended vocabulary:

- `MarketObservation`
- `ScheduledEvent`
- `Opportunity`
- `PolicyDecision`
- `Instruction`
- `Action`
- `AccountingEvent`
- `JournalEntry`
- `Posting`
- `CauseId`

### 5. Accounting Should Be Reconciled, Not Merely Reported

Pattern: result arrays are derived views. Accounting truth lives in journal
entries, balances, lots, basis, liabilities, and reconciliation checks.

Current Augur alignment:

- `SimulationLedgerEntry`, `SimulationBalanceSnapshot`, and
  `SimulationAccountingDetail` exist.
- Many `ScenarioRunArrays` fields are derived from ledger/accounting detail.
- `augur/core/test_e2e.py` includes explicit reconciliation assertions.

Gap:

- Ledger rows are not balanced journal entries. `domain` and `category` are
  strings; there is no chart of accounts, posting group id, debit/credit
  direction, or requirement that postings balance.
- Asset lots, basis, realized gain, tax liabilities, and payment timing are only
  partly modeled.
- Some explanatory arrays still bypass typed accounting detail.

Recommended vocabulary:

- `ChartOfAccounts`
- `AccountId`
- `JournalEntry`
- `Posting`
- `Lot`
- `CostBasis`
- `TaxLotDisposition`
- `LiabilitySchedule`
- `ReconciliationCheck`

### 6. Failure And Default States Must Be Explicit

Pattern: a simulation should distinguish a bad outcome from an invalid state.

Current Augur alignment:

- `checking_floor_shortfall_usd` records a liquidity shortfall for one policy.
- The TODO explicitly asks whether `cash_usd <= 0` should produce failure unless
  an enabled sale/financing policy can cover it.

Risk:

- Negative cash currently behaves like an implicit, unlimited credit facility.
  That can make infeasible plans look viable.

Recommended vocabulary:

- `RolloutStatus`: `active`, `failed`, `defaulted`, `insolvent`,
  `terminated`.
- `FailureEvent`: first month and cause of failure.
- `Shortfall`
- `RejectedInstruction`
- `CreditFacility` or `OverdraftAccount` if negative cash is allowed.

### 7. Provenance And Governance Should Ride With Every Result

Pattern: a result should carry enough provenance to know what model, data,
parameters, code, and limitations produced it.

Current Augur alignment:

- `MarketBundleMetadata` has source metadata.
- `ScenarioSetRunResponse` includes request, market request, report spec,
  market metadata, and warnings.
- `augur/model/markets/data.py` separates evidence loading from historical
  series construction.

Gap:

- No model inventory or model card exists.
- No validation report identity is attached to a market model.
- No evidence/calibration artifact hash is required.
- Warnings are generic strings rather than typed limitations.

Recommended vocabulary:

- `ModelSpec`
- `ModelVersion`
- `ModelCard`
- `ValidationReport`
- `EvidenceSetId`
- `CalibrationArtifactId`
- `RunProvenance`
- `KnownLimitation`

### 8. Keep Calibration And Evidence Separate From Runtime Inputs

Pattern: evidence informs fitted models; fitted models generate scenario paths;
projection consumes paths.

Current Augur alignment:

- `augur/model/` owns evidence loading, market model protocols, fitting, and
  `MacroMarketBundleProvider`.
- `augur/plans/e2e_redesign.md` explicitly says source-data shapes belong in
  `augur/model`, not app state or the simulator contract.

Gap:

- The result metadata should state which evidence and calibration were used.
- Core should never accept source-specific data such as FRED/Zillow/Manifold
  shapes.

Recommended vocabulary:

- `RawEvidence`
- `EvidenceSet`
- `CalibratedMarketModel`
- `MarketModelFit`
- `ScenarioGenerator`
- `MarketBundle`

## Alignment Audit

### Too Simplistic Or Weird

- `rollout_index` is doing too much. It is a UI selector, array coordinate,
  paired-comparison key, and pseudo trajectory id.
- `MarketBundleMetadata.event_stream_ids` names streams but not event instances,
  event source versions, or opportunity ids.
- `PrivateEquitySaleOpportunityObservation` has no stable opportunity id.
- Policy execution is class-grouped. This conflicts with "ordered actor policy
  programs" and will become brittle when policies compete for cash or assets.
- Some asset/liability variants still look more public than their runtime
  semantics justify; each should either drive simulation state or be rejected
  until implemented.
- `TaxPaymentTiming.ALLOCATED_TO_SOURCE_MONTH` is documented as a debt item; it
  should become liability/payment timing, not just a source-month adjustment.
- `TaxProfile.marginal_tax_rate` and `cap_gains_rate` are too coarse for
  serious household tax modeling, even with explicit non-goals.
- `domain`/`category` ledger rows are useful but too weak as the long-term
  accounting substrate.
- Initial state and scheduled transitions still live across several shapes:
  property selection, financing, occupancy, rental, events, policies, and
  initial balance sheet.

### Actively Risky

- Negative `cash_usd` can be an accepted projection state without explicit
  default, borrowing, liquidation, or failed-rollout semantics.
- Result arrays can still become a parallel truth source unless every field is
  state-, ledger-, snapshot-, or accounting-detail-backed.
- A user could treat tender-eligible private-equity value as liquid if result
  labels or APIs drift; the code is moving away from this, but the domain
  distinction must stay enforced.
- Reproducibility is under-specified. A URL with seed and rollout is not enough
  if model code, source data, calibration, or scenario generator settings move.
- Policy-order regressions will be hard to spot when multiple policies interact
  with the same cash, assets, lots, or opportunity stream, so the ordered
  program dispatcher needs durable guard tests.
- Governance is not yet strong enough for financial advice-like use. Even for a
  personal tool, outputs should state model version, evidence, assumptions,
  limitations, and validation status.

## Recommended Architecture Vocabulary

Standardize these names before the next large redesign:

| Concept                             | Proposed name                | Purpose                                                                   |
| ----------------------------------- | ---------------------------- | ------------------------------------------------------------------------- |
| Raw observations                    | `RawEvidence`                | Source-specific data before alignment or cleaning.                        |
| Aligned model input                 | `EvidenceSet`                | Versioned, cleaned data used for fitting.                                 |
| Fitting run                         | `CalibrationRun`             | Produces fitted parameters/artifacts.                                     |
| Fitted generator                    | `CalibratedMarketModel`      | Model ready to simulate paths.                                            |
| Scenario generation invocation      | `ScenarioGeneratorRun`       | Records seed, generator version, evidence id, calibration id, factor set. |
| Sampled exogenous worlds            | `ExogenousPathSet`           | Current `MarketBundle` plus stronger identity/provenance.                 |
| One sampled world                   | `ExogenousPathId`            | Stable identity for one path.                                             |
| Factor path                         | `RiskFactorPath`             | SP500, CPI, home value, rent, mortgage rate, PE mark, etc.                |
| Event/opportunity stream            | `OpportunityStream`          | Tender/IPO/acquisition/lockup/other exogenous opportunities.              |
| Deterministic projection invocation | `ProjectionRun`              | Scenario set plus exogenous path set plus code/model versions.            |
| Per-scenario path result            | `ProjectionTrajectoryId`     | Scenario id plus exogenous path id plus policy version.                   |
| Runtime state                       | `RolloutState`               | Accounts, assets, liabilities, lots, tax state, ownership ledgers.        |
| Rollout health                      | `RolloutStatus`              | Active/failed/defaulted/terminated state.                                 |
| Actor rules                         | `PolicyProgram`              | Ordered policy sequence for one actor.                                    |
| One rule                            | `PolicyStep`                 | Executable policy node with order and phase.                              |
| Decision trace                      | `PolicyExecutionTrace`       | Inputs, decision, instructions, rejects, applied actions.                 |
| Intended mutation                   | `Instruction`                | Policy output before validation/application.                              |
| Realized mutation                   | `Action`                     | Applied operation after validation.                                       |
| Accounting truth                    | `JournalEntry` and `Posting` | Balanced economic record.                                                 |
| Point-in-time truth                 | `BalanceSnapshot`            | State value at a month.                                                   |
| Explanatory detail                  | `AccountingDetail`           | Basis/gain/tax/depreciation calculation detail.                           |
| Result mode                         | `DistributionResult`         | Percentiles/fans over many trajectories.                                  |
| Result mode                         | `TrajectoryResult`           | One selected trajectory with trace rows.                                  |
| Result mode                         | `AccountingTrace`            | Journal, postings, lots, liabilities, reconciliation.                     |
| Governance                          | `ModelCard`                  | Intended use, assumptions, limitations, validation.                       |
| Governance                          | `ValidationReport`           | Backtests, invariants, sensitivity/stress results.                        |

## Prioritized Recommendations

### Near-Term Implementation Slices

1. Generalize rollout health and required-obligation settlement.
   - Extend the landed `RolloutStatus`, `FailureEvent`, obligation, funding
     decision, and settlement-result shapes beyond annual tax obligations.
   - Decide whether `cash_usd < 0` is failed, defaulted, or allowed only through
     an explicit `CreditFacility`.
   - Add tests for mortgage/payment shortfalls, policy rescue through sale, and
     unrecoverable default.

2. Persist explicit trajectory/path provenance.
   - Keep `rollout_index`, `PathSetId`, `ExogenousPathId`, and
     `ProjectionTrajectoryId` as the public identity vocabulary.
   - Back those IDs with persisted market model, generator version, evidence,
     calibration, seed, path index, risk-factor set, event-stream, and code
     version artifacts where available.

3. Extend ordered actor program tracing.
   - Keep policy execution on the ordered actor program dispatcher.
   - Emit `PolicyExecutionTrace` rows even for no-op or rejected decisions.
   - Preserve vectorized performance, but make semantics actor/order-first.

4. Strengthen cause ids.
   - Require every action, ledger row, balance snapshot, and accounting detail
     to carry a typed cause: policy decision, scheduled event, market
     observation/opportunity, or system accounting process.
   - Give private-equity sale opportunities stable ids.

5. Continue Step 7 reconciliation.
   - Move the remaining explanatory arrays into typed accounting detail or
     documented state snapshots.
   - Add tests that fail if monthly columns drift from ledger/snapshot/detail
     rows.

6. Replace placeholder governance with reviewed artifacts.
   - Keep the current model-card/version and validation-report fields, but back
     them with reviewed intended use, source data, calibration, limitations,
     validation status, and known non-goals.
   - Attach durable artifact hashes or reviewed IDs to `MarketBundleMetadata`.

### Medium-Term Redesign

1. Replace stringly ledger rows with journal entries and postings.
   - Keep compatibility views for UI arrays.
   - Add typed accounts, assets, liabilities, lots, tax liabilities, and
     ownership claims.

2. Move tax/regulation toward OpenFisca-like parameters.
   - Put tax/regulation constants in perioded parameter sets.
   - Keep approximations explicit, versioned, and testable.
   - Separate rules from deployment-specific location/property data.

3. Introduce fitted market-model artifacts.
   - Persist evidence/calibration identity, factor set, fitted parameters, and
     validation metrics.
   - Let `MacroMarketBundleProvider` report those artifacts, not just latest
     observations.

4. Make result helpers mode-specific.
   - `DistributionResult` should not expose selected-path rows except through
     explicit trajectory selection.
   - `TrajectoryResult` should expose observations, decisions, actions, ledger,
     balances, and accounting trace locally.

5. Add scenario-variant/reform vocabulary.
   - For comparing policies, model "baseline vs changed scenario" as two
     scenario variants over the same exogenous path set.
   - Borrow the OpenFisca/Tax-Calculator "reform" pattern only where it means
     modification to a reference rule/parameter set.

### Things To Avoid

- Do not make a single deterministic rollout the main product API. It is an
  inspection view for one path from a distribution.
- Do not let the browser-side flat scenario row become the source of truth.
- Do not put FRED, Zillow, Manifold, or other source-specific evidence objects
  in `augur/core`.
- Do not revive arbitrary manual sale controls as the private-equity liquidity
  model.
- Do not treat tender-eligible private-equity marks as liquid net worth.
- Do not build a general equilibrium or agent-based market simulator until a
  concrete policy question requires it.
- Do not add more policy enum hacks for actor agreements. Model agreements as
  contracts or policy programs between actors.

## Testing And Verification Implications

### Golden Projection Tests

Keep deterministic flat/noop market fixtures and hand-computed tests. Expand
them for:

- property purchase and sale;
- public stock sale with basis and tax;
- private-equity tender opportunity, sale, and non-sale reasons;
- partner contribution and ownership accrual;
- tax liability timing;
- failure/default outcomes.

### Reproducibility Tests

Add tests that assert:

- same scenario input plus same path-set provenance produces identical outputs;
- changing seed changes path ids and usually results;
- changing evidence/calibration/model version changes provenance even if some
  values happen to match;
- `ExogenousPathId` is preserved across scenarios in one scenario-set run.

### Invariant Tests

Add invariant checks for:

- no state mutation without a cause id;
- no negative cash unless an explicit credit/default/failure state explains it;
- nonnegative asset units and remaining basis;
- ownership claims reconcile to equity ledger rules;
- liability balances follow schedules and payments;
- tax lots reconcile sale proceeds, basis, gain, tax, and cash proceeds;
- `liquid_net_worth_usd` excludes tender-only private-equity value;
- terminal distribution metrics equal statistics over terminal trajectory
  values.

### Ledger Reconciliation Tests

Continue the current e2e pattern:

- arrays that describe transaction flows must equal ledger sums;
- arrays that describe balances must equal balance snapshots or explicit state;
- arrays that explain calculations must equal typed accounting detail;
- compatibility aliases must document their backing source.

The target is not "no arrays"; the target is "arrays are views, not truth."

### Policy Program Tests

Add tests for:

- actor policy order;
- policy no-op trace rows;
- policy rejection rows;
- two policies competing for the same cash or asset;
- policy decisions caused by market observations;
- policy actions linked back to decisions and accounting postings.

### Failure Semantics Tests

Add tests that cover:

- monthly spending draining cash below zero;
- mortgage/property costs exceeding cash;
- sale policy covering a shortfall;
- sale policy unable to cover a shortfall;
- rollout termination vs continued projection after failure;
- distribution summaries that include failed-rollout rates.

### Backtesting And Calibration Checks

Add model-layer tests and reports for:

- historical holdout/backtest behavior;
- calibration data coverage and recency;
- factor correlation/covariance sanity;
- path distribution sanity against historical moments;
- stress scenarios outside ordinary expectations;
- sensitivity of household outcomes to major assumptions.

### UI And Result-Mode Verification

Keep visual and browser tests that assert:

- distribution pages show only distribution panels;
- trajectory pages show selected-path and accounting-detail panels;
- property/location data is scenario context, not a result;
- selected trajectory URLs include enough provenance to reproduce or reject the
  selected path when provenance is missing.

## Source Categories Covered

- Quant/risk architecture: QuantLib and ORE.
- Tax-benefit/rule microsimulation: OpenFisca and PolicyEngine.
- Public tax microsimulation: Tax-Calculator/Policy Simulation Library.
- Dynamic microsimulation literature: International Journal of Microsimulation
  survey and challenge papers.
- General simulation frameworks: Mesa and SimPy.
- Model and risk-data governance: SR 11-7 and BCBS 239.
