"""Proposition 13 assessment and the secured bill, pinned to published figures and hand arithmetic.

Month 0 is January of the case's start year; fiscal year `y` runs from July of `y` (month
`12 * (y - start) + 6`) through the June after.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

import pytest
import pytest_bazel

from finance.augur.model.series import LocationId
from finance.augur.sim.actor import MonthOpened
from finance.augur.sim.books import AccountRef
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, PropertyId
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath, MarketStatement
from finance.augur.sim.money import USD
from finance.augur.sim.property import Housing, Parcel, PropertyStatement, ScheduledPurchase
from finance.augur.sim.property_tax import PropertyTaxAuthority, PropertyTaxPolicy
from finance.augur.sim.situs import SitusLaw, compile_situs
from finance.augur.sim.world import World

OWNER, COUNTY = AgentId("test-owner"), AgentId("test-county")
CHECKING = AccountId("checking")
HOME = PropertyId("test-home")
SAN_FRANCISCO = compile_situs(load_jurisdiction(JurisdictionId("san_francisco")), currency=USD)


def dollars(amount: Decimal | int | str) -> int:
    return USD.quanta(Decimal(amount))


def cents(amount: Decimal) -> int:
    """An exact amount rounded to the cent, ties away from zero."""
    return dollars(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def sf_bill(amount: Decimal) -> int:
    """San Francisco rounds each half of a secured bill down to the cent."""
    return 2 * dollars((amount / 2).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


# The combined secured rate in San Francisco, by fiscal year.
SF_2024 = Decimal("0.01") + Decimal("0.0017143563")
SF_2025 = Decimal("0.01") + Decimal("0.0018268325")


def purchase(situs: SitusLaw, *, month: int, price: int, prior: int | None = None) -> ScheduledPurchase:
    return ScheduledPurchase(
        month=month,
        cause_id="test-purchase",
        property_id=HOME,
        parcel=Parcel(situs=situs, prior_assessed_value=prior),
        market=LocationId("test-market"),
        buyer_agent_id=OWNER,
        buyer_account_id=CHECKING,
        seller_agent_id=COUNTY,
        seller_account_id=CHECKING,
        purchase_price=price,
        down_payment=price,
        buyer_closing_cost=0,
        rented_fraction_ppb=0,
        land_value_fraction_ppb=0,
        mortgage=None,
    )


def policy(start_year: int) -> PropertyTaxPolicy:
    return PropertyTaxPolicy(
        property_id=HOME,
        owner_agent_id=OWNER,
        from_account_id=CHECKING,
        tax_authority_agent_id=COUNTY,
        tax_authority_account_id=CHECKING,
        start_year=start_year,
        start_month=0,
        end_month=None,
    )


@dataclass(frozen=True)
class Holding:
    """A parcel held from `purchase_month` through `horizon`, as its statements tell the authority."""

    start_year: int
    purchase_month: int
    price: int
    horizon: int
    # The months the owner lives there; none by default.
    occupied: frozenset[int] = frozenset()
    # Construction completed, by month.
    construction: Mapping[int, int] = field(default_factory=dict)
    # The modeled CPI by month, when the case models one.
    cpi: Sequence[int] | None = None
    # The seller's assessed value, where the case names it.
    prior: int | None = None


@dataclass(frozen=True)
class Assessed:
    # The regular and the supplemental bills, by month.
    bills: dict[int, int]
    supplemental: dict[int, int]
    # The assessed value at the close of each month.
    values: dict[int, int]

    def fiscal_year(self, start_year: int, year: int) -> int:
        first = 12 * (year - start_year) + 6
        return sum(amount for month, amount in self.bills.items() if first <= month < first + 12)


def assess(case: Holding) -> Assessed:
    authority = PropertyTaxAuthority(
        policy(case.start_year), purchase(SAN_FRANCISCO, month=case.purchase_month, price=case.price, prior=case.prior)
    )
    bills: dict[int, int] = {}
    supplemental: dict[int, int] = {}
    values: dict[int, int] = {}
    for month in range(case.horizon):
        if month >= case.purchase_month:
            authority.handle(
                PropertyStatement(
                    month=month,
                    property_id=HOME,
                    active=True,
                    purchase_month=case.purchase_month,
                    rented_fraction_ppb=0,
                    owner_occupied=month in case.occupied,
                    new_construction=case.construction.get(month, 0),
                    transfer_tax=0,
                )
            )
        authority.handle(MarketStatement(month=month, cpi=None if case.cpi is None else (case.cpi[month], case.cpi[0])))
        for bill in authority.handle(MonthOpened(month=month)):
            (supplemental if "_supplemental_tax_" in bill.cause_id else bills)[month] = bill.amount
        if authority.assessed_value is not None:
            values[month] = authority.assessed_value
    return Assessed(bills, supplemental, values)


def test_san_francisco_s_published_comparative_bill() -> None:
    """The Controller's worked example (Budget and Finance Committee, September 18, 2024): a
    home assessed at $717,300 for FY 2023-24 pays (717,300 - 7,000) × 1.17769382% = $8,365.14;
    the 2024 lien date adds the 2% inflation factor, $14,346, and FY 2024-25 pays
    (731,646 - 7,000) × 1.17143563% = $8,488.76. Bought on the 2023 lien date, so no factor
    applies to it, and lived in throughout."""
    assessed = assess(
        Holding(start_year=2023, purchase_month=0, price=dollars(717_300), horizon=30, occupied=frozenset(range(30)))
    )

    assert assessed.values[12] == dollars(731_646)
    assert assessed.fiscal_year(2023, 2023) == dollars("8365.14")
    assert assessed.fiscal_year(2023, 2024) == dollars("8488.76")


@pytest.mark.parametrize(
    ("cpi", "factor"),
    [
        # 3% CPI: capped at 2%.
        ((100_000, 103_000), Decimal("1.02")),
        # 1% CPI: grows by it.
        ((100_000, 101_000), Decimal("1.01")),
        # -0.49986%, rounded to the nearest 0.001%: a factor below 1.
        ((104_030, 103_510), Decimal("0.995")),
    ],
    ids=["above-cap", "below-cap", "negative"],
)
def test_a_simulated_lien_date_grows_by_modeled_cpi_capped(cpi: tuple[int, int], factor: Decimal) -> None:
    """2030 has no published factor, so the change in modeled CPI from January 2029 sets it."""
    path = [cpi[0]] * 12 + [cpi[1]] * 12
    assessed = assess(Holding(start_year=2029, purchase_month=0, price=dollars(1_000_000), horizon=24, cpi=path))

    assert assessed.values[11] == dollars(1_000_000)
    assert assessed.values[12] == dollars(1_000_000 * factor)


def test_a_published_lien_year_takes_the_board_s_factor_over_modeled_cpi() -> None:
    """2021-22's factor is 1.01036 (BOE Letter To Assessors 2026/002), whatever CPI the world models."""
    path = [100_000] * 12 + [110_000] * 12
    assessed = assess(Holding(start_year=2020, purchase_month=6, price=dollars(500_000), horizon=24, cpi=path))

    assert assessed.values[12] == dollars(505_180)


