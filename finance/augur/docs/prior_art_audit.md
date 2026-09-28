# Augur Prior Art Audit

## Summary

Augur is closest to a dynamic household microsimulation driven by an economic
scenario generator. Exogenous models sample market paths first; a `World` of
simulated actors is built on each path and reads it, with no feedback into the
paths. In financial-risk terms, a `Sampler` returning a `SampledExogenousBundle`
is the scenario generator, one `World` per path is the pathwise deterministic
projector, and the caller's loop is the Monte Carlo driver. Augur supplies
building blocks rather than a framework: the caller samples, declares, steps and
reads results (README § Using Augur).

The prior art below still points at open work:

- `cause_id` on journal entries, payments, claims and tax accruals is a
  formatted string whose shape differs per module.
- Tax law is one `law_year` per jurisdiction, held flat or CPI-indexed into
  later years, with no external oracle checking it.
- Promoting a fitted model from `x/models/` into core needs evidence beyond the
  current sanity bands, and providers record evidence and fit identity in
  `provenance` inconsistently.

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

Augur has the same separation without the instrument domain: a `Sampler` or
`HistoricalWindowsModel.materialize` generates paths, `compile_series` fixes
them onto the monthly grid, each `World` evaluates one `MarketPath`
deterministically given the caller's ordered actions, and accumulation is the
caller's (or the product's metric fans).

### Open Source Risk Engine (ORE)

