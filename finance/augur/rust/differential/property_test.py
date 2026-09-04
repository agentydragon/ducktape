"""Rust/JAX differential coverage for the financed-property lifecycle: purchase, carrying
costs, rental transitions, sale, and mortgage interest.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

import json
from typing import Any

import polars as pl
import pytest
import pytest_bazel

from finance.augur.rust.differential.backend import BACKENDS, Backend, assert_backends_agree
from finance.augur.rust.differential.fixtures import (
    failure_fixture,
    financed_property_fixture,
    property_cashflow_fixture,
    property_depreciation_fixture,
    tax_fixture,
)
from finance.augur.rust.fixture_spec import account_ref, shared_series


def property_cashflow_gating_fixture() -> dict[str, Any]:
    fixture = failure_fixture()
    fixture["rollout_count"] = 1
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 4
    scenario["accounts"] = [
        {"account": account_ref("alice", "checking"), "opening_balance": 1_000},
        {"account": account_ref("seller", "checking"), "opening_balance": 0},
        {"account": account_ref("vendor", "checking"), "opening_balance": 0},
        {"account": account_ref("creditor", "checking"), "opening_balance": 0},
    ]
    scenario["scheduled_transfers"] = []
    scenario["recurring_transfers"] = []
    scenario["obligations"] = [
        {
            "month": 2,
            "obligation_id": "unaffordable",
            "from": account_ref("alice", "checking"),
            "to": account_ref("creditor", "checking"),
            "amount_due": 876,
        }
    ]
    scenario["recurring_obligations"] = []
    scenario["locations"] = [
        {
            "location_id": "test",
            "display_name": "Test",
            "jurisdiction_ids": [],
            "annual_property_tax_rate_ppb": 0,
            "annual_special_assessment": 0,
        }
    ]
    scenario["scheduled_property_purchases"] = [
        {
            "month": 1,
            "cause_id": "buy-home",
            "property_id": "home",
            "location_id": "test",
            "buyer_agent_id": "alice",
            "buyer_account_id": "checking",
            "seller_agent_id": "seller",
            "seller_account_id": "checking",
            "purchase_price": 100,
            "down_payment": 100,
            "buyer_closing_cost": 0,
            "mortgage": None,
        }
    ]
    scenario["property_tax_policies"] = []
    scenario["scheduled_property_cashflows"] = [
        {
            "month": 0,
            "property_id": "home",
            "cause_id": "before-purchase",
            "from": account_ref("alice", "checking"),
            "to": account_ref("vendor", "checking"),
            "amount": 3,
        },
        {
            "month": 1,
            "property_id": "home",
            "cause_id": "purchase-month",
            "from": account_ref("alice", "checking"),
            "to": account_ref("vendor", "checking"),
            "amount": 5,
        },
    ]
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 3,
            "property_id": "home",
            "cause_id": "property-carry",
            "from": account_ref("alice", "checking"),
            "to": account_ref("vendor", "checking"),
            "amount": 10,
        }
    ]
    scenario["initial_lots"] = []
    scenario["scheduled_sales"] = []
    scenario["tax_profiles"] = []
    scenario["distributions"] = []
    fixture["series"] = []
    return fixture


SALE_MONTH = 8
HOA_DUES = 45_001
RENTED_FRACTION_PPB = 600_000_000
MONTHLY_RENT = 800_000


def property_obligation_fixture() -> dict[str, Any]:
    """A rented home whose HOA dues are a property-gated, Schedule E deductible obligation.

    Alice buys the home outright in month 0, rents 60% of it, and sells it in month 8. The dues
    name the property and tag themselves deductible, so they must accrue only while she owns it
    and take the rented share of each payment off her ordinary income for the year.

    Nothing else about the sale is under test: the home value never moves, and an all-land basis
    with no capitalized closing costs leaves nothing to depreciate, so the sale realizes no gain,
    no loss, and no recapture to trip the §1250-rate gap in README § Fixture gotchas. `HOA_DUES`
    is odd against a 60% share on purpose: the share has to round the same way on each side.
    """

    fixture = financed_property_fixture()
    fixture["rollout_count"] = 1
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["accounts"] = [
        {"account": account_ref("alice", "checking"), "opening_balance": 60_000_000},
        {"account": account_ref("seller", "checking"), "opening_balance": 0},
        {"account": account_ref("tenant", "checking"), "opening_balance": 12_000_000},
        {"account": account_ref("hoa", "checking"), "opening_balance": 0},
        {"account": account_ref("irs", "checking"), "opening_balance": 0},
    ]
    purchase = scenario["scheduled_property_purchases"][0]
    purchase["mortgage"] = None
    purchase["down_payment"] = purchase["purchase_price"]
    purchase["buyer_closing_cost"] = 0
    purchase["rented_fraction_ppb"] = RENTED_FRACTION_PPB
    purchase["land_value_fraction_ppb"] = 1_000_000_000
    scenario["property_tax_policies"] = []
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 11,
            "property_id": "home",
            "cause_id": "rent",
            "from": account_ref("tenant", "checking"),
            "to": account_ref("alice", "checking"),
            "amount": MONTHLY_RENT,
            "income_category": "ordinary",
        }
    ]
    scenario["recurring_obligations"] = [
        {
            "start_month": 0,
            "end_month": 11,
            "obligation_id": "hoa-dues",
            "obligation_type": "hoa_dues",
            "from": account_ref("alice", "checking"),
            "to": account_ref("hoa", "checking"),
            "amount_due": HOA_DUES,
            "property_id": "home",
            "deduction_category": "ordinary",
        }
    ]
    scenario["property_sales"] = [{"month": SALE_MONTH, "property_id": "home", "closing_cost_bps": 0}]
    federal_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    federal_profile["jurisdictions"] = federal_profile["jurisdictions"][:1]
    scenario["tax_profiles"] = [federal_profile]
    fixture["series"] = [shared_series("home_value:sf", rollout_count=1, path=[50_000_000] * 13)]
    return fixture


def property_sale_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    fixture["rollout_count"] = 2
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 4
    scenario["accounts"].extend(
        [
            {"account": account_ref("tenant", "checking"), "opening_balance": 10_000},
            {"account": account_ref("gift", "checking"), "opening_balance": 1_000},
        ]
    )
    scenario["scheduled_transfers"] = [
        {
            "month": 2,
            "cause_id": "sale-month-generic-transfer",
            "from": account_ref("gift", "checking"),
            "to": account_ref("alice", "checking"),
            "amount": 7,
        }
    ]
    scenario["recurring_property_cashflows"] = [
        {
            "start_month": 0,
            "end_month": 3,
            "property_id": "home",
            "cause_id": "rent",
            "from": account_ref("tenant", "checking"),
            "to": account_ref("alice", "checking"),
            "amount": 1_000,
        }
    ]
    scenario["property_sales"] = [{"month": 2, "property_id": "home", "closing_cost_bps": 600}]
    fixture["series"] = [
        {
            "series_id": "home_value:sf",
            "snapshots": 5,
            "values": [
                50_000_000,
                50_000_000,
                60_000_000,
                60_000_000,
                60_000_000,
                50_000_000,
                50_000_000,
                55_000_000,
                55_000_000,
                55_000_000,
            ],
        }
    ]
    return fixture


def section_121_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 86
    scenario["accounts"] = [
        *[
            {"account": {"agent_id": agent_id, "account_id": "checking"}, "opening_balance": 60_000_000}
            for agent_id in ("alice", "bob", "carol", "dave")
        ],
        *[
            {"account": {"agent_id": seller_id, "account_id": "checking"}, "opening_balance": 0}
            for seller_id in ("seller-a", "seller-b", "seller-c", "seller-d")
        ],
        {"account": account_ref("irs", "checking"), "opening_balance": 0},
    ]
    scenario["scheduled_property_purchases"] = [
        {
            "month": 0,
            "cause_id": f"{agent_id}-buys-home",
            "property_id": property_id,
            "location_id": "sf",
            "buyer_agent_id": agent_id,
            "buyer_account_id": "checking",
            "seller_agent_id": seller_id,
            "seller_account_id": "checking",
            "purchase_price": 50_000_000,
            "down_payment": 50_000_000,
            "buyer_closing_cost": 0,
            "rented_fraction_ppb": 0,
            "mortgage": None,
        }
        for agent_id, property_id, seller_id in (
            ("alice", "alice-home", "seller-a"),
            ("bob", "bob-home", "seller-b"),
            ("carol", "carol-home", "seller-c"),
            ("dave", "dave-home", "seller-d"),
        )
    ]
    scenario["initial_primary_residences"] = [
        {"agent_id": "alice", "property_id": "alice-home"},
        {"agent_id": "dave", "property_id": "dave-home"},
    ]
    scenario["primary_residence_events"] = [
        {"month": 7, "agent_id": "bob", "property_id": "bob-home"},
        {"month": 24, "agent_id": "dave", "property_id": None},
        {"month": 30, "agent_id": "carol", "property_id": "carol-home"},
    ]
    scenario["property_sales"] = [
        {"month": 30, "property_id": property_id, "closing_cost_bps": 0}
        for property_id in ("alice-home", "bob-home", "carol-home")
    ] + [{"month": 84, "property_id": "dave-home", "closing_cost_bps": 0}]
    scenario["property_tax_policies"] = []
    federal_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    federal_profile["jurisdictions"] = federal_profile["jurisdictions"][:1]
    scenario["tax_profiles"] = []
    for agent_id in ("alice", "bob", "carol", "dave"):
        profile = json.loads(json.dumps(federal_profile))
        profile["agent_id"] = agent_id
        profile["section_121_exclusion"] = 25_000_000
        scenario["tax_profiles"].append(profile)
    fixture["series"] = [
        {"series_id": "home_value:sf", "snapshots": 87, "values": [50_000_000] * 30 + [75_000_000] * 57}
    ]
    return fixture


def uncapped_mortgage_interest_fixture() -> dict[str, Any]:
    fixture = property_cashflow_fixture()
    scenario = fixture["scenario"]
    purchase = scenario["scheduled_property_purchases"][0]
    purchase["purchase_price"] = 100_000_000
    purchase["down_payment"] = 20_000_000
    purchase["buyer_closing_cost"] = 1_000_000
    purchase["mortgage"]["principal"] = 80_000_000
    purchase["rented_fraction_ppb"] = 0
    scenario["property_tax_policies"] = []
    scenario["mortgage_interest_deduction_policies"] = [{"liability_id": "home-mortgage", "owner_agent_id": "alice"}]
    return fixture


def mortgage_interest_policy_fixture() -> dict[str, Any]:
    fixture = financed_property_fixture()
    scenario = fixture["scenario"]
    scenario["horizon_months"] = 12
    scenario["accounts"] = [
        {"account": account_ref("alice", "checking"), "opening_balance": 30_000_000},
        {"account": account_ref("bob", "checking"), "opening_balance": 30_000_000},
        {"account": account_ref("seller-a", "checking"), "opening_balance": 0},
        {"account": account_ref("seller-b", "checking"), "opening_balance": 0},
        {"account": account_ref("bank-a", "checking"), "opening_balance": 0},
        {"account": account_ref("bank-b", "checking"), "opening_balance": 0},
        {"account": account_ref("irs", "checking"), "opening_balance": 0},
    ]
    scenario["scheduled_property_purchases"] = [
        {
            "month": 0,
            "cause_id": f"{agent_id}-buys-home",
            "property_id": f"{agent_id}-home",
            "location_id": "sf",
            "buyer_agent_id": agent_id,
            "buyer_account_id": "checking",
            "seller_agent_id": seller_id,
            "seller_account_id": "checking",
            "purchase_price": 100_000_000,
            "down_payment": 20_000_000,
            "buyer_closing_cost": 0,
            "rented_fraction_ppb": 0,
            "land_value_fraction_ppb": 1_000_000_000,
            "mortgage": {
                "liability_id": f"{agent_id}-mortgage",
                "lender_agent_id": bank_id,
                "lender_account_id": "checking",
                "principal": 80_000_000,
                "annual_interest_rate_ppb": 60_000_000,
                "term_months": 360,
            },
        }
        for agent_id, seller_id, bank_id in (("alice", "seller-a", "bank-a"), ("bob", "seller-b", "bank-b"))
    ]
    scenario["mortgage_interest_deduction_policies"] = [
        {
            "liability_id": "alice-mortgage",
            "owner_agent_id": "alice",
            "debt_class": "acquisition",
            "per_jurisdiction_principal_cap": {"federal_us": 75_000_000, "california": 100_000_000},
        },
        {
            "liability_id": "bob-mortgage",
            "owner_agent_id": "bob",
            "debt_class": "home_equity",
            "per_jurisdiction_principal_cap": {"federal_us": 75_000_000, "california": 100_000_000},
        },
    ]
    scenario["property_tax_policies"] = []
    scenario["scheduled_property_cashflows"] = []
    scenario["recurring_property_cashflows"] = []
    base_profile = tax_fixture()["scenario"]["tax_profiles"][0]
    scenario["tax_profiles"] = []
    for agent_id in ("alice", "bob"):
        profile = json.loads(json.dumps(base_profile))
        profile["agent_id"] = agent_id
        scenario["tax_profiles"].append(profile)
    fixture["series"] = []
    return fixture


def test_backends_agree_on_a_financed_purchase_and_its_first_carry_month() -> None:
    result = assert_backends_agree(financed_property_fixture())
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

    result = assert_backends_agree(property_cashflow_fixture())
    causes = result.events.transfers.get_column("cause_id").to_list()

    assert causes.count("leasing-fee") == 1
    assert causes.count("management-fee") == 12
    assert causes.count("rent") == 12
    [accrual] = result.tax_accrual_details.to_dicts()
    assert accrual["ordinary_income_quanta"] == 5_300_000
    assert accrual["total_tax_quanta"] == 437_600


def test_backends_agree_that_property_cashflows_are_gated_on_ownership() -> None:
    """No cashflow before the purchase month, and none after the rollout fails."""

    result = assert_backends_agree(property_cashflow_gating_fixture())

    assert result.rollout_status.get_column("failed_month").to_list() == [2]


def test_backends_agree_on_property_gated_deductible_obligations() -> None:
    """The dues accrue while the home is owned, stop at the sale, and shelter that year's income."""

    result = assert_backends_agree(property_obligation_fixture())
    dues = result.events.obligation_settlements.filter(pl.col("obligation_type") == "hoa_dues")
    owned_months = list(range(SALE_MONTH))

    assert dues.sort("month_index").get_column("month_index").to_list() == owned_months
    assert dues.get_column("amount_paid_quanta").to_list() == [HOA_DUES] * len(owned_months)
    # The rent stops with the dues, so the year's income is what the months of ownership earned,
    # less the rented share of every payment the property obligated her to make.
    deducted = len(owned_months) * round(HOA_DUES * RENTED_FRACTION_PPB / 1_000_000_000)
    [federal] = result.events.tax_breakdowns.to_dicts()
    assert federal["ordinary_income_quanta"] == len(owned_months) * MONTHLY_RENT - deducted


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_every_backend_rejects_an_obligation_naming_an_unknown_property(backend: Backend) -> None:
    fixture = property_obligation_fixture()
    fixture["scenario"]["recurring_obligations"][0]["property_id"] = "not-a-property"

    with pytest.raises(ValueError, match="unknown property"):
        backend(fixture)


