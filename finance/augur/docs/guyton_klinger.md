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

## Decisions still needed before a faithful label

| ID        | Specific ambiguity / required decision                                                                                                                                                                                                                                                                                  |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ORDER     | GK2006 describes guardrail triggers using the rules in effect, then says other rules apply to the adjusted amount. Figure 1 is not execution pseudocode. Pin the candidate used by each comparison, inflation/freeze/guardrail order and deflation handling; retain threshold-crossing controls for competing readings. |
| PORTFOLIO | Pin overweight denominator and timing, performance ranking direction/ties, and funding versus surplus reinvestment. G2004's sweep names equities; GK2006's first PMR bullet says asset classes. Do not hide this variant.                                                                                               |
| OPENING   | Preserve G2004's explicit withdrawal reserve for its replication. Establish whether GK2006 inherited it; its Table 1 does not restate the exclusion. Specify the final-15-year boundary by year index.                                                                                                                  |
| DATA      | Recover the exact panel or approve named replacements. Resolve 2004's 30-year/through-2003/2004–2012 endpoint bookkeeping before matching its terminal tables. Replacing its stipulated tail with subsequently observed returns is another experiment.                                                                  |
| METRICS   | Pin the trigger-count population and source Table 7 heading interpretation. Initial wealth/rounding and sample-statistic conventions are needed before digit-level stochastic comparisons.                                                                                                                              |

Start the paper control without investor taxes or added trading/advisory fees,
labeling that modeling choice; do not infer zero embedded fund expenses from the
mixed fund/index history. A taxable personal extension needs actual price/payout
character and tax coverage and is not the paper replication. No source claim of
complete statutory treatment follows from these studies.

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