def test_construction_adds_its_cost_when_completed_and_is_factored_from_the_next_lien_date() -> None:
    """$100,000 of construction completed in March 2024 is not on the roll for FY 2024-25, set on
    the January 2024 lien date. On the 2025 lien date the whole value takes 2025's 1.02 factor.
    Construction completed in January 2025 falls after that lien date, so it is added unfactored."""
    assessed = assess(
        Holding(
            start_year=2024,
            purchase_month=0,
            price=dollars(800_000),
            horizon=30,
            construction={2: dollars(100_000), 12: dollars(50_000)},
        )
    )

    assert assessed.values[2] == dollars(900_000)
    assert assessed.fiscal_year(2024, 2024) == sf_bill(800_000 * SF_2024)
    assert assessed.values[12] == dollars(900_000 * Decimal("1.02") + 50_000)
    assert assessed.fiscal_year(2024, 2025) == sf_bill(918_000 * SF_2025)


def test_the_homeowners_exemption_needs_the_owner_living_there_on_the_lien_date() -> None:
    """Moving in on February 1 misses the lien date: FY 2024-25 pays on the whole value. Living
    there on the next lien date takes $7,000 off FY 2025-26's taxable value."""
    assessed = assess(
        Holding(start_year=2024, purchase_month=0, price=dollars(900_000), horizon=30, occupied=frozenset(range(1, 30)))
    )

    assert assessed.fiscal_year(2024, 2024) == sf_bill(900_000 * SF_2024)
    assert assessed.fiscal_year(2024, 2025) == sf_bill((918_000 - 7_000) * SF_2025)


