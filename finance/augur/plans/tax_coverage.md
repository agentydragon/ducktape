# Tax coverage gate for the household experiment

Decision checklist for **GT** in [the landing plan](roadmap.md), grounded at
`e9d11f92e6` on 2026-09-09. This is not a certification of current tax fidelity
or approval of a particular household scope. No private records are needed in
this PR. Record private facts downstream; publish generic supported cases here.

## Scope to confirm before implementing TAX

- [ ] **Start date and opening tax facts.** Is this a mid-year start after the
      tender, or a future January start? Include income/gains already realized,
      withholding/estimated payments, unpaid liabilities and carryovers. Starting
      from post-sale cash must neither forget the earlier gain nor sell the shares
      again. Keep the actual tender's tax characterization downstream.
- [ ] **Filing unit and residence.** Confirm filing status, jurisdictions and
      whether they remain fixed. Federal + California is the existing configuration
      to audit, not an assumption about the user. Joint filers cannot be represented
      by silently running two independent single-filer profiles.
- [ ] **Investable products.** Name the first comparison's cash/deposit or money
      fund, equity funds/stocks, taxable/municipal bond funds and any direct bonds.
      Specify account tax treatment, reinvestment, sale-lot selection, fees and any
      harvesting. A total-return equity index is a tax-free study proxy, not a
      distributing taxable fund.
- [ ] **Other material income/deductions.** Decide which outside income,
      mortgage/itemization, charitable gifts, property gains and special equity
      treatments actually enter the first experiment. Declare exclusions explicitly;
      do not approximate an applicable rule by omitting it.
- [ ] **Law and payment assumptions.** Pin actual law years and the assumption
      for future years; distinguish annual liability from when cash leaves. Decide
      whether underpayment/refund timing matters before excluding it.
- [ ] **Backstop boundary.** The first domestic report can exclude relocation.
      A Europe arm needs its own residency, FX and cross-border-tax scope under GX;
      cheaper USD spending is not its substitute.

## Current coverage and acceptance work

“Present” means there is code and a relevant acceptance suite, not that all
statutory variations are covered. Source paths below are relative to Augur.

| Area                              | Current evidence / limitation                                                                                                                                                                                                                           | Case needed for the selected scope                                                                                                                                                                                                               |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Calendar and annual rules         | `sim/data/jurisdictions/{federal_us,california}.yaml` explicitly use 2024 brackets. `Scenario` has a month count, not a dated tax calendar; `Jurisdiction` is not a law-year schedule. SALT has a separate year-indexed cap mechanism.                  | A start partway through a real year, its first year-end, next-year rules and a partial final year. Pin future indexation/law assumptions; never label the fixed 2024 tables as current law.                                                      |
| Opening tax state                 | `TaxProfile` supplies an aggregate `prior_year_tax`; `simulate_rollout` initializes income, gain/carryover and payment state. No opening YTD/carryover import surface is exposed.                                                                       | Same continuation started before versus after a synthetic taxable sale, including earlier payments and a brought-forward loss; no tax reset or duplicated sale.                                                                                  |
| Ordinary income, gains and losses | `rust/tax.rs` and `rust/public_sales_test.py` cover ordinary/LTCG stacking, deduction interaction, netting and carryforward. Lot dates/classification are monthly; capital-loss state is shared across jurisdictions.                                   | Compare statutory holding-period boundaries, mixed short/long gains/losses, multi-year loss use, and different federal/state opening carryovers where applicable. A test of the engine against itself is insufficient.                           |
| Equity/fund distributions         | `SecurityDistribution` routes issuer-character slices as interest; `IncomeSource` has ordinary/interest variants. Equity samplers emit total-return prices. Qualified dividends and distinct fund capital-gain/return-of-capital processing are absent. | Compatible price-return plus payout amounts, qualification/holding-period cases, distribution character, reinvestment basis and no double-counted return. Either implement other payout kinds needed by the products or reject those products.   |
| Treasury/muni interest            | Direct interest and configured distribution slices apply issuer exemptions; `sim/testing/{income_sources,security_distributions,bonds}.py` exercise them.                                                                                               | Federal, in-state and out-of-state treatment for the _actual product_. Fund-level eligibility and annual tax reporting are not derivable solely from a constituent issuer's name.                                                                |
| High-income taxes                 | `TaxRules` has no NIIT fields; the California YAML explicitly defers its additional high-income tax.                                                                                                                                                    | NIIT below/across/above the relevant threshold, including excluded interest; California's additional tax if applicable. These are acceptance blockers for affected reports, not optional “later refinements.”                                    |
| Payments versus liabilities       | `engine/obligations.rs::tax_obligations` emits equal first-three installments from one aggregate prior-year target, then Q4/true-up at month 12. This is functioning payment machinery, not full federal/CA safe-harbor support.                        | Separate jurisdictions, applicable prior/current-year safe-harbor rules, installment fractions/dates, withholding, large one-off gains, annualized-income option if selected, overpayments/refunds and payments funded by further taxable sales. |
| Itemization and housing           | Mortgage interest, SALT cap schedules, rental deductions/depreciation and sale exclusions/recapture have existing code/tests. Their presence does not establish complete current-year eligibility, phase-outs or passive-loss treatment.                | Only the household's selected mortgage/rental/gift cases, including federal/state differences and purchase/sale dates. Preserve working housing mechanics while adding missing rules.                                                            |
| Direct bonds                      | Existing nominal/TIPS slice is initial, par-held and nontradable, with coupon/redemption and indexed-principal processing.                                                                                                                              | Retain its controls. Before tradable/off-par arms: coupon dates, clean/dirty price, accrued interest, premium/discount/OID, basis and tax character at sale/redemption. Do not infer full TIPS coverage from indexed principal alone.            |
| Other accounts/residencies        | No general retirement-account withdrawal/tax regime or cross-border residency transition contract.                                                                                                                                                      | Explicitly exclude irrelevant cases; add an independent branch when required. Changing a currency label or jurisdiction list is not a residency model.                                                                                           |

