# Tax coverage gate for the household experiment

Decision checklist for **GT** in [the landing plan](roadmap.md); code references
checked on devel 2026-09-26. The owner's scope decisions are recorded here; this
is not a certification of current tax fidelity. No private records are needed in
this PR. Record private facts downstream; publish generic supported cases here.

## Scope

- [x] **Start date and opening tax facts.** A simulation starts at the current
      month, with the current tax year's year-to-date facts as inputs: income,
      realized gains (e.g. an equity tender earlier this year) and estimated or
      withheld tax already paid. The coming true-up is a cash claim within the
      horizon. Starting from post-sale cash must neither forget the earlier gain
      nor sell the shares again. Keep the actual tender's tax characterization
      downstream.
- [x] **Filing unit and residence.** Federal + California, single filer, US
      residence, fixed throughout.
- [x] **Investable products.** Equity funds (e.g. VT, VXUS), US Treasury bond
      funds, municipal bonds and funds (e.g. California munis), direct bonds, and
      simulated TLH direct indexing (a Wealthfront-style
      managed portfolio, `sim/tlh.py`). Qualified dividends and NIIT are in scope:
      they materially affect these equity funds and a high-income year. A
      total-return equity index is a tax-free study proxy, not a distributing
      taxable fund.
- [x] **Account treatment for these products.** Taxable accounts only; no
      retirement-account regime. Sales select lots FIFO. Distributions are paid as
      cash; reinvesting them is the household policy's choice. Trading fees and
      fund expense ratios are out of scope for now (see Later).
