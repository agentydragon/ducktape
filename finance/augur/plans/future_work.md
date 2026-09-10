# Remaining capability and research questions

The [landing roadmap](roadmap.md) owns priorities and dependencies. This file
retains questions from the older project/simulator TODOs without reviving their
completed work, obsolete implementation prescriptions or separate phase order.
Private configuration, evidence, deployed artifacts and issuer/account-specific
assumptions remain downstream.

## Domain/API cleanup

- **BIND:** use typed prepared facts and conditioning/artifact observations for
  explicit financial-product bindings. Distinguish configured
  opening levels, relative path anchors and fixed contractual amounts. Keep their
  anchoring conventions explicit at composition, not inferred from source names.
- **P12 / ACCEPT / CAP:** retire configured drivers and test adapters while
  preserving actual state/receipt facts. Account/component selection and any
  additional trade/contract capture need a named consumer. Do not create another
  accounting store, generic posting-template framework or mandatory metric slab.
- **Consider the name “exogenous.”** Removing it may better describe the boundary
  between a coarse statistical world model and explicit economic simulation.
  This is a naming consideration, not a mandatory repository-wide rename or a
  prerequisite for domain work; the current statistical models need not define
  what every future world model can express.
- **GP:** initial ownership, existing contracts and later decisions must not be
  contradictory representations of the same purchase/financing/event. Preserve
  account ownership and distinguish passive counterparties from additional
  decision-making actors. Repeated account names are not a reason to introduce a
  universal entity registry; revisit identity ergonomics with real consumers.
- **Independent randomness:** distinguish market sampling, any stochastic policy
  decisions and other modeled processes in experiment provenance. Do not turn a
  storage position into another rollout identity or promise seed equivalence
  between different models.
- **IDTYPES, deferred:** use distinct strongly typed entity IDs so security and
  property identities cannot be interchanged at domain/API boundaries. This is
  not merely a prefix spelling change. Preserve existing typed keys; do not make
  a global serialization/ID sweep a prerequisite for product composition.

## Reduced-form TLH portfolios

**MA1/MA2** retain integration acceptance for the implemented opaque Python
component and its drivers; **MA3** remains the paired experiment. See
[the TLH migration plan](managed_portfolio.md). Representation and the common
pre-investor monthly phase are settled, not a gate to reopen here.

- Check modeled realized losses and subsequent gain/basis consequences against
  named real-account evidence. Refit decay as longitudinal evidence becomes
  available; calibration values and private forms are not shared statutory facts.
- Examine wash-sale, fees, cohort composition and market-regime limitations when
  evaluating fidelity. A calibration period with no reported wash sales does not
  establish their absence in future scenarios.
- A representative-sleeve or constituent-level model is a much-later option, not
  a prerequisite or a permanent prohibition. Reconsider it only if discrepancies
  with reality affect a decision worth modeling. It belongs to the investment
  service model, not a new household policy discriminator.
- Report realized effects and aggregate value/basis from the component's financial
  statements. Keep cohort state private to the Python component; neither policy
  observations nor native capture owns a second mutable basis/deferral authority.

## Taxes and financial contracts

**GT / TAX** own supported-law scope and independent controls in
[the tax checklist](tax_coverage.md). Existing wage/interest sources, quarterly
payments, loss netting and rental deductions are not missing from the core merely
because the app has no corresponding input control.

- Preserve the distinction between available payment machinery and full
  jurisdiction-specific safe-harbor, withholding, penalty and refund rules.
  Calendar/law-year selection, opening YTD facts/carryovers, qualified dividends,
  NIIT and filing/residency coverage remain scoped requirements, not optional
  omissions from a report that needs them.
- Reconcile purchase costs, depreciation, improvements and disposal basis using
  independent cases. Improvements may need their own in-service schedules;
  identifying that requirement does not authorize a silent tax-method change.
