# Guyton–Klinger source contract

Research specification, not an implemented reproduction. Primary sources and
current Augur APIs checked on 2026-09-10. The destination is an inspectable
historical experiment using the common financial executor, not a guardrail-only
toy or another simulator.

## Which study are we reproducing?

| Source                                 | Reference experiment                                                                                                                                                   | Useful comparison target                                                                                                                                                                  |
| -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Guyton 2004][g04], October, pp. 54–62 | One January 1973 retirement; observed 1973–2003 history. The 40-year outcome assumes an average annual return 3% above inflation for 2004–2012, not 40 observed years. | Tables 3–6 separate terminal objectives, withdrawal freezes and the inflation cap; Table 2 specifies initial allocation excluding the first withdrawal.                                   |
| [Guyton–Klinger 2006][gk06], March     | Correlated lognormal Monte Carlo, 14,000 lives per scenario; fitted to 1973–2004 multi-asset history and a separate 1928–2004 single-equity history.                   | Table 2 isolates rules; Tables 3/4 compare capital-preservation/prosperity combinations for 65% single/multi-class equity; Tables 6/7 impose purchasing-power standards over 40/30 years. |
| [Klinger 2007][k07], August            | Tunable withdrawal profiles, 60/30/10 allocation, annual rebalancing and 1,000 simulations per scenario.                                                               | A later study, not a clarification that permits silently replacing the 2006 portfolio rule.                                                                                               |

**Historical GK replay is a new experiment applying the 2006 rules to observed
sequences.** It cannot reproduce the 2006 Monte Carlo frequencies. Keep three
labels distinct: the 2004 historical-plus-stipulated-tail replication; historical
replay of the 2006 policy; reconstructed 2006 stochastic table comparisons.

