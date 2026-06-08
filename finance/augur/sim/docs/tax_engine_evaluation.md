# Tax Engine Evaluation

Reviewed: 2026-05-20.

## Recommendation

Do not adopt an external tax engine as Augur's simulator runtime right now.
Keep `augur/sim`'s event-sourced, per-month evaluator and YAML-backed
jurisdiction data as the production path. External engines are useful, but
mostly as rule references and validation oracles for annual filing-unit tax
liability, not as a replacement for Augur's month-by-month cash/tax event
model.

Best practical path:

1. Use PSLmodels Tax-Calculator as the first external federal oracle for
   sampled annual tax computations. Its CC0 licensing and Python API make it
   the lowest-risk source to call from tests or to borrow federal rule logic.
2. Evaluate PolicyEngine US in a small spike if Augur needs broader federal
   plus state validation. It has the strongest US tax-benefit coverage, but
   its AGPL license, OpenFisca-style household/tax-unit model, and static
   annual microsimulation orientation make direct production integration
   expensive.
3. Borrow OpenFisca/PolicyEngine design ideas: parameter trees, variable
   formulas, YAML tests, and explicit entities. Do not migrate Augur onto the
   OpenFisca runtime unless the simulator becomes a tax-benefit
   microsimulation product instead of a pathwise personal-finance simulator.
4. Treat OpenTaxSolver, IRS Direct File, TenForty, and similar tax-prep tools
   as reference material or spot-check tools only.

The key reason is data-model mismatch. Augur needs tax law to interact with
asset sales, mortgage payments, property taxes, forced liquidations,
quarterly estimated payments, January true-up, and rollout failure. Most tax
engines compute annual liability from tax-return-shaped aggregates. That is
valuable for year-end liability, but not enough to own Augur's event log,
payment timing, safe-harbor cash obligations, or property lifecycle.

## Current Augur Fit Criteria

Augur's simulator currently or soon needs:

- Python-callable code that can run inside the existing Bazel/Python stack.
- Federal US and California individual income tax, initially single filer but
  with room for other filing statuses.
- Ordinary income, short-term and long-term capital gains, Net Investment
  Income Tax later, itemized-vs-standard deduction, SALT cap, qualified
  residence mortgage-interest deduction, Section 121 exclusion, Section 1250
  recapture, rental income and depreciation.
- Property mechanics outside tax forms: property tax accrual, mortgage
  amortization and payoff, transfer costs, basis tracking, co-ownership, and
  per-location taxes.
- Quarterly estimated tax and safe-harbor cash timing. This is a simulator
  obligation-settlement problem as much as a tax-law problem.
- A row/vector-friendly interface for many rollouts, with explainable
  per-year breakdown rows.

## Options Considered

### PolicyEngine US

PolicyEngine US is the closest external match on tax-law breadth. The project
describes itself as a Python package and rules engine for the US state and
federal tax-benefit system, installable as `policyengine-us`. The docs say it
is built on the OpenFisca framework and implements most federal income tax
code. The repository is active and AGPL-3.0 licensed. The current raw
`pyproject.toml` reports version `1.680.0`, Python `>=3.9,<3.15`, and a
dependency on `policyengine-core`.

Coverage:

- Federal: strong. Docs include capital gains examples and general IRS
  program coverage. PolicyEngine-TAXSIM also exposes TAXSIM-compatible inputs
  for wages, dividends, long-term capital gains, short-term capital gains,
  mortgage interest, property tax, state tax, NIIT, and other outputs.
- State: strongest surveyed OSS option, but verify before relying on any
  given state-year. PolicyEngine's own docs show state pages, while the
  PolicyEngine-TAXSIM project claims all 50 states plus DC for TAXSIM-style
  comparisons. For Augur's California needs this is promising but still needs
  a focused CA fixture spike.
- Property/mortgage: models tax-return inputs such as deductible mortgage
  interest and real-estate taxes. It does not own Augur-style mortgage
  amortization, property basis, sale closing flow, Section 121 clocks, or
  month-by-month property-tax accrual.
- Quarterly estimates/safe harbor: no clear sign that it models the cash
  payment schedule Augur needs. Keep that in Augur.
- Capital gains/deductions/filing status: good candidate for annual liability
  validation. Data model is household/tax-unit/entity based, not Augur's event
  log.