### Housing basis reconciliation

`rust/engine/property.rs` adds `buyer_closing_cost` to the purchase's recorded
adjusted basis, but `settle_property_sales` reconstructs gain basis from purchase
price, later improvements and depreciation, omitting that opening cost. The two
basis calculations need one supported cost-classification contract under GT/TAX.
Pin which acquisition/financing costs are capitalized, deducted or excluded;
then test purchase, depreciation and disposal against independently calculated
basis and gains, including nonzero opening costs. Do not preserve the mismatch
as a regression expectation. Tax basis is separate from gross market valuation;
this work blocks only housing arms that rely on this treatment.

### Primary sources to pin into acceptance cases

Consulted 2026-09-09. These identify the governing questions; the implementation
PR must pin the selected tax-year publication/form and independently calculated
expected values. No personal return is being calculated here.

- [IRS annual inflation adjustments](https://www.irs.gov/newsroom/inflation-adjusted-tax-items-by-tax-year):
  use the selected year's brackets/deductions, including subsequent legislation,
  rather than extrapolating the bundled 2024 values by accident.
- [IRS Publication 550 (2025)](https://www.irs.gov/publications/p550):
  investment-income character, qualified-dividend holding periods, basis,
  capital-loss carryovers, bond interest and premium/discount treatment.
- [IRS Form 8960 instructions (2025)](https://www.irs.gov/instructions/i8960):
  NIIT is separate from regular capital-gains tax and depends on filing-unit
  income; a universal extra capital-gain rate is not an equivalent model.
- [IRS Publication 505 (2026)](https://www.irs.gov/publications/p505):
  current-year versus prior-year payment tests, the higher-income prior-year
  percentage, withholding and annualized-income alternatives. Those cannot all
  be represented by the existing single aggregate target.
- [California estimated payments](https://www.ftb.ca.gov/pay/estimated-tax-payments.html):
  installment proportions differ from equal quarters; higher-income rules can
  change or remove the prior-year safe-harbor option. This matters even if annual
  federal and state liabilities were already correct.
- [California Schedule CA instructions (2025)](https://www.ftb.ca.gov/forms/2025/2025-540-ca-instructions.html)
  and [Form 540 instructions](https://www.ftb.ca.gov/forms/2025/2025-540-instructions.html):
  federal/state distribution differences, fund exemption eligibility and the
  additional high-income tax. Preserve those distinctions when testing funds.

## Small follow-on changes, once scope is selected

1. **Calendar/opening-state slice:** dated start, selected law years, prior tax
   facts and explicit final-year treatment. Retain a declared frozen-law mode
   for controlled studies; do not silently make that the household default.
2. **Distribution slice:** supported price/payout construction plus tax character,
   qualified dividends and basis after reinvestment. Coordinate with BIND; the
   tax-free proxy remains useful only under its declared assumption.
3. **High-income slice:** independently test NIIT and any applicable state
   addition. It need not wait for the whole instrument refactor.
4. **Payment slice:** jurisdiction-specific schedules and selected safe-harbor,
   withholding and true-up/refund semantics. Annual liability and cash timing
   must each have an oracle.

These are separate review units, not a four-step serial dependency chain. Existing
statute cases remain controls. Recheck the prior tax-engine evaluation's current
coverage/licensing before choosing an external oracle; do not select a new runtime
backend merely because an annual calculator is useful for comparison.

GT closes when the owner-selected scope is recorded, every relevant gap maps to a
bounded implementation/acceptance case, and exclusions are explicit. TAX closes
only when those cases pass through canonical execution. Neither gate is closed
by this checklist alone.
