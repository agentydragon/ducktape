"""Rust/JAX differential coverage for the financed-property lifecycle: purchase, carrying
costs, rental transitions, sale, and mortgage interest.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from decimal import Decimal

import polars as pl
import pytest
import pytest_bazel

from finance.augur.rust.differential.backend import BACKENDS, assert_backends_agree
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    ORDINARY_INCOME,
    MortgageFinancing,
    MortgageInterestDeductionPolicy,
    ObligationType,
    PrimaryResidenceAssignment,
    PropertyLifecycleEvent,
    PropertySaleEvent,
    RecurringObligation,
    RecurringPropertyCashflow,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    SetPrimaryResidenceEvent,
)
from finance.augur.sim.testing.case import Case, flat, levels, scenario
from finance.augur.sim.testing.fixtures import (
    FINANCED_PROPERTY_ACCOUNTS,
    SF,
    SF_HOME,
    cash_spend,
    checking,
    county_property_tax,
    financed_property_case,
    home_mortgage,
    home_purchase,
    property_cashflow_case,
    property_depreciation_case,
    taxed,
    transfer,
)
from finance.augur.sim.testing.simulation_result import Backend

# A place with no property tax, so a property there costs only what its cashflows say.
UNTAXED_LOCATION = Location(
    location_id="test",
    display_name="Test",
    jurisdiction_ids=[],
    annual_property_tax_rate=0.0,
    annual_special_assessment=Decimal(0),
)

SALE_MONTH = 8
HOA_DUES = Decimal("450.01")
RENTED_FRACTION = 0.6
MONTHLY_RENT = Decimal(8_000)


def property_cashflow_gating_case() -> Case:
    """Cashflows naming a property, around the month it is bought and the month cash runs out."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(10)), ("seller", Decimal(0)), ("vendor", Decimal(0)), ("creditor", Decimal(0))),
            horizon_months=4,
            tax_profiles=[],
            scheduled_obligations=[
                cash_spend(
                    "unaffordable", month=2, agent_id="alice", to_agent_id="creditor", amount_due=Decimal("8.76")
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=1,
                    cause_id="buy-home",
                    property_id="home",
                    location_id="test",
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    seller_account_id="checking",
                    purchase_price=Decimal(1),
                    down_payment=Decimal(1),
                )
            ],
            scheduled_property_cashflows=[
                ScheduledPropertyCashflow(
                    month=0,
                    property_id="home",
                    cause_id="before-purchase",
                    from_agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="vendor",
                    to_account_id="checking",
                    amount=Decimal("0.03"),
                ),
                ScheduledPropertyCashflow(
                    month=1,
                    property_id="home",
                    cause_id="purchase-month",
                    from_agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="vendor",
                    to_account_id="checking",
                    amount=Decimal("0.05"),
                ),
            ],
            recurring_property_cashflows=[
                RecurringPropertyCashflow(
                    start_month=0,
                    end_month=3,
                    property_id="home",
                    cause_id="property-carry",
                    from_agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="vendor",
                    to_account_id="checking",
                    amount=Decimal("0.10"),
                )
            ],
        ),
        rollout_count=1,
        locations={"test": UNTAXED_LOCATION},
    )