The [author's journal-layout reprint][print06] and FPA's archived web PDF agree
on the inspected Tables 6/7. Table 7's 30-year title nevertheless retains a
“Year 40” purchasing-power column heading in both editions. Record that source
defect; do not invent a 40th-year observation for a 30-year run.

## Financial conventions

The 2006 methodology takes the whole annual withdrawal at the year's opening,
then applies annual asset returns at year-end before the next withdrawal.
Inflation adjustments use the preceding year's CPI change. The paper specifies
correlated asset/inflation draws, not independent stock and inflation samples.
Its parameter matrix, original random stream and complete source panel are not
published in the paper. Exact random paths are not a prerequisite. [Methodology][gk06]

### Spending rules and versions

Let `w0` be the initial withdrawal divided by initial wealth; `W` the current
annual withdrawal candidate; `V` opening wealth before that withdrawal.

| Rule                       | Source convention                                                                                                                                                  |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Original withdrawal freeze | G2004 first tests declining portfolio **value**, then selects negative investment **return** instead. Those are different after a withdrawal.                      |
| Modified withdrawal freeze | GK2006 freezes an inflation increase after a negative portfolio-return year only when the candidate withdrawal rate would exceed `w0`. No catch-up.                |
| Inflation cap              | G2004 caps increases at 6%, with no catch-up. GK2006 drops this cap when the capital-preservation rule is enabled; do not attach it to the final four-rule recipe. |
| Capital preservation       | Strict `W / V > 1.2 * w0`; reduce the withdrawal by 10%. Disabled during the final 15 years. The reduced amount becomes next year's basis.                         |
| Prosperity                 | Strict `W / V < 0.8 * w0`; increase the withdrawal by 10%, establishing next year's basis.                                                                         |

[G2004, Withdrawal Decision Rules / What About Inflation][g04];
[GK2006, Withdrawal Enhancements / Portfolio Rescue / When Prosperity Rules][gk06];
[K2007, explicit inequalities and examples][k07]. Equality does not trigger a
guardrail. The ordinary inflation adjustment and a guardrail adjustment must
not accidentally apply inflation twice. Remaining ordering choices are below.

### Portfolio management is part of the policy

The source rule sells positive-return overweight allocations into cash.
Withdrawal sources are ordered: equity overweight, fixed-income overweight,
cash, remaining fixed income, then remaining equities ordered by prior-year
performance. Losing equity is protected when cash/fixed income can fund the
withdrawal. This is not periodic restoration of every underweight sleeve.
[G2004, Portfolio Management Decision Rule][g04]; [GK2006, PMR][gk06].
[K2007's methodology][k07] explicitly contrasts its annual rebalancing with GK2006.

The multi-asset target percentages, in source order, are:

| Sleeve                     | 50% equity | 65% equity | 80% equity |
| -------------------------- | ---------: | ---------: | ---------: |
| Cash / fixed income        |    10 / 40 |    10 / 25 |    10 / 10 |
| Large value / large growth |    10 / 10 |    13 / 13 |    15 / 15 |
| Small value / small growth |      7 / 7 |      9 / 9 |    10 / 10 |
| International / REIT       |     11 / 5 |     15 / 6 |    20 / 10 |

[GK2006 Table 1][gk06]. REIT is a traded portfolio sleeve, not a house.
G2004 places the first withdrawal in cash **before** allocating the remainder
according to Table 2. Its cash sleeve earns the stated money-market/T-bill return;
it is not idle zero-return checking cash. [G2004, pp. 4–5 of the web PDF][g04]

## Evidence and reproducibility limits

G2004 names Russell 1000/2000 value/growth, MSCI EAFE and the NAREIT-through-1987
/ Wilshire-REIT-from-1988 splice. Before 1979, the four US sleeves use Windsor,
Morgan Growth, Pennsylvania Mutual and Explorer funds. Fixed income uses Babson
before the paper's stated 1987 Lehman Aggregate transition. Cash uses Franklin
Money Market from 1977 and 91-day bills earlier. These splice definitions matter;
current ETFs or Trinity's CRSP/Aaa construction are not equivalent. [G2004][g04]

Accessibility checked, without purchasing data or assuming redistribution rights:

- [LSEG's free history page][lseg] generally supplies two years, not the required
  original full panel. Exact fund share classes, index vintages, EAFE return
  variant/currency and missing early data remain to be pinned.
- [Nareit][nareit] publishes annual sector returns from 1972; selecting the exact
  historical index and later Wilshire splice still requires verification.
- [FRED CPIAUCNS][cpi] supplies monthly unadjusted CPI; the papers do not identify
  enough CPI details to silently choose December/December over annual averages.
- [Damodaran's annual history][damodaran] provides S&P-with-dividends, bills and
  bond returns. It enables a sourced **single-equity adaptation**, not proof of
  the authors' exact bond/cash inputs or a six-equity replication.

Pin dataset bytes/revision, coverage, units, total-return definition, splice
years and calendar transformation beside each actual source. Fetch/load once;
keep explicit original start years/IDs across policy cells and selected replay.
Use January starts with complete windows for the historical extension. Do not
inherit Trinity's every-month starts, impute missing years, or manufacture IID
uncertainty from overlapping retirements. The 1973 reference is one path.

## Declared three-sleeve adaptation

Historical replay of the 2006 rules runs this package on three
sleeves, not Table 1's eight. Targets are the 65%-equity column with its six equity
sleeves merged:

| Sleeve | Target | Annual series, in the style of [Damodaran's history][damodaran]                        |
| ------ | -----: | -------------------------------------------------------------------------------------- |
| Cash   |    10% | 3-month T-bill return, all of it interest                                              |
| Bonds  |    25% | 10-year Treasury: coupon at the prior year-end yield, and the rest of its total return |
| Equity |    65% | S&P 500: dividends over the prior year-end index level, and price return               |

Spending indexes to annual CPI change. `--taxes none` is the paper control: each
sleeve is a tax-free total-return proxy unit, with no investor taxes or added fees.
`--taxes federal-ca` declares a taxable variant, not a paper replication:

- **Split returns.** Units move by price return; each year's income is paid in its
  December into the sleeve's income account. Bill and bond interest is Treasury
  interest, federally taxable and California-exempt; dividends are taxed as ordinary
  income until qualified-dividend character lands. Sales realize FIFO lot gains, long
  term from 12 months as the engine counts them (statute: more than a year).
- **W is gross.** The withdrawal leaves the portfolio and its year's tax is paid out of
  it: spendable = W − tax. Guardrails test W / V as in the paper. The TAXES reading
  below schedules the payments.
- **Brackets fixed.** The bundled federal and California single-filer tables (2024
  law, with NIIT and California's 1% surtax) hold in nominal dollars in every historical
  year, and every window starts with $1M nominal. Early start years thus pay 2024
  nominal thresholds at a far lower price level, and inflation pushes a window's
  constant real income into higher brackets.
- **Taxpayer.** A single California resident with no other income, on the standard
  deduction (state tax is not itemized), with no prior-year tax: no estimated
  instalments, so each year's whole tax falls due at the next January review.

`records.json` carries each year's W, federal tax (NIIT included), California tax,
their total and spendable, nominal and in the window's January dollars; the study
summary leads with real spendable. Because W is gross and income reinvests where it
was earned, the taxed portfolio tracks the untaxed control up to rounding and the final
year's tax its reserve does not cover: taxes show in spending, not in wealth or
guardrail triggers. What this adaptation cannot claim:

- PMR's last funding stage ranks remaining equities by prior-year performance; with
  one equity sleeve that ranking collapses.
- Its results are not comparable with the paper's tables, which come from a fitted
  eight-sleeve Monte Carlo.
- The 10-year Treasury stands in for the aggregate bond index; duration and credit
  differ.
- Overlapping January-start windows share years; they are not independent trials.
- Taxed, it omits qualified dividends, CPI-indexed brackets, estimated instalments,
  wash sales, the SALT deduction and fund expense ratios or fees; the bond coupon is the
  prior year-end yield on the unit's value, not a held bond's par.

## Decisions still needed before a faithful label

| ID        | Specific ambiguity / required decision                                                                                                                                                                                                                                                                                  |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ORDER     | GK2006 describes guardrail triggers using the rules in effect, then says other rules apply to the adjusted amount. Figure 1 is not execution pseudocode. Pin the candidate used by each comparison, inflation/freeze/guardrail order and deflation handling; retain threshold-crossing controls for competing readings. |
| PORTFOLIO | Pin overweight denominator and timing, performance ranking direction/ties, and funding versus surplus reinvestment. G2004's sweep names equities; GK2006's first PMR bullet says asset classes. Do not hide this variant.                                                                                               |
| OPENING   | Preserve G2004's explicit withdrawal reserve for its replication. Establish whether GK2006 inherited it; its Table 1 does not restate the exclusion. Specify the final-15-year boundary by year index.                                                                                                                  |
| DATA      | Recover the exact panel; the three-sleeve adaptation above names replacements for historical replay only. Resolve 2004's 30-year/through-2003/2004–2012 endpoint bookkeeping before matching its terminal tables. Replacing its stipulated tail with subsequently observed returns is another experiment.               |
| METRICS   | Pin the trigger-count population and source Table 7 heading interpretation. Initial wealth/rounding and sample-statistic conventions are needed before digit-level stochastic comparisons.                                                                                                                              |

Start the paper control without investor taxes or added trading/advisory fees,
labeling that modeling choice; do not infer zero embedded fund expenses from the
mixed fund/index history. A taxable personal extension needs actual price/payout
character and tax coverage and is not the paper replication. No source claim of
complete statutory treatment follows from these studies.

### Policy readings of the three-sleeve adaptation

<policy.py> pins one reading of ORDER, PORTFOLIO and
OPENING for the declared cash/bond/single-equity adaptation, and of tax payment for its
taxable variant. The faithful-label decisions above stay open.

- **ORDER.** Scale last year's withdrawal by the preceding year's CPI ratio;
  deflation lowers it, since the freeze text names only increases. The freeze
  withholds an increase when the preceding year's investment return was
  negative and the inflated amount exceeds `w0 * V`. At most one guardrail then
  tests that candidate against `V`; its result is next year's basis. The rate
  tested is thus the rate that would be taken, and inflation applies once.
  Investment return compares the next opening wealth with the settled wealth
  just after the withdrawal, so a withdrawal is never a loss and a 0% year is
  not negative. The basis stays exact; each payment rounds half up to the
  currency quantum.
- **PORTFOLIO.** A sleeve's overweight is its value above its target share of
  `V`, at the review's prices before any trade, and counts only if the sleeve's
  unit price rose over the preceding year: GK2006's "asset classes", so bonds
  sweep too. Funding: rising-overweight equity, rising-overweight bonds,
  checking then the cash sleeve, remaining bonds, remaining equity, FIFO lots
  within a sleeve, each unit reserved once. A losing sleeve therefore funds only
  after cash and bonds run out. After the withdrawal, what funding left of the
  rising sleeves' excess sells into the cash sleeve; nothing is bought back.
- **OPENING.** Targets cover all opening wealth, without G2004's first-withdrawal
  reserve, which GK2006 Table 1 does not restate. Year 0 has no prior-year
  returns, so nothing is overweight and the first withdrawal comes from cash.
  Capital preservation applies at zero-based year index `t < years - 15`: in 30
  years, the first 15 withdrawals.
- **PORTFOLIO, taxed.** A sleeve is its lots plus its unspent payouts. It rises when
  its total return, price change plus payouts, was positive, as the untaxed proxy's
  price does. Funding and the sweep draw a sleeve's payouts before selling its lots;
  what they leave reinvests in that sleeve, so sleeve values match the proxy's.
- **TAXES, prior-year reserve.** Each review first keeps invested the part of W that
  repays tax the portfolio advanced, then moves the last closed year's assessed tax
  (none in year 0) into the tax reserve, outside `V`, and spends the rest. Tax
  claims are paid from the reserve; what it lacks is advanced through the funding
  stages (overweight stages only at a review), and repaid from the next withdrawals.
  The review after a tax year closes spends the reserve's remainder once that year's
  claims are paid. So a year's spending is W less its tax, the portfolio's net outflow
  is W, and the investment-return test adds advances back. The final year's settlement
  falls past the horizon: terminal wealth nets the tax its reserve does not cover.

## Outputs that make comparisons meaningful

GK2006 success requires at least $1 at the terminal horizon, not merely completion
with zero wealth. Its purchasing-power statistics are medians **over successful
lives**: inflation-deflated total withdrawals relative to `years * initial_W`,
and final-year withdrawal relative to `initial_W`. Its 95%/99% standards combine
success and purchasing-power criteria; they are not statistical confidence
intervals. [GK2006, Life Expectancy / When Prosperity Rules / Confidence Standards][gk06]

Report those source-conditioned quantities separately from all-path observed
paid consumption, intended consumption, minimum real spending, cut/freeze/raise
counts and time below a chosen spending anchor. Carry denominator and path IDs.
Use canonical payment receipts, final books and stop causes. On an observed
month an unattempted withdrawal has unknown requested amount but zero paid;
post-stop months are absent, not a stream of future zero observations. Keep a
valid unfunded withdrawal distinct from a malformed action/financial-code error.
Scan a declared initial-rate grid; Trinity's monotone SAFEMAX bisection is not
proved for a strategy that changes future spending at endogenous thresholds.

[g04]: https://www.financialplanningassociation.org/sites/default/files/2021-10/OCT04%20JFP%20Guyton%20PDF.pdf
[gk06]: https://www.financialplanningassociation.org/sites/default/files/2021-11/2006%20-%20Guyton%20and%20Klinger%20-%20Decision%20Rules%20and%20SWR%20%281%29.PDF
[print06]: https://cornerstonewealthadvisors.com/wp-content/uploads/2014/09/08-06_WebsiteArticle.pdf
[k07]: https://www.financialplanningassociation.org/sites/default/files/AUG07%20Klinger.pdf
[lseg]: https://www.lseg.com/en/ftse-russell/index-resources/historic-index-values
[nareit]: https://www.reit.com/data-research/reit-market-data/report/annual-index-values-returns
[cpi]: https://fred.stlouisfed.org/series/CPIAUCNS
[damodaran]: https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/histret.html
