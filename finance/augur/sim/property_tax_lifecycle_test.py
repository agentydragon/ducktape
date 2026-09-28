"""Ten years of one home per location, against a hand calculation.

A single filer earning $25,000 a month buys a home in January 2024 (month 0) with 20% down, lives
in it for three years, lets all of it for four at $6,000 a month from January 2027 (month 36), and
sells it in January 2031 (month 84) at 1.2 times the price, paying 5% commission and 1% escrow and
title and the whole transfer tax. Month 0 is January 2024; the modeled CPI is flat, so each
simulated lien date (2027 on) grows nothing, while 2025 and 2026 take the BOE's published 1.02.
The land is 20% of the price, chosen so the building depreciates in whole dollars.

**Assessed value and bills** (fiscal year FY y runs July y through June y+1):

- FY 2023: the purchase fiscal year, billed on the price without the exemption.
- FY 2024-2026: the value enrolled on that January's lien date, while the owner lives there,
  less the $7,000 exemption.
- FY 2027 on: let on the lien date, so no exemption.
- The value is the price through the 2024 lien date, ×1.02 on 2025 and on 2026, and flat after.
- A fiscal year's bill is its rate on the taxable value, rounded as the rate area rounds it. FY
  2026 on carries FY 2025's published rate forward.
- Month j of a fiscal year (0 = July) pays the cumulative j+1 twelfths less the j twelfths, each
  rounded to the cent. So calendar year y pays FY y-1's last six twelfths, B(y-1) - T6(B(y-1)),
  plus FY y's first six, T6(B(y)), where T6(b) = b/2 rounded. Year 0 instead pays FY 2023's
  February-June share, B - T7(B), where T7(b) = 7b/12 rounded.

**Deductions.** The owner's share of a year's bills is federal SALT (no cap is declared, so SALT
is that share plus the year's California income tax) and California's own itemized property tax.
From month 36 the whole bill is the let share, a rental expense. Depreciation is (price × 0.8) /
330 a month for the 48 let months.

**Sale.** Amount realized = 1.2 × price, less 6%, less the seller's transfer tax. The gain is the
amount realized less (price - depreciation). Recapture is the lesser of the gain and the
depreciation. The §121 exclusion is nothing: the owner lived there 12 of the 60 months before
the sale, not the 24 it needs.
"""

from dataclasses import dataclass
from decimal import Decimal

import polars as pl
import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.model.series import LocationId
from finance.augur.policy.funding import ClaimPayer
from finance.augur.sim.actions import DecisionActions
from finance.augur.sim.books import AccountRef, TaxAccrual
from finance.augur.sim.fixed_point import rate_to_ppb
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, LiabilityId, PropertyId
from finance.augur.sim.income import ORDINARY_INCOME
from finance.augur.sim.jurisdictions import load_jurisdiction
from finance.augur.sim.market_path import MarketPath, Series
from finance.augur.sim.money import USD
from finance.augur.sim.property import (
    Housing,
    MortgageFinancing,
    Parcel,
    PrimaryResidence,
    PrimaryResidenceEvent,
    RentedFraction,
    ScheduledPurchase,
    ScheduledSale,
)
from finance.augur.sim.property_tax import PropertyTaxPolicy
from finance.augur.sim.results import Finished, Rollout
from finance.augur.sim.schedule import Recurring
from finance.augur.sim.session import ActionSession
from finance.augur.sim.situs import compile_situs
from finance.augur.sim.tax_authority import SaltDeduction, TaxAuthority
from finance.augur.sim.tax_indexation import FixedNominalLaw
from finance.augur.sim.tax_profile import TaxProfile, compile_profile
from finance.augur.sim.world import World

OWNER, EMPLOYER, TENANT, SELLER, BANK, COUNTY, IRS = (
    AgentId("test-owner"),
    AgentId("test-employer"),
    AgentId("test-tenant"),
    AgentId("test-seller"),
    AgentId("test-bank"),
    AgentId("test-county"),
    AgentId("test-irs"),
)
CHECKING = AccountId("checking")
HOME = PropertyId("test-home")
FEDERAL, CALIFORNIA = JurisdictionId("federal_us"), JurisdictionId("california")
HORIZON, LET, SOLD = 120, 36, 84
WAGES, RENT = Decimal(25_000), Decimal(6_000)