def test_backends_agree_on_the_property_sale_lifecycle() -> None:
    result = assert_backends_agree(property_sale_fixture())

    assert result.property_sale_details.get_column("gross_proceeds_quanta").to_list() == [56_400_000, 51_700_000]
    # The property and its mortgage leave the books in the sale month.
    assert result.properties.filter(pl.col("month_index") >= 3).is_empty()
    assert result.liabilities.filter(pl.col("month_index") >= 3).is_empty()
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_primary_residence_events_and_section_121_boundaries() -> None:
    """The exact 24-of-trailing-60-month use test, at and around its edges."""

    result = assert_backends_agree(section_121_fixture())
    occupancy = result.property_details.filter(pl.col("month_index") == 30).sort("rollout_index")

    assert occupancy.get_column("owner_occupied_months").to_list() == [30, 23, 0, 24]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_rental_transitions_capex_and_depreciation() -> None:
    result = assert_backends_agree(property_depreciation_fixture(sale=False))

    assert not result.events.set_rented_fraction_events.is_empty()
    assert not result.events.capital_improvement_events.is_empty()
    # Depreciation accrues only once the property is rented, so month 6 is still zero.
    details = result.property_details.filter(pl.col("rollout_index") == 0).sort("month_index")
    assert details.filter(pl.col("month_index") == 6).get_column("cumulative_depreciation_quanta").item() == 0
    assert details.filter(pl.col("cumulative_depreciation_quanta") > 0).height > 0
    [accrual] = result.tax_accrual_details.filter(pl.col("jurisdiction_id") == "federal_us").to_dicts()
    assert accrual["section_1250_recapture_quanta"] == 0