- For housing arms, assess purchase-based property-tax assumptions, assessed-value
  evolution, supplemental bills and separate special-assessment schedules/expiry.
  Do not prescribe a generic CPI cap or report precise statutory effects before
  selecting and verifying the jurisdiction's rules.
- Debt facilities, refinancing, adjustable rates and additional liens require
  explicit contracts and settlement; unexplained negative cash is not borrowing.
  Partial payment, delinquency, grace periods and recovery remain separate
  deferred mechanics, not an invitation to add retries to the monthly action API.
- Lot-selection algorithms are policy helpers over explicit sale requests.
  Additional HIFO/specific-selection helpers may be useful; this is distinct from
  changing a product's tax-basis method. Keep existing FIFO controls.
- Trading costs/spreads and settlement timing must be stated in studies that
  compare turnover. Add friction to canonical settlement when the selected study
  requires it; do not certify a free-trading comparison as cost-aware. Existing
  tracking issue: [#5486](https://github.com/agentydragon/ducktape/issues/5486).

## Bonds and spending strategies

**BOND / GT** own dated-position marking, partial sale, off-par treatment and
acquisition. A hold-to-maturity choice must not become an instrument's permanent
illiquidity rule. Supplied-curve controls can proceed without a new forecast model;
a common yield beyond ten years does not give bonds of different maturities the
same price. A duration-matched fund is a sensitivity comparison, not a proven
bound on the outcomes of a dated-bond ladder.

**STUDY / RUN** own executable strategy comparisons:

- Ladder rung selection, coverage targets, purchase deferral and roll timing are
  experiment policy code. Native acquisition/trading is a scoped BOND dependency,
  not a reason to introduce a central structured ladder policy or purchase slots.
- Spending flexibility, reserve policies and rebalance cadence compose through
  the same batch action interface and Python helpers. A forward-reserve rule may
  use known contractual schedules and explicitly modeled expectations, never
  inspect the realized future market path.
- Stateful lifestyle tiers may use separate cut/recovery thresholds and explicit
  transition timing. Implement them as experiment-owned policy memory, not a
  `TieredAmount` scenario variant or a native/JAX policy-state subsystem.
- Keep PE tender choices distinct from compulsory recovery mechanics. A combined
  allocation policy can consider sellable holdings and tender constraints after
  the GPE/PE boundary is supported; do not force PE into the old allocator schema.
- Express scenario-specific spending rules in policies; use contract schedules
  for obligations. Rent caps/lease changes require supported contract terms rather
  than a universal `ScenarioKey` knob or silently altered CPI indexing.
- Report real purchasing-power measures as explicit reductions with a stated
  base date, not a second accounting currency/mode.

## Deferred housing and private-equity scope

**GHOUSE / HOUSING / HOUSE / GPE / PE** retain these capabilities without making
them prerequisites for public-portfolio cleanup:

- Purchases, mortgage origination, rent changes, occupancy transitions,
  improvements and sale must agree on timing and ownership. No new app controls
  are needed to establish the library action boundary.
- Fractional ownership requires coherent co-owner contributions, liabilities,
  expenses, deductions and sale proceeds; do not restore an isolated percentage
  knob. Partner-equity accrual has the same prerequisite.
- Model offered mortgage terms, lumpy maintenance, HOA changes/assessments,
  insurance premiums/claims and stochastic tenancy only when a study needs the
  additional fidelity. These are optional model/contract refinements, not
  generic requirements to migrate the current fixed-rate lifecycle.
- Finish typed PE protocol controls for capacity, eligibility, liquidity blocks,
  forced sale and recovery. Preserve issuer/exogenous events separately from
  explicit tender responses and avoid silent nonparticipation defaults.
- Keep meaningful deterministic inspections of public-market opening, impairment,
  forced recovery, acquisition and low-value paths as consumers migrate.

## Market evidence and model evaluation

**SCORE / GM / MODEL / ROBUST / READY** own comparison, adoption and adequacy;
model families are candidates, not mandatory implementation projects.

- Compare VECM fitting approaches on common held-out data and horizons. Old
  isolated fit scores or shrinkage hypotheses are not a current acceptance target;
  record evidence/window/transformations and uncertainty with a new comparison.
- Give baseline providers compatible observables when comparing predictive
  quality. Missing factors must remain explicitly unscored, not filled in.
- Investigate term-structure support beyond the current short/ten-year proxy,
  real-rate construction, issuer/credit behavior and rolling-fund assumptions.
  Do not conflate observed curve interpolation, statistical forecasts and
  risk-neutral pricing or mandate a particular affine/regime model.
- Fit location-specific rent/home-value factors or document an explicit proxy
  when independent evidence is unavailable. Which cities/regions and private
  deployment mappings are wanted remains downstream scope.
- Candidate joint models include simple VAR, stochastic-volatility/correlation,
  actuarial models and historical/block resampling where their behavior helps a
  study. Reuse current interfaces, not deleted market-bundle adapters.
- PE research remains deferred: valuation/dilution dependence, primary versus
  secondary event treatment, discrete issuance, posterior-predictive deployment,
  and population-informed failure/no-liquidity tails. A population fit must
  account for failed/non-exited companies; do not tune a hazard solely to one
  anecdote or market quote. Existing behavior is described in
  [the calibration contract](../docs/calibration.md), not a pending implementation plan.
- Reconcile the mint-stream fitter's Poisson event-rate posterior with the
  sampler's direct monthly Bernoulli probability, and validate the non-unit
  step-up valuation/share convention before claiming cash/cap-table fidelity.
  [Current PE conventions](../docs/private_equity_model.md) identify the affected
  implementations. Neither this question nor synthetic sanity bands endorse
  smooth-dilution fidelity or select a deployment default.
- [Optional PM and sparse-company research](market_model_research.md) retains
  market-quality/duplicate weighting, joint-coupling and censored-reference-class
  questions. It is not an additional architecture mandate or gate stack.

## Deferred evidence operations and app work

These are retained ideas, not active DAG prerequisites. New `product/` and UI
features remain deprioritized; correctness and legacy retirement are separate.
Richer PE app controls are dropped from the backlog, not queued as deferred work.

- Retirement of the smooth-dilution PE mode and the experimental PM reifier is
  deferred. The owner found smooth dilution unsatisfactory for fidelity; its
  continued presence is not an endorsement. PM-based forecast comparison remains
  a possible later experiment, but keeping the reifier executable is not a
  requirement. If calibration/reification code is deleted, leave a revival task
  for comparing Augur forecasts with prediction-market beliefs; do not silently
  lose that research intent or turn it into a current feature obligation.
- Keep existing evidence loaders and access working. Postpone new fetch-cadence,
  caching and throttling infrastructure until observed throttling justifies it;
  no speculative ingestion subsystem or cache prerequisite for studies. Quote
  age/quality presentation and an aggregate calibration score need a concrete
  consumer and weighting convention. Preserve price-index versus total-return,
  point-in-time versus deadline, anchoring and unresolved-horizon distinctions
  in the existing calibration controls; their implementation is not new backlog.
- A gated refit/publish workflow can refresh trained artifacts from new evidence.
  Keep data acquisition, fit acceptance and downstream artifact/config deployment
  distinct; publication is not evidence that a fit is adequate.
- Better brokerage imports may need per-security model bindings, authoritative
  lot history, missing-date/basis treatment, transfers and custody provenance.
  Do not infer complete tax lots from aggregate position values.
- Possible app improvements: grouped financial/tax inputs,
  macro fans, deterministic lifecycle markers, paired scenario comparisons,
  consistent percentile labels and hover/selected-trajectory detail. UI choice
  does not redefine the simulation's accounting or policy interface.
- Reuse shared browser-test infrastructure when a concrete remaining duplicate
  is found. Performance/layout tuning waits for an actual measured workload;
  deleted cache paths and Python-engine loops are not optimization backlog.