def property_obligation_case(*, dues_property_id: str = "home") -> Case:
    """A rented home whose HOA dues are a property-gated, Schedule E deductible obligation.

    Alice buys the home outright in month 0, rents 60% of it, and sells it in month 8. The dues
    name the property and tag themselves deductible, so they must accrue only while she owns it
    and take the rented share of each payment off her ordinary income for the year.

    Nothing else about the sale is under test: the home value never moves, and an all-land basis
    with no capitalized closing costs leaves nothing to depreciate, so the sale realizes no gain,
    no loss, and no recapture. `HOA_DUES` is odd against a 60% share on purpose: the share has to
    round the same way on each side.
    """

    return Case(
        scenario=scenario(
            checking(
                ("alice", Decimal(600_000)),
                ("seller", Decimal(0)),
                ("tenant", Decimal(120_000)),
                ("hoa", Decimal(0)),
                ("irs", Decimal(0)),
            ),
            horizon_months=12,
            scheduled_property_purchases=[
                home_purchase(
                    mortgage=None,
                    down_payment=Decimal(500_000),
                    buyer_closing_cost=Decimal(0),
                    rented_fraction=RENTED_FRACTION,
                    land_value_fraction=1.0,
                )
            ],
            recurring_property_cashflows=[
                RecurringPropertyCashflow(
                    start_month=0,
                    end_month=11,
                    property_id="home",
                    cause_id="rent",
                    from_agent_id="tenant",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=MONTHLY_RENT,
                    income_category=ORDINARY_INCOME,
                )
            ],
            recurring_obligations=[
                RecurringObligation(
                    start_month=0,
                    end_month=11,
                    obligation_id="hoa-dues",
                    obligation_type=ObligationType.HOA_DUES,
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="hoa",
                    to_account_id="checking",
                    amount_due=HOA_DUES,
                    property_id=dues_property_id,
                    deduction_category="ordinary",
                )
            ],
            property_lifecycle_events=[PropertySaleEvent(month=SALE_MONTH, property_id="home", closing_cost_pct=0.0)],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=1,
        locations={"sf": SF},
        series={SF_HOME: flat(Decimal(500_000), rollout_count=1, horizon_months=12)},
    )


def property_sale_case() -> Case:
    """The financed home sold in month 2, into two different markets."""

    return Case(
        scenario=scenario(
            [*checking(*FINANCED_PROPERTY_ACCOUNTS), *checking(("tenant", Decimal(100)), ("gift", Decimal(10)))],
            horizon_months=4,
            tax_profiles=[],
            scheduled_property_purchases=[home_purchase(mortgage=home_mortgage())],
            property_tax_policies=[county_property_tax()],
            scheduled_transfers=[
                transfer(
                    "sale-month-generic-transfer",
                    month=2,
                    from_agent_id="gift",
                    to_agent_id="alice",
                    amount=Decimal("0.07"),
                )
            ],
            recurring_property_cashflows=[
                RecurringPropertyCashflow(
                    start_month=0,
                    end_month=3,
                    property_id="home",
                    cause_id="rent",
                    from_agent_id="tenant",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=Decimal(10),
                )
            ],
            property_lifecycle_events=[PropertySaleEvent(month=2, property_id="home", closing_cost_pct=6.0)],
        ),
        rollout_count=2,
        locations={"sf": SF},
        series={
            SF_HOME: levels(
                [
                    [Decimal(500_000), Decimal(500_000), Decimal(600_000), Decimal(600_000), Decimal(600_000)],
                    [Decimal(500_000), Decimal(500_000), Decimal(550_000), Decimal(550_000), Decimal(550_000)],
                ]
            )
        },
    )


SECTION_121_OWNERS = (
    ("alice", "alice-home", "seller-a"),
    ("bob", "bob-home", "seller-b"),
    ("carol", "carol-home", "seller-c"),
    ("dave", "dave-home", "seller-d"),
)


def section_121_case() -> Case:
    """Four owners whose occupancy clocks land on either side of the 24-of-60-month test."""

    sales: list[PropertyLifecycleEvent] = [
        PropertySaleEvent(month=30, property_id=property_id, closing_cost_pct=0.0)
        for property_id in ("alice-home", "bob-home", "carol-home")
    ]
    sales.append(PropertySaleEvent(month=84, property_id="dave-home", closing_cost_pct=0.0))
    return Case(
        scenario=scenario(
            [
                *checking(*[(agent_id, Decimal(600_000)) for agent_id, _, _ in SECTION_121_OWNERS]),
                *checking(*[(seller_id, Decimal(0)) for _, _, seller_id in SECTION_121_OWNERS]),
                *checking(("irs", Decimal(0))),
            ],
            horizon_months=86,
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id=f"{agent_id}-buys-home",
                    property_id=property_id,
                    location_id="sf",
                    buyer_agent_id=agent_id,
                    buyer_account_id="checking",
                    seller_agent_id=seller_id,
                    seller_account_id="checking",
                    purchase_price=Decimal(500_000),
                    down_payment=Decimal(500_000),
                    buyer_closing_cost=Decimal(0),
                    rented_fraction=0.0,
                )
                for agent_id, property_id, seller_id in SECTION_121_OWNERS
            ],
            initial_primary_residences=[
                PrimaryResidenceAssignment(agent_id="alice", property_id="alice-home"),
                PrimaryResidenceAssignment(agent_id="dave", property_id="dave-home"),
            ],
            primary_residence_events=[
                SetPrimaryResidenceEvent(month=7, agent_id="bob", property_id="bob-home"),
                SetPrimaryResidenceEvent(month=24, agent_id="dave", property_id=None),
                SetPrimaryResidenceEvent(month=30, agent_id="carol", property_id="carol-home"),
            ],
            property_lifecycle_events=sales,
            tax_profiles=[taxed(agent_id, "federal_us") for agent_id, _, _ in SECTION_121_OWNERS],
        ),
        rollout_count=1,
        locations={"sf": SF},
        series={SF_HOME: levels([[Decimal(500_000)] * 30 + [Decimal(750_000)] * 57])},
    )


