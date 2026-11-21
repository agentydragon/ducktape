# Tax engine evaluation

Reviewed: 2026-05-20. Engine capabilities and licenses are as read from each project's pages then; re-check before relying on one.

## Recommendation

Do not adopt an external tax engine as Augur's simulator runtime. Keep `augur/sim`'s month-by-month evaluator and
YAML-backed jurisdiction data (`sim/data/jurisdictions/`) as the production path. External engines are useful as rule
references and validation oracles for annual filing-unit liability, not as a replacement for the monthly cash and tax
model.

The reason is a data-model mismatch. Augur needs tax law to interact with asset sales, mortgage payments, property
taxes, forced liquidations, quarterly estimated payments, the January true-up and rollout failure. Tax engines compute
annual liability from return-shaped aggregates. That answers "what is this unit's annual tax?", not "when does cash
leave the account, can the obligation be funded, and did the rollout fail?". These stay Augur logic:

- Quarterly estimates, safe harbor, the January payment and the true-up.
- Property mechanics that engines take only as inputs: purchase events, monthly property-tax accrual from location
  data, mortgage amortization and payoff, basis from closing costs and depreciation, Section 121 clocks, co-owner
  equity and forced-sale policy.

Augur derives the annual tax inputs (`tax.TaxFacts`) from its own books.

Best practical path:

1. PSLmodels Tax-Calculator is the first external federal oracle for sampled annual computations.
2. PolicyEngine US is the candidate for wider federal and California validation, in a small spike.
3. Borrow OpenFisca and PolicyEngine design ideas (parameter trees, date-versioned values, YAML tests, explicit
   entities), not their runtime, unless the simulator becomes a tax-benefit microsimulation product.
4. OpenTaxSolver, IRS Direct File and TenForty are references or spot-checks only.

No oracle exists yet: nothing in the repository imports one.

## What Augur needs from tax law

- Python-callable code inside the existing Bazel/Python stack.
- Federal US and California individual income tax. Single filer first, other filing statuses later.
- Ordinary income, short- and long-term gains, qualified dividends, Net Investment Income Tax, itemized versus standard
  deduction, SALT cap, mortgage-interest deduction, Section 121 exclusion, Section 1250 recapture, rental income and
  depreciation.
- Quarterly estimates and safe-harbor timing, which is a simulator obligation problem as much as a tax-law one.
- A vector-friendly interface for many rollouts, with explainable per-year breakdown rows.

## Engines

- **PolicyEngine US** ([repository](https://github.com/PolicyEngine/policyengine-us)). Strongest surveyed US federal
  and state coverage, with TAXSIM-compatible validation, built on OpenFisca. It uses household and tax-unit entities
  and static annual microsimulation, takes mortgage interest and property tax as inputs only, and has no quarterly cash
  schedule. AGPL-3.0 needs legal review before linking into a distributed product. Production integration is hard;
  validation use is medium. Role: secondary oracle. California coverage needs a focused fixture spike.
- **OpenFisca Core and Country Template** ([architecture](https://openfisca.org/doc/architecture.html)). Framework and
  boilerplate with no US rules; the US package is PolicyEngine. Adopting it means re-expressing scenarios as entities,
  variables and periods and bridging results back to the monthly event frames. Role: architecture reference. Revisit an
  OpenFisca-inspired parameter schema if Augur needs a public rules-as-code authoring surface.
- **PSLmodels Tax-Calculator** ([repository](https://github.com/PSLmodels/Tax-Calculator)). Federal individual income
  and payroll tax only, no California. Strong annual filing-unit machinery (`Policy`, `Records`, `Calculator`, with
  documented inputs for filing status, gains, Schedule A property tax and interest, and Section 1250 gain), vectorized
  over filing units, CC0. No property lifecycle or quarterly timing. Role: first oracle, through a test-only or
  optional adapter.
- **OpenTaxSolver** ([site](https://opentaxsolver.sourceforge.net/)). C tax-preparation tool with federal forms and some
  states, including California. GPLv2, form-line oriented, not a Python library. Role: occasional spot-check when an
  oracle disagrees with Augur on a form line. Never a runtime dependency.
- **IRS Direct File and Fact Graph** ([repository](https://github.com/IRS-Public/direct-file)). Interview-based federal
  filing on Scala, JVM and JavaScript; not a library, and the published source is incomplete. Role: reference for
  explanation and traceability if Augur builds user-facing tax input flows.
- **TenForty** ([PyPI](https://pypi.org/project/tenforty/)). Beta Python wrapper over OpenTaxSolver with MIT metadata;
  verify the bundled OpenTaxSolver source's obligations. Inherits its coverage limits. Role: watch, and quick
  comparative checks.

PolicyEngine's validation docs showed drift in at least one page import, so pin versions and test locally.

## If an oracle is added

Build an optional test adapter (`TaxOracle`: Augur's own calculation, Tax-Calculator federal, maybe PolicyEngine US) and
compare scoped, explainable quantities only: federal income tax, taxable income, the itemized-versus-standard choice,
long-term-gain tax, NIIT and mortgage and property-tax itemization. Feed it Augur's annual aggregates; golden scenarios
compare liability and selected intermediate fields. For California, spike PolicyEngine or OpenTaxSolver/TenForty on a
few fixtures: W-2 only, capital gains, property tax plus mortgage interest, and a rental sale with Section 1250.

If the internal tax code outgrows itself, consider an explicit annual-liability provider interface that consumes Augur's
annual aggregates and returns liability plus breakdown rows. It must not own monthly events, property mechanics,
liquidity policy, settlement or rollout status.