Integration difficulty: High for production use, medium for validation use.
Calling it as a Python library is feasible, but mapping each rollout-year into
PolicyEngine households/tax units and reconciling its variable names,
entities, and periods would be nontrivial. The AGPL-3.0 license is the main
policy risk for linking it into a distributed product; get legal review before
shipping it as a runtime dependency.

Disposition: Use as a benchmark/oracle candidate after Tax-Calculator, not as
the core engine. Especially useful to validate California and richer federal
cases if license posture is acceptable for test-only or separate-process use.

Sources:

- https://github.com/PolicyEngine/policyengine-us
- https://raw.githubusercontent.com/PolicyEngine/policyengine-us/master/pyproject.toml
- https://policyengine.github.io/policyengine-us/
- https://policyengine.github.io/policyengine-us/gov/irs/capital-gains.html
- https://policyengine.github.io/policyengine-us/validation/taxsim.html
- https://github.com/PolicyEngine/policyengine-taxsim
- https://github.com/PolicyEngine/policyengine-core

### OpenFisca Core, Country Template, and Country Packages

OpenFisca is a mature rules-as-code framework. Its architecture separates
Core, country packages, extension packages, and templates. Country packages
define parameters, variables, and entities; Core provides the Python API, DSL,
testing tools, and optional web API. The country template is a Python package
template and is AGPL-3.0 licensed.

Coverage:

- OpenFisca Core and Country Template do not provide US tax rules. They are
  framework and boilerplate.
- The OpenFisca package gallery lists US as PolicyEngine, so the practical US
  OpenFisca-lineage package is PolicyEngine US rather than a separate
  canonical OpenFisca-US package.
- Country packages such as OpenFisca-France are evidence of framework
  maturity, not directly useful for US/California personal finance rules.

Integration difficulty: Very high if adopted as the simulator runtime. Augur
would need to re-express scenarios as OpenFisca entities, variables, and
periods, and then bridge the results back into a monthly event log. That would
move the simulator away from its current event-sourced design.

Borrowing value: High. OpenFisca's parameter tree, bracket/scale YAML,
date-versioned values, variable formulas, YAML tests, and extension model are
good patterns for Augur's jurisdiction data. Its framework is a better
architecture reference than a runtime dependency.

Disposition: Borrow design patterns, not code or runtime. If Augur later needs
a public rules-as-code authoring surface, revisit an OpenFisca-inspired
adapter or parameter schema.

Sources:

- https://openfisca.org/doc/architecture.html
- https://openfisca.org/doc/installation/install-country-package.html
- https://openfisca.readthedocs.io/en/latest/coding-the-legislation/legislation_parameters.html
- https://github.com/openfisca/openfisca-core
- https://github.com/openfisca/country-template
- https://openfisca.org/fr/packages/

### PSLmodels Tax-Calculator

Tax-Calculator is an open-source Python microsimulation model for static
analysis of US federal individual income and payroll taxes. It has a
documented Python API centered on `Policy`, `Records`, and `Calculator`, and
`calc_all()` computes liability and intermediate variables. The license is
CC0/public-domain dedication, which is much easier for Augur than copyleft
engines.

Coverage:

- Federal individual income and payroll taxes only. No California or other
  state income tax.
- Strong annual federal filing-unit machinery: filing status (`MARS`), wages,
  dividends, short-term and long-term capital gains (`p22250`, `p23250`),
  Schedule A property tax (`e18500`), itemizable interest (`e19200`), and
  Section 1250 gain (`e24515`) are documented inputs.
- Itemized deduction outputs include state/local plus real-estate taxes,
  interest deduction, itemized deductions before phase-out, and regular
  taxable income.
- Does not model Augur's property and mortgage lifecycle. Augur must compute
  mortgage amortization, interest paid, property tax paid, basis, closing
  costs, depreciation, sale treatment, and then pass annual aggregates to
  Tax-Calculator if used.
- Does not own quarterly estimated tax/safe-harbor payment timing. Augur
  should keep those obligations and settlements.

Integration difficulty: Medium-low for federal validation; medium for
production federal liability. Mapping an Augur rollout-year to a Tax-Calculator
`Records` row is straightforward for simple cases, but harder for detailed
real-estate/rental cases. It is vectorized over filing units, so it can
validate batches of sampled rollouts.

Disposition: Best first external integration. Add a test-only or optional
adapter that feeds annual Augur aggregates into Tax-Calculator and compares
federal liability and selected intermediate fields for golden scenarios. Use
its CC0 rules/parameter names as references when Augur implements its own
minimal federal tax functions.