def uncapped_mortgage_interest_case() -> Case:
    """A loan exactly at its principal cap, so the whole year's owner interest is deductible."""

    return property_cashflow_case(
        purchase=home_purchase(
            mortgage=home_mortgage(principal=Decimal(800_000)),
            purchase_price=Decimal(1_000_000),
            down_payment=Decimal(200_000),
        ),
        property_tax_policies=[],
        mortgage_interest_deduction_policies=[
            MortgageInterestDeductionPolicy(
                liability_id="home-mortgage",
                owner_agent_id="alice",
                per_jurisdiction_principal_cap={"federal_us": Decimal(800_000)},
            )
        ],
    )


MID_OWNERS = (("alice", "seller-a", "bank-a"), ("bob", "seller-b", "bank-b"))


def mortgage_interest_policy_case() -> Case:
    """Two identical loans, one acquisition debt and one home-equity debt.

    Both jurisdictions' caps are the real ones — $750k federal, $1M California — and the
    principal sits above the federal cap, so the deduction scales on one side and not the other.
    """

    return Case(
        scenario=scenario(
            [
                *checking(*[(agent_id, Decimal(300_000)) for agent_id, _, _ in MID_OWNERS]),
                *checking(*[(seller_id, Decimal(0)) for _, seller_id, _ in MID_OWNERS]),
                *checking(*[(bank_id, Decimal(0)) for _, _, bank_id in MID_OWNERS]),
                *checking(("irs", Decimal(0))),
            ],
            horizon_months=12,
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id=f"{agent_id}-buys-home",
                    property_id=f"{agent_id}-home",
                    location_id="sf",
                    buyer_agent_id=agent_id,
                    buyer_account_id="checking",
                    seller_agent_id=seller_id,
                    seller_account_id="checking",
                    purchase_price=Decimal(1_000_000),
                    down_payment=Decimal(200_000),
                    buyer_closing_cost=Decimal(0),
                    rented_fraction=0.0,
                    land_value_fraction=1.0,
                    mortgage=MortgageFinancing(
                        liability_id=f"{agent_id}-mortgage",
                        lender_agent_id=bank_id,
                        lender_account_id="checking",
                        principal=Decimal(800_000),
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                )
                for agent_id, seller_id, bank_id in MID_OWNERS
            ],
            mortgage_interest_deduction_policies=[
                MortgageInterestDeductionPolicy(
                    liability_id="alice-mortgage", owner_agent_id="alice", debt_class="acquisition"
                ),
                MortgageInterestDeductionPolicy(
                    liability_id="bob-mortgage", owner_agent_id="bob", debt_class="home_equity"
                ),
            ],
            tax_profiles=[taxed(agent_id, "federal_us", "california") for agent_id, _, _ in MID_OWNERS],
        ),
        rollout_count=1,
        locations={"sf": SF},
    )


def test_backends_agree_on_a_financed_purchase_and_its_first_carry_month() -> None:
    result = assert_backends_agree(financed_property_case())
    # Month 2 is the first snapshot after the purchase month's payment has settled.
    purchased = result.properties.filter(pl.col("month_index") == 2).to_dicts()[0]
    stake = result.property_stakes.filter(pl.col("month_index") == 2).to_dicts()[0]
    mortgage = result.liabilities.filter(pl.col("month_index") == 2).to_dicts()[0]

    assert purchased["property_id"] == "home"
    assert purchased["location_id"] == "sf"
    assert purchased["adjusted_basis_quanta"] == 51_000_000
    assert stake["contribution_used_quanta"] == 11_000_000
    assert stake["equity_ledger_quanta"] == 10_000_000
    assert mortgage["monthly_payment_quanta"] == 239_820
    assert mortgage["principal_quanta"] == 39_960_180
    assert mortgage["interest_paid_ytd_quanta"] == 200_000

    payment = result.events.mortgage_payments.sort("month_index").to_dicts()[0]
    assert payment["interest_quanta"] == 200_000
    assert payment["principal_quanta"] == 39_820
    assert payment["total_payment_quanta"] == 239_820


def test_backends_agree_on_property_cashflows_and_their_tax_tagging() -> None:
    """A one-off leasing fee, a monthly management fee, and monthly rent."""

    result = assert_backends_agree(property_cashflow_case())
    causes = result.events.transfers.get_column("cause_id").to_list()

    assert causes.count("leasing-fee") == 1
    assert causes.count("management-fee") == 12
    assert causes.count("rent") == 12
    [accrual] = result.tax_accrual_details.to_dicts()
    assert accrual["ordinary_income_quanta"] == 5_300_000
    assert accrual["total_tax_quanta"] == 437_600