@dataclass(frozen=True)
class Expected:
    """One location's hand calculation, in dollars."""

    situs: JurisdictionId
    price: Decimal
    # Property tax paid in calendar years 0 through 6; year 7 has none, the sale being in January.
    paid_by_year: tuple[Decimal, ...]
    depreciation_per_let_year: Decimal
    transfer_tax: Decimal
    gain: Decimal
    recapture: Decimal


SAN_FRANCISCO = Expected(
    situs=JurisdictionId("san_francisco"),
    price=Decimal(1_237_500),
    # Bills (rounded down to an even cent in San Francisco):
    #   FY 2023: 1,237,500 × 1.17769382% = 14,573.96
    #   FY 2024: (1,237,500 - 7,000) × 1.17143563% = 14,414.50
    #   FY 2025: (1,262,250 - 7,000) × 1.18268325% = 14,845.62
    #   FY 2026: (1,287,495 - 7,000) × 1.18268325% = 15,144.18
    #   FY 2027-2030: 1,287,495 × 1.18268325% = 15,226.98
    paid_by_year=(
        Decimal("13279.73"),  # (14,573.96 - 8,501.48) + 7,207.25
        Decimal("14630.06"),  # (14,414.50 - 7,207.25) + 7,422.81
        Decimal("14994.90"),  # (14,845.62 - 7,422.81) + 7,572.09
        Decimal("15185.58"),  # (15,144.18 - 7,572.09) + 7,613.49
        Decimal("15226.98"),  # (15,226.98 - 7,613.49) + 7,613.49, and alike for years 5 and 6
        Decimal("15226.98"),
        Decimal("15226.98"),
    ),
    depreciation_per_let_year=Decimal(36_000),  # 12 × 990,000 / 330
    # 1,485,000 is in "$1,000,000 or more but less than $5,000,000": 3.75 × 2,970 units of $500.
    transfer_tax=Decimal("11137.50"),
    # 1,485,000 - 74,250 - 14,850 - 11,137.50 - (1,237,500 - 144,000)
    gain=Decimal("291262.50"),
    recapture=Decimal(144_000),
)
MAINLAND_VALLEJO = Expected(
    situs=JurisdictionId("solano_tra_007000"),
    price=Decimal(618_750),
    # Bills (to the nearest cent in Solano County):
    #   FY 2023: 618,750 × 1.113727% = 6,891.19
    #   FY 2024: (618,750 - 7,000) × 1.133439% = 6,933.81
    #   FY 2025: (631,125 - 7,000) × 1.119373% = 6,986.29
    #   FY 2026: (643,747.50 - 7,000) × 1.119373% = 7,127.58
    #   FY 2027-2030: 643,747.50 × 1.119373% = 7,205.94
    paid_by_year=(
        Decimal("6338.24"),  # (6,891.19 - 4,019.86) + 3,466.91
        Decimal("6960.05"),  # (6,933.81 - 3,466.91) + 3,493.15
        Decimal("7056.93"),  # (6,986.29 - 3,493.15) + 3,563.79
        Decimal("7166.76"),  # (7,127.58 - 3,563.79) + 3,602.97
        Decimal("7205.94"),
        Decimal("7205.94"),
        Decimal("7205.94"),
    ),
    depreciation_per_let_year=Decimal(18_000),  # 12 × 495,000 / 330
    # Solano County's $0.55 and Vallejo's $1.65 on each of 1,485 units of $500 of 742,500.
    transfer_tax=Decimal("3267.00"),
    # 742,500 - 37,125 - 7,425 - 3,267 - (618,750 - 72,000)
    gain=Decimal(147_933),
    recapture=Decimal(72_000),
)


def dollars(amount: Decimal | int) -> int:
    return USD.quanta(Decimal(amount))