Sources:

- https://pslmodels.org/Catalog/Tax-Calculator.html
- https://taxcalc.pslmodels.org/usage/overview.html
- https://taxcalc.pslmodels.org/api/calculator.html
- https://taxcalc.pslmodels.org/guide/input_vars.html
- https://taxcalc.pslmodels.org/guide/output_vars.html
- https://taxcalc.pslmodels.org/about/LICENSE.html
- https://github.com/PSLmodels/Tax-Calculator

### OpenTaxSolver

OpenTaxSolver is a long-running C tax-preparation/calculation tool. The 2025
tax-year release page lists US 1040, Schedules 1-3 and A-D, forms 6251, 8949,
8889, and state versions including California. SourceForge lists GPLv2, C, and
Linux/Mac/Windows support.

Coverage:

- Useful form coverage for individual federal tax and a set of states,
  including California in current releases.
- Good for checking form-line outputs against a desktop tax-prep style tool.
- Not designed as a Python library. It is C code plus text/GUI form workflows.
- State support is limited to the forms included each tax year, not a
  generalized all-state tax engine.
- Property/mortgage coverage is form/input-line oriented, not a property
  lifecycle model.
- No native quarterly safe-harbor cashflow model suitable for Augur.

Integration difficulty: High. A subprocess wrapper is possible but brittle;
borrowing code is unattractive because of GPLv2 and form-centric C structure.
It could be useful as an occasional manual or automated spot-check for
California/federal form scenarios.

Disposition: Do not integrate as a runtime dependency. Keep as a reference or
spot-check tool if Tax-Calculator/PolicyEngine disagree with Augur on a form
line.

Sources:

- https://opentaxsolver.sourceforge.net/
- https://opentaxsolver.sourceforge.net/download2025.html
- https://sourceforge.net/projects/opentaxsolver/
- https://sourceforge.net/p/opentaxsolver/wiki/Home/

### IRS Direct File and Fact Graph

IRS Direct File is not a general simulator, but it is relevant prior art. The
GitHub README describes an interview-based federal filing service that
translates taxpayer answers into standard tax forms and uses a declarative
Fact Graph for incomplete return information. The repository also says not all
source code, documentation, and metadata are included.

Coverage:

- Federal filing workflow, not California filing and not Augur's personal
  finance simulator.
- Strong official-rule signal for tax-year-specific federal return logic and
  test scenarios.
- Scala/JVM/JavaScript system shape, not Python.
- The Fact Graph is interesting for explainable, partially-known tax-return
  facts, but Augur already has a different core truth source: event log plus
  yearly tax breakdown rows.

Integration difficulty: Very high for runtime use. Some concepts and fixtures
may be worth reading, but this is not a library candidate.

Disposition: Reference only. Borrow ideas for explanation, traceability, and
question-to-fact modeling if Augur builds user-facing tax input flows.

Sources:

- https://github.com/IRS-Public/direct-file
- https://github.com/IRS-Public

### TenForty

TenForty is a newer Python package that wraps OpenTaxSolver to compute US
federal and some state taxes. PyPI lists it as beta, MIT licensed, Python
`>=3.10`, with `evaluate_return` and `evaluate_returns` interfaces.

Coverage and fit:

- More Python-friendly than raw OpenTaxSolver.
- Inherits the tax-prep/form orientation and coverage limits of OpenTaxSolver.
- Because it is built on OpenTaxSolver, verify bundled-source licensing and
  redistribution posture before relying on the MIT package metadata.
- Not mature enough to be Augur's tax engine, but potentially useful for quick
  comparative checks.

Integration difficulty: Medium for experiments, high for production confidence.

Disposition: Watch, but do not adopt now.

Sources:

- https://pypi.org/project/tenforty/
- https://github.com/mmacpherson/tenforty

## Cross-Cutting Findings

### Annual tax liability and payment timing are separate concerns

External engines mostly answer "what is the annual tax liability for this tax
unit?" Augur also needs "when does cash leave the account, can the obligation
be funded, and did this rollout fail?" Quarterly estimates, safe harbor, Q4
January payment, and true-up should remain native Augur logic even if the
annual liability comes from an external engine.

### Property mechanics are mostly outside the engines

Tax engines accept property-tax paid, mortgage interest, real-estate tax,
rental income, depreciation, Section 1250 gain, or form-line equivalents.
They generally do not model:

