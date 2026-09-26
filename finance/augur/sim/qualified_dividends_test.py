"""Qualified dividends at the year close, against liabilities worked by hand from the bundled 2024 tables.

Every expected figure is a single filer's tax computed on paper from `data/jurisdictions/`:
federal rates qualified dividends on the long-term capital-gain brackets, stacked with net
long-term gain above ordinary income after the deduction; California has no such brackets
and taxes them as ordinary income. Federal figures are regular income tax (ordinary plus
capital-gain rate tax); a surtax such as NIIT is assessed apart from them.
"""

from dataclasses import dataclass
from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.sim.books import TaxAccrual
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.fixed_point import currency_amount_to_quanta
from finance.augur.sim.ids import AgentId, JurisdictionId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.scenario import ORDINARY_INCOME, QualifiedDividendIncome, TaxProfile
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_year import TaxBook

QUANTUM = Decimal("0.01")
FILER = AgentId("test_filer")
FEDERAL, CALIFORNIA = JurisdictionId("federal_us"), JurisdictionId("california")
YEAR_END = 11


@dataclass(frozen=True)
class Year:
    """One tax year's facts in whole dollars; `short_term` and `long_term` are net realized gains."""

    wages: int = 0
    dividends: int = 0
    short_term: int = 0
    long_term: int = 0


def _quanta(dollars: int) -> int:
    return int(currency_amount_to_quanta(Decimal(dollars), quantum=QUANTUM))


def close(year: Year) -> dict[JurisdictionId, TaxAccrual]:
    """The year-end rows a federal + California single filer's authority posts for `year`."""
    profile = compile_profile(
        TaxProfile(agent_id=FILER, jurisdiction_ids=[FEDERAL, CALIFORNIA], tax_authority_agent_id=AgentId("test_irs")),
        {id_: load_jurisdiction(id_) for id_ in (FEDERAL, CALIFORNIA)},
        quantum=QUANTUM,
    )
    book = TaxBook((ORDINARY_INCOME, QualifiedDividendIncome()))
    book.enroll(FILER)
    book.income.accrue(FILER, ORDINARY_INCOME, _quanta(year.wages))
    book.income.accrue(FILER, QualifiedDividendIncome(), _quanta(year.dividends))
    book.gain(FILER, _quanta(year.short_term), long_term=False)
    book.gain(FILER, _quanta(year.long_term), long_term=True)
    return {row.jurisdiction_id: row for row in TaxAuthority(profile).assessments(book, YEAR_END, [], ())}


@pytest.mark.parametrize(
    ("year", "federal", "california"),
    [
        # Federal: taxable 24,000 - 14,600 = 9,400, all of it dividends inside the 0% band.
        # California: 24,000 - 5,363 = 18,637 at 1% to 10,412 and 2% above: 104.12 + 164.50.
        pytest.param(Year(dividends=24_000), (0, 0), 26_862, id="dividends_alone"),
        # Federal: ordinary 40,000 - 14,600 = 25,400 -> 1,160 + 13,800 x 12% = 2,816.
        # Dividends fill 25,400..55,400: 0% to 47,025, then 8,375 x 15% = 1,256.25.
        # California: 64,637 ordinary -> 104.12 + 285.44 + 571.00 + 907.32 + 10,556 x 8% = 2,712.36.
        pytest.param(Year(wages=40_000, dividends=30_000), (281_600, 125_625), 271_236, id="across_0_15"),
        # Federal: ordinary 485,400 -> 1,160 + 4,266 + 11,742.50 + 21,942 + 16,568 + 241,675 x 35%
        # = 140,264.75. Dividends fill 485,400..535,400: 33,500 x 15% + 16,500 x 20% = 8,325.
        # California: 544,637 -> 3,009.40 through 68,350, + 26,113.191 + 7,191.872
        # + 125,676 x 11.3% = 50,515.851.
        pytest.param(Year(wages=500_000, dividends=50_000), (14_026_475, 832_500), 5_051_585, id="across_15_20"),
        # The 25,000 short-term loss absorbs the 10,000 long-term gain; 3,000 of the remaining
        # 15,000 offsets wages and 12,000 carries forward. The dividends are not netted.
        # Federal: ordinary 60,000 - 3,000 - 14,600 = 42,400 -> 1,160 + 30,800 x 12% = 4,856.
        # Dividends fill 42,400..62,400: 0% to 47,025, then 15,375 x 15% = 2,306.25.
        # California: 57,000 + 20,000 - 5,363 = 71,637 -> 3,009.40 + 3,287 x 9.3% = 3,315.091.
        pytest.param(
            Year(wages=60_000, dividends=20_000, short_term=-25_000, long_term=10_000),
            (485_600, 230_625),
            331_509,
            id="with_gain_and_loss",
        ),
    ],
)
def test_liability(year: Year, federal: tuple[int, int], california: int) -> None:
    rows = close(year)
    assert (rows[FEDERAL].ordinary_tax, rows[FEDERAL].capital_gain_tax) == federal
    assert rows[CALIFORNIA].total_tax == california


def test_the_deduction_left_over_by_ordinary_income_shelters_dividends() -> None:
    federal = close(Year(dividends=24_000))[FEDERAL]
    assert (federal.ordinary_taxable, federal.long_term_capital_gain_taxable) == (0, 940_000)


def test_the_capital_loss_is_netted_against_gains_only_and_carries_forward() -> None:
    rows = close(Year(wages=60_000, dividends=20_000, short_term=-25_000, long_term=10_000))
    assert {row.capital_loss_carryforward for row in rows.values()} == {1_200_000}


def test_california_taxes_dividends_as_it_taxes_wages() -> None:
    assert close(Year(wages=40_000, dividends=30_000))[CALIFORNIA].total_tax == (
        close(Year(wages=70_000))[CALIFORNIA].total_tax
    )


if __name__ == "__main__":
    pytest_bazel.main()