Sources: [ORE GitHub README](https://github.com/OpenSourceRisk/Engine),
[ORE documentation overview](https://opensourcerisk.org/documentation/), and
[ORE scenario reference](https://www.opensourcerisk.org/docs/orea/group__scenario.html).

ORE demonstrates how a production risk system splits trade/portfolio input,
market data, application configuration, simulation configuration, risk-factor
evolution, scenario generation, exposure simulation, stress scenarios,
sensitivity scenarios, historical scenarios, and generated reports. Its scenario
module names useful concepts: scenario generator, scenario path generator,
scenario data, scenario sim market, risk-factor key, stress scenario, historical
scenario, and aggregation scenario data.

Augur is not pricing derivatives, but ORE is useful prior art for path identity,
market data provenance, stress/path replay, and auditability. Its risk-factor
key corresponds to `LevelSeriesKey`, its scenario sim market to the
`MarketPath` over `compile_series` output, and its historical scenarios to
`HistoricalWindowsModel`. ORE's application configuration owns the run; in Augur
the caller does.

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

Augur keeps tax law as data: jurisdiction tables in `sim/data/jurisdictions/`,
each stating its `law_year`, resolved by `sim/tax_profile.py` into immutable
profiles. Tax facts are traced as `TaxAccrual` rows. Parameters are not yet
perioded (§ Open recommendations). Engine-level options are evaluated in
<../sim/docs/tax_engine_evaluation.md>.

### PolicyEngine

Sources: [PolicyEngine Core introduction](https://policyengine.github.io/policyengine-core/intro.html)
and [PolicyEngine US docs](https://policyengine.github.io/policyengine-us/).

PolicyEngine, a fork of OpenFisca Core, highlights a clean split between the
generic microsimulation framework and country-specific logic, parameters, and
data. The framework calculates variables for periods and can trace computation
trees; the country packages define entities, variables, parameters, and data.

Augur draws the same boundaries: core code in this repo, jurisdiction rules as
data, deployment-specific and private data in downstream repos (README
§ Planning boundary), and inspectable `Trace` output (books, journal, events,
receipts).

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

In Augur, month-0 declarations on a `World` play the role of `Records`, the
resolved tax profile the role of `Policy`, and the `World` the role of
`Calculator`. Cross-model validation is the open item: Tax-Calculator is the
recommended federal oracle in <../sim/docs/tax_engine_evaluation.md>. Citing
model and replication materials is AGENTS § Provenance of a reported number.

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

Augur's answers: ex-post validation is the `study/` reproductions of published
results; behavioral feedback into markets is excluded by the one-way contract;
and "no layer without a caller that needs it now" (AGENTS § Conventions) guards
against early complexity.

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

In Augur, agents are tracked `Actor[In, Out]`s exchanging closed typed message
unions, the model is the `World`, and the schedule is its monthly open, act and
close, counterparties before agents, skipping undeclared domains. Data
collection is the caller reading
state between steps, or `FinancialCapture`; a Mesa-style collector or event bus
was considered and rejected (<../sim/DESIGN.md> § Common experiment session and
§ Rejected designs).

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

Augur's model inventory is its stability tiers: historical replay in core,
fitted models in `x/models/` until evidence promotes one, and a checked-in fit
either passing `x/models/calibrated/sanity_test.py` or listed in its
`QUARANTINED`. The caller names the model it ran; Augur stamps no model
identity on paths or results.

## Where Augur Stands

| Pattern                                      | Augur today                                                                                                                                                                                                      |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scenario generation separate from projection | Exogenous models sample first; `World` reads a `MarketPath` and never feeds back. `fit/` and model providers own evidence; `sim/` never loads it.                                                                |
| Path identity                                | Original `rollout_id` survives selection and reordering (SPEC). Independent series models derive a per-stream seed from each rollout seed (`derive_stream_rollout_seeds`). Samples carry free-form `provenance`. |
| Decisions, actions and outcomes kept apart   | Caller policies emit `Action`s; each gets a `Receipt` with `Executed` or `Rejected`. Private-equity opportunities carry ids and come from the issuer protocol, not from policy.                                  |
| Accounting as truth, arrays as views         | `Ledger` applies balanced `JournalEntry` groups atomically; lots keep exact basis; mortgage principal lives in the liability ledger. The product projects from canonical frames.                                 |
| Explicit failure states                      | A path stops on `RejectedAction` or `UnpaidClaims`; an unfunded action is rejected, never an implicit overdraft (SPEC § Accounting and failure).                                                                 |
| Provenance on reported results               | AGENTS § Provenance of a reported number: sampler, fit window, policy config, instrument construction, sampling noise.                                                                                           |

## Open Recommendations

1. **Typed causes.** Replace the formatted `cause_id` strings (`opening-lot:…`,
   `pe_forced_sale_m…`, `…_estimated_tax_q…`) with a typed cause naming the
   claim, action receipt, scheduled flow, opportunity or opening declaration, so
   a journal entry links back without string parsing.
2. **Perioded tax parameters.** Let jurisdiction tables carry dated law changes
   (OpenFisca parameters) instead of one `law_year` held flat or CPI-indexed.
3. **External tax oracle.** Cross-check annual tax facts against Tax-Calculator,
   as <../sim/docs/tax_engine_evaluation.md> recommends.
4. **Promotion evidence for fitted models.** Define what moves a model from
   `x/models/` into core using SR 11-7's list: intended use, limitations,
   holdout and rolling-origin scores (`fit/metrics.py`), stressed conditions,
   and sensitivity of household outcomes to the major assumptions.
5. **Consistent fit provenance.** `vecm` and `state_space` put digests of their
   evidence and fit into `provenance`; `structural_macro` records only a note.
   Every fitted provider should record which evidence and fit artifact it
   sampled from, kept descriptive per the `SampledExogenousBundle.provenance`
   contract.

## Considered and Rejected

These follow naturally from the prior art and were rejected; the reasons are in
<../sim/DESIGN.md> § Rejected designs.

- **Scenario and reform objects** (OpenFisca reforms, a baseline-vs-variant
  vocabulary). A comparison is two worlds per path over the same sampled series,
  as in `x/bounded_spending/compare.py`; see "A scenario object".
- **Run objects** (ORE application configuration, a `ProjectionRun` or
  generator-run manifest tying inputs, paths and code versions). The caller owns
  the run; see "A facade that runs the rollout loop".
- **Policy programs** (ordered rule steps with a framework-owned execution
  trace). Strategies are caller code; the world records only receipts; see "A
  facade that runs the rollout loop" and "a universal component/plugin
  framework".
- **Typed model or path identity objects.** The caller names the model it
  ran; nothing is stamped (AGENTS § Conventions).

## Things To Avoid

- Do not make a single deterministic rollout the main product API. It is an
  inspection view of one path from a distribution.
- Do not let source-specific evidence (FRED, Zillow, Manifold shapes) reach the
  API or `sim/`; a `World` reads integer series only.
- Do not model private-equity liquidity as manual sale controls; it comes from
  the issuer protocol and `declare_tender_policy`.
- Do not count tender-eligible private-equity marks as liquid net worth.
- Do not model actor feedback into markets or build a general-equilibrium
  simulator; exogenous paths flow one way.
- Do not add core options for agreements between actors. An agreement is a
  tracked counterparty or contract (`Biller`, `Mortgage`).

## Source Categories Covered

- Quant/risk architecture: QuantLib and ORE.
- Tax-benefit/rule microsimulation: OpenFisca and PolicyEngine.
- Public tax microsimulation: Tax-Calculator/Policy Simulation Library.
- Dynamic microsimulation literature: International Journal of Microsimulation
  survey and challenge papers.
- General simulation frameworks: Mesa and SimPy.
- Model and risk-data governance: SR 11-7 and BCBS 239.