- property purchase events;
- monthly property tax accrual from location data;
- mortgage amortization and payoff;
- adjusted basis evolution from closing costs and depreciation;
- Section 121 use/ownership clocks;
- co-owner equity ledgers;
- forced sale/liquidity policy behavior.

Augur should own those mechanics and generate annual tax inputs from the event
log.

### License posture matters

- Tax-Calculator: CC0/public-domain dedication. Lowest friction for tests,
  runtime dependency, or rule borrowing.
- PolicyEngine/OpenFisca: AGPL-3.0. Good OSS, but requires review before
  linking into any distributed Augur product or service.
- OpenTaxSolver: GPLv2. Fine as a user tool or subprocess in controlled
  contexts, unattractive for embedded rule borrowing.
- TenForty: MIT metadata, but it wraps/bundles OpenTaxSolver; verify inherited
  obligations before use.

### Stability and testing

- Tax-Calculator and OpenFisca/PolicyEngine have mature project structure,
  public docs, APIs, and tests.
- PolicyEngine US has active development and TAXSIM validation material, but
  the docs themselves show drift in at least one validation page import. Treat
  it as strong but still requiring local pinned-version tests.
- OpenTaxSolver is actively updated by tax year and useful for form-line
  checking, but it is maintained more like a tax-prep application than a
  library.

## Proposed Augur Integration Shape

Near term:

- Keep `augur/sim/tax.py` and jurisdiction YAML as the canonical runtime for
  the spike.
- Add focused federal golden tests from hand calculations and, later,
  Tax-Calculator for sampled annual aggregate rows.
- Keep quarterly estimated tax and safe-harbor logic in Augur.
- Expand Augur's own annual tax input record before integrating anything:
  ordinary income, STCG, LTCG, qualified dividends if needed, property tax
  paid, state income tax paid, mortgage interest paid, rental income,
  depreciation, Section 1250 gain, filing status, prior-year tax.

Medium term:

- Build an optional `TaxOracle` test adapter with implementations for:
  `AugurInternal`, `TaxCalculatorFederal`, and maybe `PolicyEngineUS`.
- Compare only scoped, explainable quantities. Start with federal income tax,
  taxable income, itemized/standard deduction choice, LTCG tax, NIIT, and
  mortgage/property-tax itemization.
- For California, run a PolicyEngine or OpenTaxSolver/TenForty spike with a
  small set of fixtures: W-2 only, capital gains, property tax + mortgage
  interest, and rental sale with Section 1250 if supported.

Long term:

- If the internal tax code grows too much, consider an explicit annual
  liability provider interface. The provider should consume Augur's annual
  aggregates and return liability plus breakdown rows. It should not own
  monthly events, property mechanics, liquidity policies, settlement, or
  rollout status.

## Decision Matrix

| Engine                  | Python/library fit                            | US federal              | CA/state                      | Property/mortgage mechanics    | Quarterly safe harbor | License                              | Integration difficulty             | Recommended role           |
| ----------------------- | --------------------------------------------- | ----------------------- | ----------------------------- | ------------------------------ | --------------------- | ------------------------------------ | ---------------------------------- | -------------------------- |
| PolicyEngine US         | Good Python package; OpenFisca-style entities | Strong                  | Promising; verify CA fixtures | Tax inputs only, not lifecycle | Not a fit             | AGPL-3.0                             | High runtime, medium oracle        | Secondary oracle/reference |
| OpenFisca Core/Template | Good framework, not rules                     | None by itself          | None by itself                | Framework only                 | Framework only        | AGPL-3.0                             | Very high                          | Borrow architecture        |
| PSL Tax-Calculator      | Good Python API                               | Strong                  | None                          | Tax inputs only                | Not a fit             | CC0                                  | Medium-low oracle                  | First federal oracle       |
| OpenTaxSolver           | CLI/C, not Python-native                      | Form-prep coverage      | Selected states incl. CA      | Form inputs only               | Not a fit             | GPLv2                                | High                               | Spot-check/reference       |
| IRS Direct File         | JVM/web app, not Python                       | Federal filing workflow | No state engine               | Filing facts, not lifecycle    | Filing workflow only  | Needs reuse review                   | Very high                          | Reference only             |
| TenForty                | Python wrapper over OTS                       | OTS-backed              | Some states                   | OTS-backed form inputs         | Not a fit             | MIT metadata; verify OTS inheritance | Medium experiment, high production | Watch/spot-check           |
