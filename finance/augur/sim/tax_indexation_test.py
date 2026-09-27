"""Statutory amounts carried from the tables' law year to a CPI-indexed tax year, over the shipped 2024 tables."""

from decimal import Decimal
from fractions import Fraction

import pytest
import pytest_bazel

from finance.augur.sim.fixed_point import currency_amount_to_quanta
from finance.augur.sim.ids import AgentId, JurisdictionId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath, MarketStatement
from finance.augur.sim.money import USD
from finance.augur.sim.tax import PreparedTaxProfile, TaxFacts, assess
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.tax_indexation import CpiIndexedLaw, rules_for_year
from finance.augur.sim.tax_profile import TaxProfile, compile_profile
from finance.augur.sim.tax_year import TaxBook
from finance.augur.sim.world import World

QUANTUM = Decimal("0.01")
FILER = AgentId("test_filer")
FEDERAL, CALIFORNIA = JurisdictionId("federal_us"), JurisdictionId("california")


def dollars(amount: Fraction | int) -> int:
    return int(currency_amount_to_quanta(Decimal(amount.numerator) / amount.denominator, quantum=QUANTUM))


@pytest.fixture
def profile() -> PreparedTaxProfile:
    return compile_profile(
        TaxProfile(agent_id=FILER, jurisdiction_ids=[FEDERAL, CALIFORNIA], tax_authority_agent_id=AgentId("test_irs")),
        {id_: load_jurisdiction(id_) for id_ in (FEDERAL, CALIFORNIA)},
        currency=USD,
    )


def year(*, wages: int, short_term: int = 0, long_term: int = 0, dividends: int = 0, k: Fraction) -> TaxFacts:
    """A year's facts in dollars, every amount scaled by `k`."""
    return TaxFacts(
        taxable_ordinary_income=dollars(wages * k),
        short_term_gain=dollars(short_term * k),
        long_term_gain=dollars(long_term * k),
        qualified_dividends=dollars(dividends * k),
        investment_income=dollars(dividends * k),
    )


def test_a_hand_worked_year_at_index_one_and_a_half(profile: PreparedTaxProfile) -> None:
    """Starting two years after the law year with CPI 5/4 of the law year's, and year 1's January
    CPI 6/5 of month 0's: index 3/2. $150,000 of wages and a $30,000 long-term gain.

    Federal: deduction 21,900, ordinary taxable 128,100:
      10% × 17400 + 12% × 53325 + 22% × 57375 = 20761.50
    The gain stacks on it inside the 15% band (70,537.50 to 778,350): 4500.00. MAGI 180,000 is
    under the fixed $200,000 NIIT threshold. Total 25261.50.
    California: taxable 180,000 - 8,044.50 = 171,955.50:
      1% × 15618 + 2% × 21408 + 4% × 21412.50 + 6% × 22683 + 8% × 21403.50 + 9.3% × 69430.50
      = 156.18 + 428.16 + 856.50 + 1360.98 + 1712.28 + 6457.0365 = 10971.1365, rounded once: 10971.14
    """
    authority = TaxAuthority(profile, indexation=CpiIndexedLaw(start_year=2026, law_year_to_start=Fraction(5, 4)))
    authority.handle(MarketStatement(month=12, cpi=(360, 300)))
    book = TaxBook((ORDINARY_INCOME,))
    book.enroll(FILER)
    book.income.accrue(FILER, ORDINARY_INCOME, dollars(150_000))
    book.gain(FILER, dollars(30_000), long_term=True)
    federal, california = authority.assessments(book, 23, [], {})
    assert (federal.ordinary_tax, federal.capital_gain_tax, federal.net_investment_income_tax) == (
        dollars(Fraction(2_076_150, 100)),
        dollars(4_500),
        0,
    )
    assert federal.total_tax == dollars(Fraction(2_526_150, 100))
    assert california.total_tax == dollars(Fraction(1_097_114, 100))