def test_backends_agree_that_property_cashflows_are_gated_on_ownership() -> None:
    """No cashflow before the purchase month, and none after the rollout fails."""

    result = assert_backends_agree(property_cashflow_gating_case())

    assert result.rollout_status.get_column("failed_month").to_list() == [2]


def test_backends_agree_on_property_gated_deductible_obligations() -> None:
    """The dues accrue while the home is owned, stop at the sale, and shelter that year's income."""

    result = assert_backends_agree(property_obligation_case())
    dues = result.events.obligation_settlements.filter(pl.col("obligation_type") == "hoa_dues")
    owned_months = list(range(SALE_MONTH))
    dues_quanta = int(HOA_DUES * 100)

    assert dues.sort("month_index").get_column("month_index").to_list() == owned_months
    assert dues.get_column("amount_paid_quanta").to_list() == [dues_quanta] * len(owned_months)
    # The rent stops with the dues, so the year's income is what the months of ownership earned,
    # less the rented share of every payment the property obligated her to make.
    deducted = len(owned_months) * round(dues_quanta * RENTED_FRACTION)
    [federal] = result.events.tax_breakdowns.to_dicts()
    assert federal["ordinary_income_quanta"] == len(owned_months) * int(MONTHLY_RENT * 100) - deducted


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_every_backend_rejects_an_obligation_naming_an_unknown_property(backend: Backend) -> None:
    with pytest.raises(ValueError, match="unknown property"):
        backend(property_obligation_case(dues_property_id="not-a-property"))


def test_backends_agree_on_the_property_sale_lifecycle() -> None:
    result = assert_backends_agree(property_sale_case())

    assert result.property_sale_details.get_column("gross_proceeds_quanta").to_list() == [56_400_000, 51_700_000]
    # The property and its mortgage leave the books in the sale month.
    assert result.properties.filter(pl.col("month_index") >= 3).is_empty()
    assert result.liabilities.filter(pl.col("month_index") >= 3).is_empty()
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_primary_residence_events_and_section_121_boundaries() -> None:
    """The exact 24-of-trailing-60-month use test, at and around its edges."""

    result = assert_backends_agree(section_121_case())
    occupancy = result.property_details.filter(pl.col("month_index") == 30).sort("rollout_index")

    assert occupancy.get_column("owner_occupied_months").to_list() == [30, 23, 0, 24]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_rental_transitions_capex_and_depreciation() -> None:
    result = assert_backends_agree(property_depreciation_case(sale=False))

    assert not result.events.set_rented_fraction_events.is_empty()
    assert not result.events.capital_improvement_events.is_empty()
    # Depreciation accrues only once the property is rented, so month 6 is still zero.
    details = result.property_details.filter(pl.col("rollout_index") == 0).sort("month_index")
    assert details.filter(pl.col("month_index") == 6).get_column("cumulative_depreciation_quanta").item() == 0
    assert details.filter(pl.col("cumulative_depreciation_quanta") > 0).height > 0
    [accrual] = result.tax_accrual_details.filter(pl.col("jurisdiction_id") == "federal_us").to_dicts()
    assert accrual["section_1250_recapture_quanta"] == 0


def test_backends_agree_on_uncapped_acquisition_mortgage_interest() -> None:
    """At the principal cap, the whole year's owner interest is deductible."""

    result = assert_backends_agree(uncapped_mortgage_interest_case())
    interest = int(result.events.mortgage_payments.get_column("interest_quanta").sum())
    [federal] = result.events.tax_breakdowns.filter(pl.col("jurisdiction_id") == "federal_us").to_dicts()

    assert federal["mortgage_interest_deduction_quanta"] == interest


def test_backends_agree_on_mid_principal_caps_and_home_equity_exclusion() -> None:
    """Each jurisdiction caps on its own acquisition-debt principal; equity debt is out."""

    result = assert_backends_agree(mortgage_interest_policy_case())
    breakdowns = {(row["agent_id"], row["jurisdiction_id"]): row for row in result.events.tax_breakdowns.to_dicts()}

    alice_federal = breakdowns[("alice", "federal_us")]["mortgage_interest_deduction_quanta"]
    alice_california = breakdowns[("alice", "california")]["mortgage_interest_deduction_quanta"]
    assert 0 < alice_federal < alice_california
    # The two effective caps are 750k federal and the loan's own 800k in California.
    assert alice_federal == round(alice_california * 75 / 80)
    # Bob's loan is home-equity debt, which neither jurisdiction allows.
    assert breakdowns[("bob", "federal_us")]["mortgage_interest_deduction_quanta"] == 0
    assert breakdowns[("bob", "california")]["mortgage_interest_deduction_quanta"] == 0


if __name__ == "__main__":
    pytest_bazel.main()