def test_backends_agree_on_uncapped_acquisition_mortgage_interest() -> None:
    """Under the principal cap, the whole year's owner interest is deductible."""

    result = assert_backends_agree(uncapped_mortgage_interest_fixture())
    interest = int(result.events.mortgage_payments.get_column("interest_quanta").sum())
    [federal] = result.events.tax_breakdowns.filter(pl.col("jurisdiction_id") == "federal_us").to_dicts()

    assert federal["mortgage_interest_deduction_quanta"] == interest


def test_backends_agree_on_mid_principal_caps_and_home_equity_exclusion() -> None:
    """Each jurisdiction caps on its own acquisition-debt principal; equity debt is out."""

    result = assert_backends_agree(mortgage_interest_policy_fixture())
    breakdowns = {(row["agent_id"], row["jurisdiction_id"]): row for row in result.events.tax_breakdowns.to_dicts()}

    alice_federal = breakdowns[("alice", "federal_us")]["mortgage_interest_deduction_quanta"]
    alice_california = breakdowns[("alice", "california")]["mortgage_interest_deduction_quanta"]
    assert 0 < alice_federal < alice_california
    # The two caps are 750k and 800k of principal, and the deduction scales with them.
    assert alice_federal == round(alice_california * 75 / 80)
    # Bob's loan is home-equity debt, which neither jurisdiction allows.
    assert breakdowns[("bob", "federal_us")]["mortgage_interest_deduction_quanta"] == 0
    assert breakdowns[("bob", "california")]["mortgage_interest_deduction_quanta"] == 0


if __name__ == "__main__":
    pytest_bazel.main()