- [x] **Other material income/deductions.** The federal and California
      mortgage-interest deduction is in scope for a plausible mortgage: itemized
      versus standard deduction, the SALT cap interaction and federal/California
      acquisition-debt limits. Charitable gifts are out of scope for now (see
      [Later](#later)). No other outside income or deduction enters.
- [x] **Law years.** Current law held flat for future years, brackets and
      thresholds fixed in nominal dollars (no CPI indexing): an explicit, labeled
      assumption, not a forecast. "Current law" means the current tax year's
      tables; updating the bundled 2024 tables belongs to the calendar/law-year
      slice.
- [x] **Payment timing.** Model annual liability, quarterly estimated payments
      and the year-end true-up. Obligations are paid on schedule (an explicit
      assumption): no late payment, underpayment penalties or refund timing.
- [x] **Backstop boundary.** Domestic only; no cross-border model. Another
      jurisdiction (e.g. the Czech Republic) is out of scope and not planned soon;
      a Europe arm needs its own residency, FX and cross-border-tax scope under GX.
- [ ] **BOND conventions.** Clean/dirty price, accrual and premium/discount
      treatment for direct bonds belong to BOND.

## Current coverage and acceptance work

“Present” means there is code and a relevant acceptance suite, not that all
statutory variations are covered. Source paths below are relative to Augur.

| Area                              | Current evidence / limitation                                                                                                                                                                                                                                                                                                                                                         | Case needed for the selected scope                                                                                                                                                                                                                                                                                                                                        |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Calendar and annual rules         | `sim/data/jurisdictions/{federal_us,california}.yaml` explicitly use 2024 brackets. A world has a month count, not a dated tax calendar; `Jurisdiction` is not a law-year schedule. SALT has a separate year-indexed cap mechanism.                                                                                                                                                   | A start at the current month of the current tax year, its first year-end, later years under current law held flat in nominal dollars, and a partial final year. Update the 2024 tables to the current tax year; never label them current law.                                                                                                                             |
| Opening tax state                 | `TaxProfile` supplies an aggregate `prior_year_tax`; `TaxBook.enroll` (`sim/tax_year.py`) starts income, gain/carryover and payment state empty. No opening YTD/carryover import surface.                                                                                                                                                                                             | The current year's year-to-date income, realized gains and estimated/withheld payments, so the coming true-up is a cash claim within the horizon. Same continuation started before versus after a synthetic taxable sale, including a brought-forward loss; no tax reset or duplicated sale.                                                                              |
| Ordinary income, gains and losses | `sim/tax.py` and `sim/public_sales_test.py` cover ordinary/LTCG stacking, deduction interaction, netting and carryforward. Lot dates/classification are monthly; capital-loss state is shared across jurisdictions.                                                                                                                                                                   | Compare statutory holding-period boundaries, mixed short/long gains/losses, multi-year loss use, and different federal/state opening carryovers where applicable. A test of the engine against itself is insufficient.                                                                                                                                                    |
| Equity/fund distributions         | `SecurityDistribution` routes issuer-character slices as interest; income is `OrdinaryIncome` or `InterestIncome`. Equity samplers emit total-return prices. Qualified dividends and distinct fund capital-gain/return-of-capital processing are absent.                                                                                                                              | Compatible price-return plus payout amounts for the equity funds in scope, qualification/holding-period cases, federal qualified-dividend versus California ordinary treatment, reinvestment basis and no double-counted return. Either implement other payout kinds these funds need or reject those products.                                                           |
| Treasury/muni interest            | Direct interest and configured distribution slices apply issuer exemptions; `sim/{income_sources,security_distributions,bond}_test.py` exercise them.                                                                                                                                                                                                                                 | Federal, California and out-of-state treatment for the muni funds and bonds actually held; Treasury fund interest is federally taxable and California-exempt only when the fund qualifies. Fund-level eligibility and annual tax reporting are not derivable solely from a constituent issuer's name.                                                                     |
| High-income taxes                 | `sim/tax.py` assesses NIIT and California's 1% surcharge above $1M taxable income as separate components of each jurisdiction's total, with rates and thresholds sourced in `sim/data/jurisdictions/`; `sim/{test_tax,year_end_tax_test}.py` hold independently calculated cases. NII is taxable interest plus net gains and recapture after loss netting; muni interest is excluded. | Net rental income is not NII because `OrdinaryIncome` merges rent with wages: rental arms understate NIIT until rent has its own category. Form 8960 line 9 deductions (investment interest, state income tax allocable to NII) are not modeled and overstate NIIT where they apply. Qualified dividends need adding to `is_investment_income` when that category lands.  |
| Payments versus liabilities       | `sim/tax_authority.py` emits equal first-three installments from one aggregate prior-year target, then Q4/true-up after the year closes. This is functioning payment machinery, not full federal/CA safe-harbor support.                                                                                                                                                              | Per-jurisdiction estimated-payment schedules (federal equal quarters, California's unequal installment proportions) and the true-up, with the selected safe-harbor rule only as it sets the estimate amounts, including large one-off gains and payments funded by further taxable sales. No penalty or interest modeling.                                                |
| Itemization and housing           | Mortgage interest, SALT cap schedules, rental deductions/depreciation and sale exclusions/recapture have existing code/tests. Their presence does not establish complete current-year eligibility, phase-outs or passive-loss treatment.                                                                                                                                              | Required: a single filer's federal and California mortgage-interest deduction, itemized versus standard, with the SALT cap (including its current-law income phase-out) and each jurisdiction's acquisition-debt limit, plus the existing rental cases, purchase/sale dates and federal/state differences. Preserve working housing mechanics while adding missing rules. |
| Direct bonds                      | Existing nominal/TIPS slice is initial, par-held and nontradable, with coupon/redemption and indexed-principal processing.                                                                                                                                                                                                                                                            | Retain its controls. Before tradable/off-par arms: coupon dates, clean/dirty price, accrued interest, premium/discount/OID, basis and tax character at sale/redemption. Do not infer full TIPS coverage from indexed principal alone.                                                                                                                                     |
| Other accounts/residencies        | No general retirement-account withdrawal/tax regime or cross-border residency transition contract.                                                                                                                                                                                                                                                                                    | US residency throughout; reject other residencies and filing statuses. Add an independent branch for any other account type the account-treatment decision requires. Changing a currency label or jurisdiction list is not a residency model.                                                                                                                             |

### Later

- [ ] Consider indexing brackets and thresholds to the simulated CPI in the
      calendar/law-year slice (judged low significance).
- [ ] Charitable gifts: out of scope for now; scope their federal/California
      deduction before any experiment that includes them.
- [ ] Trading fees and fund expense ratios: out of scope for now; model them
      before comparing products whose costs differ materially.

### Existing indexed-bond coverage check

- [ ] Independently reconcile maturity-period CPI changes, indexed redemption
      and final taxable accretion in the currently supported TIPS slice. This is
      an unverified coverage question, not a diagnosed tax bug. Keep it under
      GT/TAX's existing-bond controls, separate from new trading/off-par features.

### Housing basis reconciliation

`sim/property.py` adds `buyer_closing_cost` to the purchase's recorded
adjusted basis, but `Properties.sell` reconstructs gain basis from purchase
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

## Implementation slices

1. **Calendar/opening-state slice:** start at the current month with the current
   tax year's tables and year-to-date facts, later years at current law held
   flat in nominal dollars, and explicit final-year treatment.
2. **Distribution slice:** supported price/payout construction plus tax character,
   qualified dividends (ordinary income in California) and basis after
   reinvestment. Coordinate with BIND; the tax-free proxy remains useful only
   under its declared assumption.
3. **Payment slice:** per-jurisdiction estimated-payment schedules and the
   true-up, with the selected safe-harbor rule only as it sets the estimates; no
   penalty or interest modeling. Annual liability and cash timing must each have
   an oracle.
4. **Mortgage-interest slice:** independently test the single-filer federal and
   California deduction, itemized versus standard, with the SALT cap's income
   phase-out and each jurisdiction's acquisition-debt limit.

These are separate review units, not a serial dependency chain. Existing
statute cases remain controls. Recheck the prior tax-engine evaluation's current
coverage/licensing before choosing an external oracle; do not select a new runtime
backend merely because an annual calculator is useful for comparison.

GT's scope is recorded above; BOND's conventions remain open. GT closes when they
are decided, every relevant gap maps to a bounded
implementation/acceptance case, and exclusions are explicit. TAX closes only when
those cases pass through canonical execution.
