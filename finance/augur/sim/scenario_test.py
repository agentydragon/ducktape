"""What the authored declarations refuse, before anything is lowered or declared."""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.series import LocationId, RentKey, SecurityKey, SecuritySymbol
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId, LiabilityId, PropertyId
from finance.augur.sim.scenario import (
    DistributionTaxSlice,
    InterestIncome,
    MortgageFinancing,
    RecurringObligation,
    RecurringPropertyCashflow,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    SecurityDistribution,
    SeriesIndexedAmount,
)

CHECKING = AccountId("checking")
ALICE = AgentId("alice")


def test_series_indexed_amount_parses_from_authored_data() -> None:
    obligation = RecurringObligation.model_validate(
        {
            "start_month": 0,
            "obligation_id": "outside_rent",
            "obligation_type": "outside_rent",
            "agent_id": "alice",
            "from_account_id": "checking",
            "to_agent_id": "landlord",
            "to_account_id": "checking",
            "amount_due": {
                "kind": "series_indexed",
                "base_amount": 1000,
                "series": {"kind": "rent", "location_id": "san_francisco_ca"},
                "base_month_index": 0,
                "adjustment_period_months": 12,
            },
        }
    )

    amount = obligation.amount_due
    assert isinstance(amount, SeriesIndexedAmount)
    assert amount.series == RentKey(location_id=LocationId("san_francisco_ca"))


def test_cashflow_income_category_allows_only_the_typed_categories() -> None:
    scheduled_data = {
        "month": 0,
        "property_id": "home",
        "cause_id": "gift",
        "from_agent_id": "bob",
        "from_account_id": "checking",
        "to_agent_id": "alice",
        "to_account_id": "checking",
        "amount": 100,
        "income_category": "gift",
    }
    recurring_data = {**scheduled_data, "start_month": 0}
    del recurring_data["month"]

    # Asserts the FIELD is rejected, not pydantic's prose for why: the message moved when
    # `income_category` became a typed union, while the behaviour under test did not.
    for model, data in ((ScheduledPropertyCashflow, scheduled_data), (RecurringPropertyCashflow, recurring_data)):
        with pytest.raises(ValidationError) as rejected:
            model.model_validate(data)
        assert [error["loc"] for error in rejected.value.errors()] == [("income_category",)]


@pytest.mark.parametrize(
    ("down_payment", "mortgage_principal"),
    [
        pytest.param(Decimal(100000), None, id="cash-buyer-covers-a-fifth-of-the-price"),
        pytest.param(Decimal(100000), Decimal(300000), id="down-payment-plus-mortgage-leaves-a-gap"),
        pytest.param(Decimal(200000), Decimal(400000), id="down-payment-plus-mortgage-overshoots"),
    ],
)
def test_scheduled_property_purchase_rejects_terms_that_do_not_fund_the_price(
    down_payment: Decimal, mortgage_principal: Decimal | None
) -> None:
    # The seller receives the down payment and the buyer books `price - principal` of equity, so
    # terms that do not add up to the price would conjure equity (or destroy it) at settlement.
    with pytest.raises(ValidationError, match=r"property purchase 'buy_home' is not funded"):
        ScheduledPropertyPurchase(
            month=0,
            cause_id="buy_home",
            property_id=PropertyId("home"),
            location_id=LocationId("san_francisco"),
            buyer_agent_id=ALICE,
            buyer_account_id=CHECKING,
            seller_agent_id=AgentId("seller"),
            purchase_price=Decimal(500000),
            down_payment=down_payment,
            mortgage=None
            if mortgage_principal is None
            else MortgageFinancing(
                liability_id=LiabilityId("mortgage"),
                lender_agent_id=AgentId("lender"),
                principal=mortgage_principal,
                annual_interest_rate=0.06,
                term_months=360,
            ),
        )


def test_scheduled_property_purchase_accepts_closing_costs_on_top_of_a_funded_price() -> None:
    # Closing costs are the buyer's own expense, not part of what the seller is paid, so they are
    # outside the identity: a purchase funded to the price stays valid however large they are.
    purchase = ScheduledPropertyPurchase(
        month=0,
        cause_id="buy_home",
        property_id=PropertyId("home"),
        location_id=LocationId("san_francisco"),
        buyer_agent_id=ALICE,
        buyer_account_id=CHECKING,
        seller_agent_id=AgentId("seller"),
        purchase_price=Decimal(500000),
        down_payment=Decimal(100000),
        buyer_closing_cost=Decimal(15000),
        mortgage=MortgageFinancing(
            liability_id=LiabilityId("mortgage"),
            lender_agent_id=AgentId("lender"),
            principal=Decimal(400000),
            annual_interest_rate=0.06,
            term_months=360,
        ),
    )
    assert purchase.buyer_closing_cost == 15000


def test_a_distribution_tax_character_must_sum_to_one() -> None:
    """A short split pays out less than the fund distributes, which reads as a lower yield
    rather than as the misconfiguration it is."""

    with pytest.raises(ValidationError, match="fractions must sum to 1"):
        SecurityDistribution(
            asset=SecurityKey(symbol=SecuritySymbol("bnd")),
            agent_id=ALICE,
            holding_account_id=AccountId("brokerage"),
            to_account_id=CHECKING,
            tax_character=(
                DistributionTaxSlice(
                    fraction=0.4, income_category=InterestIncome(issuer_jurisdiction_id=JurisdictionId("federal_us"))
                ),
            ),
        )


if __name__ == "__main__":
    pytest_bazel.main()