@pytest.mark.parametrize("k", [Fraction(2), Fraction(3), Fraction(3, 2)])
@pytest.mark.parametrize(
    "facts",
    [
        # Short- and long-term gains, MAGI at most $200,000 once scaled: NIIT stays zero.
        {"wages": 40_000, "short_term": 10_000, "long_term": 5_000},
        # Gains and dividends straddling the 0%/15% breakpoint.
        {"wages": 20_000, "long_term": 30_000, "dividends": 5_000},
        # Wages alone are not net investment income, so no threshold binds up the brackets.
        {"wages": 250_000},
    ],
)
def test_indexed_only_liabilities_scale_with_income_and_index(
    profile: PreparedTaxProfile, facts: dict[str, int], k: Fraction
) -> None:
    for rules in profile.jurisdictions:
        base = assess(year(**facts, k=Fraction(1)), rules).total_tax
        indexed = assess(year(**facts, k=k), rules_for_year(rules, k)).total_tax
        # Each tax rounds once, so the scaled tax is within a quantum of k times the base.
        assert abs(indexed - k * base) <= 1


def test_the_fixed_niit_threshold_does_not_scale(profile: PreparedTaxProfile) -> None:
    """$190,000 of wages and a $30,000 long-term gain: MAGI 220,000 is 20,000 over the fixed
    threshold, so NIIT is 3.8% × 20,000 = 760.00. Doubled with the index, MAGI 440,000 is 240,000
    over and the 60,000 gain binds: 2,280.00, three times the base rather than two."""
    federal, _ = profile.jurisdictions
    base = assess(year(wages=190_000, long_term=30_000, k=Fraction(1)), federal)
    doubled = assess(year(wages=190_000, long_term=30_000, k=Fraction(2)), rules_for_year(federal, Fraction(2)))
    assert (base.net_investment_income_tax, doubled.net_investment_income_tax) == (dollars(760), dollars(2_280))
    assert (
        abs(
            (doubled.total_tax - doubled.net_investment_income_tax)
            - 2 * (base.total_tax - base.net_investment_income_tax)
        )
        <= 1
    )


def test_cpi_indexing_without_a_modeled_cpi_is_refused(profile: PreparedTaxProfile) -> None:
    world = World(MarketPath((), 0, rollout_count=1), horizon_months=12, income_sources=(ORDINARY_INCOME,))
    authority = TaxAuthority(profile, indexation=CpiIndexedLaw(start_year=2024, law_year_to_start=Fraction(1)))
    with pytest.raises(ValueError, match="needs a modeled inflation path"):
        world.track(authority)
    with pytest.raises(ValueError, match="needs its January market statement"):
        authority.rules(0)


def test_a_start_before_the_law_year_deflates_the_indexed_amounts(profile: PreparedTaxProfile) -> None:
    """A 2019 start at CPI 4/5 of 2024's: standard deductions 14,600 × 4/5 = 11,680 and
    5,363 × 4/5 = 4,290.40; the fixed $200,000 NIIT threshold stays put."""
    authority = TaxAuthority(profile, indexation=CpiIndexedLaw(start_year=2019, law_year_to_start=Fraction(4, 5)))
    authority.handle(MarketStatement(month=0, cpi=(10**9, 10**9)))
    federal, california = authority.rules(0)
    assert (federal.standard_deduction, california.standard_deduction) == (
        dollars(11_680),
        dollars(Fraction(429_040, 100)),
    )
    assert federal.net_investment_income_tax is not None
    assert federal.net_investment_income_tax.threshold == dollars(200_000)


@pytest.mark.parametrize(
    ("start_year", "law_year_to_start", "match"),
    [(2024, Fraction(11, 10), "is 1, not 11/10"), (2026, Fraction(0), "must be positive")],
)
def test_an_anchor_inconsistent_with_the_law_year_is_refused(
    profile: PreparedTaxProfile, start_year: int, law_year_to_start: Fraction, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        TaxAuthority(profile, indexation=CpiIndexedLaw(start_year=start_year, law_year_to_start=law_year_to_start))


if __name__ == "__main__":
    pytest_bazel.main()