def ref(agent: AgentId) -> AccountRef:
    return AccountRef(agent_id=agent, account_id=CHECKING)


def compose(case: Expected) -> World:
    price = dollars(case.price)
    world = World(
        MarketPath(
            (
                Series(series_id="inflation", snapshots=HORIZON + 1, values=(100_000,) * (HORIZON + 1)),
                Series(
                    series_id="home_value:test-market",
                    snapshots=HORIZON + 1,
                    values=(100,) * SOLD + (120,) * (HORIZON + 1 - SOLD),
                ),
            ),
            0,
            rollout_count=1,
        ),
        horizon_months=HORIZON,
        income_sources=(ORDINARY_INCOME,),
    )
    for agent, balance in ((OWNER, 400_000), (EMPLOYER, 4_000_000), (TENANT, 400_000)):
        world.declare_account(account=ref(agent), opening_balance=dollars(balance))
    for agent in (SELLER, BANK, COUNTY, IRS):
        world.declare_account(account=ref(agent), opening_balance=0)
    world.track(
        TaxAuthority(
            compile_profile(
                TaxProfile(agent_id=OWNER, jurisdiction_ids=[FEDERAL, CALIFORNIA], tax_authority_agent_id=IRS),
                {id_: load_jurisdiction(id_) for id_ in (FEDERAL, CALIFORNIA)},
                currency=USD,
            ),
            indexation=FixedNominalLaw(),
        )
    )
    world.declare_deduction(SaltDeduction(profile_id=OWNER, federal_jurisdiction_id=FEDERAL, cap_schedule=()))
    world.declare_housing(
        Housing(
            purchases=(
                ScheduledPurchase(
                    month=0,
                    cause_id="test-purchase",
                    property_id=HOME,
                    parcel=Parcel(
                        situs=compile_situs(load_jurisdiction(case.situs), currency=USD), prior_assessed_value=None
                    ),
                    market=LocationId("test-market"),
                    buyer_agent_id=OWNER,
                    buyer_account_id=CHECKING,
                    seller_agent_id=SELLER,
                    seller_account_id=CHECKING,
                    purchase_price=price,
                    down_payment=price // 5,
                    buyer_closing_cost=0,
                    rented_fraction_ppb=0,
                    land_value_fraction_ppb=rate_to_ppb(Decimal("0.2")),
                    mortgage=MortgageFinancing(
                        liability_id=LiabilityId("test-mortgage"),
                        lender_agent_id=BANK,
                        lender_account_id=CHECKING,
                        principal=price - price // 5,
                        annual_interest_rate_ppb=rate_to_ppb(Decimal("0.06")),
                        term_months=360,
                    ),
                ),
            ),
            sales=(
                ScheduledSale(
                    month=SOLD,
                    property_id=HOME,
                    commission_ppb=rate_to_ppb(Decimal("0.05")),
                    escrow_title_ppb=rate_to_ppb(Decimal("0.01")),
                ),
            ),
            initial_residences=(PrimaryResidence(agent_id=OWNER, property_id=HOME),),
            residence_events=(PrimaryResidenceEvent(month=LET, agent_id=OWNER, property_id=None),),
            rented_fraction_events=(RentedFraction(month=LET, property_id=HOME, rented_fraction_ppb=rate_to_ppb(1)),),
        ),
        (
            PropertyTaxPolicy(
                property_id=HOME,
                owner_agent_id=OWNER,
                from_account_id=CHECKING,
                tax_authority_agent_id=COUNTY,
                tax_authority_account_id=CHECKING,
                start_year=2024,
                start_month=0,
                end_month=None,
            ),
        ),
    )
    world.declare_flow(
        schedule=Recurring(start_month=0, end_month=HORIZON - 1),
        cause_id="wages",
        from_account=ref(EMPLOYER),
        to_account=ref(OWNER),
        amount=dollars(WAGES),
        income_category=ORDINARY_INCOME,
        deduction_category=None,
    )
    world.declare_flow(
        schedule=Recurring(start_month=LET, end_month=SOLD - 1),
        cause_id="rent",
        from_account=ref(TENANT),
        to_account=ref(OWNER),
        amount=dollars(RENT),
        income_category=ORDINARY_INCOME,
        deduction_category=None,
        property_id=HOME,
    )
    return world


