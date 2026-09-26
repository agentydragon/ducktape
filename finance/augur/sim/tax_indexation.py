"""How a taxpayer's statutory amounts carry from the law year of their tables to each simulated tax year.

A composition picks one variant; there is no default. Under `CpiIndexedLaw`, a tax year's CPI is its
January observation, and the index is CPI(tax year) / CPI(law year). Deliberate simplifications
against statute: the world's one headline CPI stands in for both the federal chained CPI-U
(IRC 1(f)(3)) and California's CPI (R&TC 17041(h)); a tax year's amounts follow that year's own
January CPI, not the prior-period averages statute lags them by; and an indexed amount is rounded
to the currency quantum, not down to the IRS's $50 or FTB's $1 steps.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from finance.augur.sim.compiler.tax import PreparedTaxBracket, PreparedTaxRules, PreparedThresholdTax
from finance.augur.sim.jurisdictions import StatutoryAmount
from finance.augur.sim.money import scaled


@dataclass(frozen=True)
class FixedNominalLaw:
    """Every tax year applies the law year's amounts in nominal dollars."""


@dataclass(frozen=True, kw_only=True)
class CpiIndexedLaw:
    """Indexed amounts follow CPI from the law year; fixed amounts stay nominal.

    Month 0 is January of `start_year`. `law_year_to_start` is CPI(start_year) / CPI(law year),
    which the caller reads from its own CPI history; the world's modeled `inflation` series carries
    it on from month 0, so tax year `y` is indexed by `law_year_to_start` × CPI(12y) / CPI(0).
    """

    start_year: int
    law_year_to_start: Fraction

    def __post_init__(self) -> None:
        if self.law_year_to_start <= 0:
            raise ValueError(f"a CPI ratio must be positive, not {self.law_year_to_start}")

    def check(self, rules: Sequence[PreparedTaxRules]) -> None:
        """Reject rules this start cannot index: several law years, a later law year, or an anchor its own year contradicts."""
        law_years = {row.law_year for row in rules}
        if len(law_years) > 1:
            raise ValueError(f"one CPI anchor cannot index tables of several law years {sorted(law_years)}")
        for law_year in law_years:
            if self.start_year < law_year:
                raise ValueError(f"{self.start_year=} precedes the tables' law year {law_year}")
            if self.start_year == law_year and self.law_year_to_start != 1:
                raise ValueError(
                    f"starting in the law year {law_year}, CPI(start)/CPI(law) is 1, not {self.law_year_to_start}"
                )

    def index(self, cpi: tuple[int, int]) -> Fraction:
        """CPI(tax year) / CPI(law year), from the tax year's January CPI and month 0's."""
        current, origin = cpi
        return self.law_year_to_start * Fraction(current, origin)


type TaxIndexation = FixedNominalLaw | CpiIndexedLaw


def rules_for_year(rules: PreparedTaxRules, index: Fraction) -> PreparedTaxRules:
    """`rules` with every amount statute indexes scaled by `index`, rounded to the quantum; the rest unchanged."""

    def amount(value: int, kind: StatutoryAmount) -> int:
        return scaled(value, index, f"indexed {kind}") if kind in rules.indexed else value

    def brackets(schedule: Sequence[PreparedTaxBracket], kind: StatutoryAmount) -> tuple[PreparedTaxBracket, ...]:
        return tuple(
            replace(bracket, upper=None if bracket.upper is None else amount(bracket.upper, kind))
            for bracket in schedule
        )

    def threshold(tax: PreparedThresholdTax | None, kind: StatutoryAmount) -> PreparedThresholdTax | None:
        return None if tax is None else replace(tax, threshold=amount(tax.threshold, kind))

    return replace(
        rules,
        ordinary_brackets=brackets(rules.ordinary_brackets, StatutoryAmount.ORDINARY_INCOME_BRACKETS),
        long_term_capital_gain_brackets=brackets(rules.long_term_capital_gain_brackets, StatutoryAmount.LTCG_BRACKETS),
        standard_deduction=amount(rules.standard_deduction, StatutoryAmount.STANDARD_DEDUCTION),
        max_capital_loss_ordinary_offset=amount(
            rules.max_capital_loss_ordinary_offset, StatutoryAmount.MAX_CAPITAL_LOSS_ORDINARY_OFFSET
        ),
        net_investment_income_tax=threshold(rules.net_investment_income_tax, StatutoryAmount.NET_INVESTMENT_INCOME_TAX),
        taxable_income_surtax=threshold(rules.taxable_income_surtax, StatutoryAmount.TAXABLE_INCOME_SURTAX),
    )