def test_a_fiscal_year_whose_lien_date_preceded_the_purchase_is_billed_on_the_price() -> None:
    """Bought in September 2024 for $1,200,000: October through June pay nine twelfths of FY
    2024-25's tax on the price, with no exemption, though the owner lives there."""
    assessed = assess(
        Holding(
            start_year=2024, purchase_month=8, price=dollars(1_200_000), horizon=18, occupied=frozenset(range(8, 18))
        )
    )
    annual = sf_bill(1_200_000 * SF_2024)

    assert min(assessed.bills) == 9
    # The three twelfths July through September are not billed.
    assert assessed.fiscal_year(2024, 2024) == annual - cents(Decimal(annual) / 100 * 3 / 12)


def test_a_mid_year_purchase_pays_the_seller_s_roll_and_a_supplemental_on_the_increase() -> None:
    """Bought in September 2024 for $1,200,000 from a seller assessed at $500,000. October through June
    pay nine twelfths of FY 2024-25's bill on $500,000: 500,000 × 1.17143563% = $5,857.178, billed as
    $5,857.16, less the $1,464.29 of July through September, $4,392.87. The supplemental taxes the
    $700,000 increase for a full year, 700,000 × 1.17143563% = $8,200.049, billed as $8,200.04, times
    R&TC 75.41's 0.75 for an October 1 presumed date: $6,150.03. The assessed value resets to the price."""
    assessed = assess(
        Holding(start_year=2024, purchase_month=8, price=dollars(1_200_000), horizon=18, prior=dollars(500_000))
    )

    assert assessed.values[8] == dollars(1_200_000)
    assert assessed.fiscal_year(2024, 2024) == dollars("4392.87")
    assert assessed.supplemental == {9: dollars("6150.03")}


def test_a_spring_purchase_is_enrolled_at_its_price_for_the_next_fiscal_year() -> None:
    """Bought in March 2025 for $800,000 from a seller assessed at $300,000. April through June pay
    the last three twelfths of FY 2024-25's bill on $300,000 (300,000 × 1.17143563% = $3,514.307,
    billed as $3,514.30): $3,514.30 - $2,635.73 = $878.57. The supplemental is the $5,857.16 billed on
    the $500,000 increase, times 0.25 for April 1: $1,464.29. FY 2025-26, whose lien date also
    preceded the purchase, is billed on the price: 800,000 × 1.18268325% = $9,461.466, billed as
    $9,461.46."""
    assessed = assess(
        Holding(start_year=2025, purchase_month=2, price=dollars(800_000), horizon=18, prior=dollars(300_000))
    )

    assert sum(assessed.bills[month] for month in range(3, 6)) == dollars("878.57")
    assert assessed.supplemental == {3: dollars("1464.29")}
    assert assessed.fiscal_year(2025, 2025) == dollars("9461.46")


def test_a_june_purchase_has_no_supplemental_on_the_closing_roll() -> None:
    """R&TC 75.41(c)(6): a July 1 presumed date makes no supplemental on the current roll."""
    assessed = assess(
        Holding(start_year=2024, purchase_month=5, price=dollars(900_000), horizon=8, prior=dollars(400_000))
    )

    assert assessed.supplemental == {}


def test_a_purchase_below_the_prior_assessed_value_is_refused() -> None:
    """Its supplemental would be a refund, which is not modeled."""
    world = World(MarketPath((), 0, rollout_count=1), horizon_months=6)
    for agent in (OWNER, COUNTY):
        world.declare_account(
            account=AccountRef(agent_id=agent, account_id=CHECKING), opening_balance=dollars(2_000_000)
        )
    with pytest.raises(ValueError, match="below its prior assessed value"):
        world.declare_housing(
            Housing(purchases=(purchase(SAN_FRANCISCO, month=0, price=dollars(1_000_000), prior=dollars(1_000_001)),)),
            (policy(2024),),
        )


def test_a_simulated_lien_date_needs_a_modeled_cpi_path() -> None:
    """Held into 2027, whose lien date no factor is published for, on a path that models no inflation."""
    world = World(MarketPath((), 0, rollout_count=1), horizon_months=40)
    for agent in (OWNER, COUNTY):
        world.declare_account(
            account=AccountRef(agent_id=agent, account_id=CHECKING), opening_balance=dollars(2_000_000)
        )
    with pytest.raises(ValueError, match="needs a modeled inflation path"):
        world.declare_housing(
            Housing(purchases=(purchase(SAN_FRANCISCO, month=0, price=dollars(1_000_000)),)), (policy(2024),)
        )


if __name__ == "__main__":
    pytest_bazel.main()