def run(case: Expected) -> Rollout:
    household = ClaimPayer(OWNER)
    session = ActionSession({0: compose(case)}, OWNER)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            batch = session.advance(
                [
                    DecisionActions(
                        decision.rollout_id, decision.observation.month, household.decide(decision.observation)
                    )
                    for decision in batch
                ]
            )
    finally:
        session.close()
    rollout = one(batch.rollouts)
    assert rollout.stop is None
    return rollout


@pytest.fixture(scope="module", params=[SAN_FRANCISCO, MAINLAND_VALLEJO], ids=["san-francisco", "mainland-vallejo"])
def lifecycle(request: pytest.FixtureRequest) -> tuple[Expected, Rollout]:
    case: Expected = request.param
    return case, run(case)


def returns(rollout: Rollout, year: int) -> dict[JurisdictionId, TaxAccrual]:
    return {
        row.jurisdiction_id: row for row in rollout.summary.tax_accruals if row.tax_year_end_month == 12 * year + 11
    }


def test_each_year_s_bills(lifecycle: tuple[Expected, Rollout]) -> None:
    case, rollout = lifecycle
    assert rollout.trace is not None
    bills = rollout.trace.events.obligation_settlements.filter(pl.col("obligation_type") == "property_tax")
    paid = bills.group_by(pl.col("month_index") // 12).agg(pl.col("amount_paid_quanta").sum()).sort("month_index")

    assert paid.rows() == [(year, dollars(amount)) for year, amount in enumerate(case.paid_by_year)]


def test_each_year_s_deductions(lifecycle: tuple[Expected, Rollout]) -> None:
    """Lived-in years: the bills are the owner's, in SALT beside California's income tax and itemized
    in California; nothing is a rental expense. Let years: the bills are a rental expense, SALT is
    California's income tax alone, and the building depreciates."""
    case, rollout = lifecycle
    for year, paid in enumerate(case.paid_by_year):
        federal, california = returns(rollout, year)[FEDERAL], returns(rollout, year)[CALIFORNIA]
        let = year >= LET // 12
        owner, rental = (Decimal(0), paid) if let else (paid, Decimal(0))
        rent = 12 * RENT if let else Decimal(0)
        depreciation = case.depreciation_per_let_year if let else Decimal(0)

        assert federal.salt_deduction - california.total_tax == dollars(owner), year
        assert california.itemized_deduction - california.mortgage_interest_deduction == dollars(owner), year
        assert federal.depreciation_deduction == dollars(depreciation), year
        # Wages and rent less what the return deducted from them: the rental interest it reports,
        # the depreciation, and the let share of the property tax.
        assert dollars(
            12 * WAGES + rent
        ) - federal.rental_interest_deduction - federal.depreciation_deduction - federal.ordinary_income == dollars(
            rental
        ), year


def test_the_sale(lifecycle: tuple[Expected, Rollout]) -> None:
    case, rollout = lifecycle
    assert rollout.trace is not None
    sale = one(rollout.trace.events.property_sale_events.to_dicts())
    transfer = one(
        rollout.trace.events.obligation_settlements.filter(pl.col("obligation_type") == "transfer_tax").to_dicts()
    )

    assert (transfer["month_index"], transfer["amount_paid_quanta"]) == (SOLD, dollars(case.transfer_tax))
    assert sale["transfer_tax_quanta"] == dollars(case.transfer_tax)
    assert sale["realized_gain_quanta"] == dollars(case.gain)
    assert sale["section_121_exclusion_quanta"] == 0
    assert sale["depreciation_recapture_quanta"] == dollars(case.recapture)
    assert sale["long_term_capital_gain_quanta"] == dollars(case.gain - case.recapture)


if __name__ == "__main__":
    pytest_bazel.main()
