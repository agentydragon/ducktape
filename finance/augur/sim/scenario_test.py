"""What the scenario schema refuses, before any engine runs.

Every assertion here is about `Scenario` construction alone: a scenario that cannot be
built cannot be simulated, so these hold whichever engine would have run it. They live
apart from the behavioural suites for that reason — nothing here executes a simulation.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.series import LocationId, RentKey, SecurityKey, SecuritySymbol
from finance.augur.sim.scenario import (
    Agent,
    DistributionTaxSlice,
    InitialAccountBalance,
    InitialLot,
    MortgageFinancing,
    PrimaryResidenceAssignment,
    PropertySaleEvent,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SecurityDistribution,
    SeriesIndexedAmount,
    SetPrimaryResidenceEvent,
    SetRentedFractionEvent,
    SleeveTarget,
    TargetAllocationPolicy,
    TaxProfile,
)

CodeMatrix = npt.NDArray[np.int64]
FloatMatrix = npt.NDArray[np.float64]


def _property_link_validation_scenario(
    *,
    scheduled_property_purchases: list[ScheduledPropertyPurchase] | None = None,
    property_tax_policies: list[PropertyTaxPolicy] | None = None,
    tax_profiles: list[TaxProfile] | None = None,
) -> Scenario:
    return Scenario(
        agents=[
            Agent(agent_id="alice"),
            Agent(agent_id="bob"),
            Agent(agent_id="seller"),
            Agent(agent_id="lender"),
            Agent(agent_id="tax_authority"),
            Agent(agent_id="irs"),
        ],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=1000000),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance=1000000),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="lender", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="tax_authority", account_id="checking", balance=0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance=0),
        ],
        scheduled_property_purchases=scheduled_property_purchases
        or [
            ScheduledPropertyPurchase(
                month=0,
                cause_id="p1_purchase",
                property_id="p1",
                location_id="san_francisco",
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price=500000,
                down_payment=500000,
            )
        ],
        property_tax_policies=property_tax_policies or [],
        tax_profiles=tax_profiles or [],
        horizon_months=12,
    )


def test_series_indexed_amount_parses_from_scenario_data() -> None:
    scenario = Scenario.model_validate(
        {
            "agents": [{"agent_id": "alice"}, {"agent_id": "landlord"}],
            "initial_cash": [
                {"agent_id": "alice", "account_id": "checking", "balance": 10000},
                {"agent_id": "landlord", "account_id": "checking", "balance": 0},
            ],
            "recurring_obligations": [
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
            ],
            "tax_profiles": [],
            "horizon_months": 13,
        }
    )

    amount = scenario.recurring_obligations[0].amount_due
    assert isinstance(amount, SeriesIndexedAmount)
    assert amount.series == RentKey(location_id=LocationId("san_francisco_ca"))


def test_scenario_requires_explicit_tax_profiles() -> None:
    with pytest.raises(ValidationError, match="tax_profiles"):
        Scenario.model_validate(
            {
                "agents": [{"agent_id": "alice"}],
                "initial_cash": [{"agent_id": "alice", "account_id": "checking", "balance": 100}],
                "horizon_months": 1,
            }
        )


def test_scenario_rejects_duplicate_liquidity_policy_accounts() -> None:
    with pytest.raises(ValidationError, match=r"duplicate funding policies.*alice/checking"):
        Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance=100)],
            target_allocation_policies=[
                TargetAllocationPolicy(
                    agent_id="alice",
                    account_id="checking",
                    sleeves=[SleeveTarget(asset=SecurityKey(symbol=SecuritySymbol("vti")), weight=1)],
                    cash_ceiling=0,
                ),
                TargetAllocationPolicy(
                    agent_id="alice",
                    account_id="checking",
                    sleeves=[SleeveTarget(asset=SecurityKey(symbol=SecuritySymbol("qqq")), weight=1)],
                    cash_ceiling=0,
                ),
            ],
            tax_profiles=[],
            horizon_months=1,
        )


def test_scenario_rejects_duplicate_tax_profile_agent_ids() -> None:
    with pytest.raises(ValidationError, match=r"duplicate TaxProfile\.agent_id.*'alice'"):
        _property_link_validation_scenario(
            tax_profiles=[
                TaxProfile(agent_id="alice", jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs"),
                TaxProfile(agent_id="alice", jurisdiction_ids=["california"], tax_authority_agent_id="irs"),
            ]
        )


def test_scenario_rejects_duplicate_mortgage_liability_ids() -> None:
    with pytest.raises(ValidationError, match=r"duplicate mortgage liability_id.*'shared_mortgage'.*'p1'.*'p2'"):
        _property_link_validation_scenario(
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p1_purchase",
                    property_id="p1",
                    location_id="san_francisco",
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=500000,
                    down_payment=100000,
                    mortgage=MortgageFinancing(
                        liability_id="shared_mortgage",
                        lender_agent_id="lender",
                        principal=400000,
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                ),
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="p2_purchase",
                    property_id="p2",
                    location_id="san_francisco",
                    buyer_agent_id="bob",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=600000,
                    down_payment=120000,
                    mortgage=MortgageFinancing(
                        liability_id="shared_mortgage",
                        lender_agent_id="lender",
                        principal=480000,
                        annual_interest_rate=0.06,
                        term_months=360,
                    ),
                ),
            ]
        )


def test_scenario_rejects_overlapping_property_tax_policies_for_same_property_month() -> None:
    with pytest.raises(ValidationError, match=r"overlapping property tax policies.*'p1'.*month 6"):
        _property_link_validation_scenario(
            property_tax_policies=[
                PropertyTaxPolicy(
                    property_id="p1",
                    owner_agent_id="alice",
                    tax_authority_agent_id="tax_authority",
                    start_month=0,
                    end_month=6,
                ),
                PropertyTaxPolicy(
                    property_id="p1",
                    owner_agent_id="alice",
                    tax_authority_agent_id="tax_authority",
                    start_month=6,
                    end_month=11,
                ),
            ]
        )


def test_scenario_rejects_property_tax_policy_owner_mismatch() -> None:
    with pytest.raises(
        ValidationError, match=r"property tax policy.*property_id 'p1'.*owner_agent_id='bob'.*buyer_agent_id.*'alice'"
    ):
        _property_link_validation_scenario(
            property_tax_policies=[
                PropertyTaxPolicy(property_id="p1", owner_agent_id="bob", tax_authority_agent_id="tax_authority")
            ]
        )


def test_scenario_rejects_duplicate_lot_purchase_months_within_fifo_pool() -> None:
    with pytest.raises(
        ValidationError, match=r"duplicate initial lot purchase months.*alice/checking/security:vti@-12.*old_a.*old_b"
    ):
        Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance=0)],
            initial_lots=[
                InitialLot(
                    lot_id="old_a",
                    agent_id="alice",
                    asset=SecurityKey(symbol=SecuritySymbol("vti")),
                    purchase_month_index=-12,
                    quantity=10.0,
                    cost_basis_per_unit=80,
                ),
                InitialLot(
                    lot_id="old_b",
                    agent_id="alice",
                    asset=SecurityKey(symbol=SecuritySymbol("vti")),
                    purchase_month_index=-12,
                    quantity=5.0,
                    cost_basis_per_unit=90,
                ),
            ],
            tax_profiles=[],
            horizon_months=1,
        )


def test_duplicate_lot_purchase_months_are_allowed_in_different_accounts() -> None:
    Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance=0)],
        initial_lots=[
            InitialLot(
                lot_id="taxable_old",
                agent_id="alice",
                account_id="taxable",
                asset=SecurityKey(symbol=SecuritySymbol("vti")),
                purchase_month_index=-12,
                quantity=10.0,
                cost_basis_per_unit=80,
            ),
            InitialLot(
                lot_id="ira_old",
                agent_id="alice",
                account_id="ira",
                asset=SecurityKey(symbol=SecuritySymbol("vti")),
                purchase_month_index=-12,
                quantity=5.0,
                cost_basis_per_unit=70,
            ),
        ],
        tax_profiles=[],
        horizon_months=1,
    )


def test_transfer_income_category_allows_only_ordinary() -> None:
    scheduled_data = {
        "month": 0,
        "cause_id": "gift",
        "from_agent_id": "bob",
        "from_account_id": "checking",
        "to_agent_id": "alice",
        "to_account_id": "checking",
        "amount": 100,
        "income_category": "gift",
    }
    recurring_data = {
        "start_month": 0,
        "cause_id": "gift",
        "from_agent_id": "bob",
        "from_account_id": "checking",
        "to_agent_id": "alice",
        "to_account_id": "checking",
        "amount": 100,
        "income_category": "gift",
    }

    # Asserts the FIELD is rejected, not pydantic's prose for why: the message moved when
    # `income_category` became a typed union, while the behaviour under test did not.
    for model, data in ((ScheduledTransfer, scheduled_data), (RecurringTransfer, recurring_data)):
        with pytest.raises(ValidationError) as rejected:
            model.model_validate(data)
        assert [error["loc"] for error in rejected.value.errors()] == [("income_category",)]


def test_scenario_rejects_out_of_horizon_scheduled_asset_sales() -> None:
    with pytest.raises(ValidationError, match=r"scheduled asset sale 'late_sale'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance=0)],
            initial_lots=[
                InitialLot(
                    lot_id="seed",
                    agent_id="alice",
                    asset=SecurityKey(symbol=SecuritySymbol("vti")),
                    purchase_month_index=0,
                    quantity=1.0,
                    cost_basis_per_unit=100,
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=2,
                    cause_id="late_sale",
                    agent_id="alice",
                    asset=SecurityKey(symbol=SecuritySymbol("vti")),
                    quantity=1.0,
                    proceeds_account_id="checking",
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )

    with pytest.raises(ValidationError, match=r"scheduled asset sale 'pre_sale'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance=0)],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=-1,
                    cause_id="pre_sale",
                    agent_id="alice",
                    asset=SecurityKey(symbol=SecuritySymbol("vti")),
                    quantity=1.0,
                    proceeds_account_id="checking",
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )


def test_scenario_rejects_out_of_horizon_scheduled_property_purchases() -> None:
    with pytest.raises(ValidationError, match=r"scheduled property purchase 'late_purchase'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="seller")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=100000),
                InitialAccountBalance(agent_id="seller", account_id="checking", balance=0),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=2,
                    cause_id="late_purchase",
                    property_id="home",
                    location_id="san_francisco",
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=500000,
                    down_payment=500000,
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )

    with pytest.raises(ValidationError, match=r"scheduled property purchase 'pre_purchase'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="seller")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=100000),
                InitialAccountBalance(agent_id="seller", account_id="checking", balance=0),
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=-1,
                    cause_id="pre_purchase",
                    property_id="home",
                    location_id="san_francisco",
                    buyer_agent_id="alice",
                    buyer_account_id="checking",
                    seller_agent_id="seller",
                    purchase_price=500000,
                    down_payment=500000,
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )


@pytest.mark.parametrize(
    ("down_payment", "mortgage_principal"),
    [
        pytest.param(100000, None, id="cash-buyer-covers-a-fifth-of-the-price"),
        pytest.param(100000, 300000, id="down-payment-plus-mortgage-leaves-a-gap"),
        pytest.param(200000, 400000, id="down-payment-plus-mortgage-overshoots"),
    ],
)
def test_scheduled_property_purchase_rejects_terms_that_do_not_fund_the_price(
    down_payment: int, mortgage_principal: int | None
) -> None:
    # The seller receives the down payment and the buyer books `price - principal` of equity, so
    # terms that do not add up to the price would conjure equity (or destroy it) at settlement.
    with pytest.raises(ValidationError, match=r"property purchase 'buy_home' is not funded"):
        ScheduledPropertyPurchase(
            month=0,
            cause_id="buy_home",
            property_id="home",
            location_id="san_francisco",
            buyer_agent_id="alice",
            buyer_account_id="checking",
            seller_agent_id="seller",
            purchase_price=500000,
            down_payment=down_payment,
            mortgage=None
            if mortgage_principal is None
            else MortgageFinancing(
                liability_id="mortgage",
                lender_agent_id="lender",
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
        property_id="home",
        location_id="san_francisco",
        buyer_agent_id="alice",
        buyer_account_id="checking",
        seller_agent_id="seller",
        purchase_price=500000,
        down_payment=100000,
        buyer_closing_cost=15000,
        mortgage=MortgageFinancing(
            liability_id="mortgage",
            lender_agent_id="lender",
            principal=400000,
            annual_interest_rate=0.06,
            term_months=360,
        ),
    )
    assert purchase.buyer_closing_cost == 15000


def test_scenario_rejects_a_distribution_tax_character_that_does_not_sum_to_one() -> None:
    """A short split pays out less than the fund distributes, which reads as a lower yield
    rather than as the misconfiguration it is."""

    with pytest.raises(ValidationError, match="fractions must sum to 1"):
        SecurityDistribution(
            asset=SecurityKey(symbol=SecuritySymbol("bnd")),
            agent_id="alice",
            holding_account_id="brokerage",
            to_account_id="checking",
            tax_character=(DistributionTaxSlice(fraction=0.4, issuer_jurisdiction_id="federal_us"),),
        )


def test_scenario_rejects_out_of_horizon_scheduled_transfers() -> None:
    with pytest.raises(ValidationError, match=r"scheduled transfer 'late_transfer'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="bob")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="bob", account_id="checking", balance=10),
            ],
            scheduled_transfers=[
                ScheduledTransfer(
                    month=2,
                    cause_id="late_transfer",
                    from_agent_id="bob",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=5,
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )

    with pytest.raises(ValidationError, match=r"scheduled transfer 'pre_transfer'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="bob")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="bob", account_id="checking", balance=10),
            ],
            scheduled_transfers=[
                ScheduledTransfer(
                    month=-1,
                    cause_id="pre_transfer",
                    from_agent_id="bob",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=5,
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )


def test_scenario_rejects_out_of_horizon_scheduled_obligations() -> None:
    with pytest.raises(ValidationError, match=r"scheduled obligation 'late_obligation'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="vendor")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=10),
                InitialAccountBalance(agent_id="vendor", account_id="checking", balance=0),
            ],
            scheduled_obligations=[
                ScheduledObligation(
                    month=2,
                    obligation_id="late_obligation",
                    obligation_type="cash_spend",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="vendor",
                    to_account_id="checking",
                    amount_due=5,
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )

    with pytest.raises(ValidationError, match=r"scheduled obligation 'pre_obligation'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="vendor")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=10),
                InitialAccountBalance(agent_id="vendor", account_id="checking", balance=0),
            ],
            scheduled_obligations=[
                ScheduledObligation(
                    month=-1,
                    obligation_id="pre_obligation",
                    obligation_type="cash_spend",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="vendor",
                    to_account_id="checking",
                    amount_due=5,
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )


def test_scenario_rejects_reversed_recurring_windows() -> None:
    with pytest.raises(ValidationError, match=r"recurring transfer 'bad_recurring_transfer'.*before start_month 3"):
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="bob")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=0),
                InitialAccountBalance(agent_id="bob", account_id="checking", balance=10),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=3,
                    end_month=2,
                    cause_id="bad_recurring_transfer",
                    from_agent_id="bob",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=5,
                )
            ],
            tax_profiles=[],
            horizon_months=4,
        )

    with pytest.raises(ValidationError, match=r"recurring obligation 'bad_recurring_obligation'.*before start_month 3"):
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="vendor")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance=10),
                InitialAccountBalance(agent_id="vendor", account_id="checking", balance=0),
            ],
            recurring_obligations=[
                RecurringObligation(
                    start_month=3,
                    end_month=2,
                    obligation_id="bad_recurring_obligation",
                    obligation_type="cash_spend",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="vendor",
                    to_account_id="checking",
                    amount_due=5,
                )
            ],
            tax_profiles=[],
            horizon_months=4,
        )


def test_scenario_allows_noop_recurring_windows_outside_horizon() -> None:
    Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="bob"), Agent(agent_id="vendor")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=10),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance=10),
            InitialAccountBalance(agent_id="vendor", account_id="checking", balance=0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=2,
                end_month=3,
                cause_id="future_transfer",
                from_agent_id="bob",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount=5,
            )
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=2,
                end_month=3,
                obligation_id="future_obligation",
                obligation_type="cash_spend",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="vendor",
                to_account_id="checking",
                amount_due=5,
            )
        ],
        tax_profiles=[],
        horizon_months=2,
    )


def _property_lifecycle_validation_scenario(
    *, property_lifecycle_events: list[SetRentedFractionEvent | PropertySaleEvent], horizon_months: int = 3
) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="seller")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=600000),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance=0),
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=0,
                cause_id="alice_buys_home",
                property_id="home",
                location_id="san_francisco",
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price=500000,
                down_payment=500000,
            )
        ],
        property_lifecycle_events=property_lifecycle_events,
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def test_scenario_rejects_out_of_horizon_property_lifecycle_events() -> None:
    with pytest.raises(ValidationError, match=r"property lifecycle event for 'home'.*outside scenario horizon"):
        _property_lifecycle_validation_scenario(
            property_lifecycle_events=[SetRentedFractionEvent(month=2, property_id="home", rented_fraction=1.0)],
            horizon_months=2,
        )


def test_scenario_rejects_lifecycle_events_for_unknown_property() -> None:
    with pytest.raises(ValidationError, match=r"unknown property_id 'other'; known: 'home'"):
        _property_lifecycle_validation_scenario(
            property_lifecycle_events=[SetRentedFractionEvent(month=1, property_id="other", rented_fraction=1.0)]
        )


def test_scenario_rejects_lifecycle_events_at_or_before_purchase_month() -> None:
    with pytest.raises(ValidationError, match=r"purchase month is 0.*strictly after purchase"):
        _property_lifecycle_validation_scenario(
            property_lifecycle_events=[SetRentedFractionEvent(month=0, property_id="home", rented_fraction=1.0)]
        )


def test_scenario_rejects_multiple_property_sale_lifecycle_events() -> None:
    with pytest.raises(ValidationError, match=r"multiple property sale lifecycle events for 'home': months 1 and 2"):
        _property_lifecycle_validation_scenario(
            property_lifecycle_events=[
                PropertySaleEvent(month=1, property_id="home", closing_cost_pct=6.0),
                PropertySaleEvent(month=2, property_id="home", closing_cost_pct=6.0),
            ]
        )


def test_scenario_rejects_lifecycle_events_after_property_sale() -> None:
    with pytest.raises(ValidationError, match=r"event for 'home' at month 2 fires after sale at month 1"):
        _property_lifecycle_validation_scenario(
            property_lifecycle_events=[
                PropertySaleEvent(month=1, property_id="home", closing_cost_pct=6.0),
                SetRentedFractionEvent(month=2, property_id="home", rented_fraction=1.0),
            ]
        )


def _primary_residence_validation_scenario(
    *,
    initial_primary_residences: list[PrimaryResidenceAssignment] | None = None,
    primary_residence_events: list[SetPrimaryResidenceEvent] | None = None,
    property_lifecycle_events: list[PropertySaleEvent] | None = None,
    purchase_month: int = 0,
    horizon_months: int = 4,
) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="seller")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=600000),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance=0),
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=purchase_month,
                cause_id="alice_buys_home",
                property_id="home",
                location_id="san_francisco",
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price=500000,
                down_payment=500000,
            )
        ],
        initial_primary_residences=initial_primary_residences or [],
        primary_residence_events=primary_residence_events or [],
        property_lifecycle_events=property_lifecycle_events or [],
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def test_scenario_rejects_multiple_initial_primary_residences_for_one_agent() -> None:
    with pytest.raises(ValidationError, match=r"multiple initial primary residences for agent_id 'alice'"):
        _primary_residence_validation_scenario(
            initial_primary_residences=[
                PrimaryResidenceAssignment(agent_id="alice", property_id="home"),
                PrimaryResidenceAssignment(agent_id="alice", property_id="home"),
            ]
        )


def test_scenario_rejects_primary_residence_assignment_before_purchase() -> None:
    with pytest.raises(ValidationError, match=r"before its purchase month 2"):
        _primary_residence_validation_scenario(
            primary_residence_events=[SetPrimaryResidenceEvent(month=1, agent_id="alice", property_id="home")],
            purchase_month=2,
        )


def test_scenario_allows_primary_residence_assignment_at_sale_month() -> None:
    _primary_residence_validation_scenario(
        primary_residence_events=[SetPrimaryResidenceEvent(month=2, agent_id="alice", property_id="home")],
        property_lifecycle_events=[PropertySaleEvent(month=2, property_id="home", closing_cost_pct=6.0)],
    )


def test_scenario_rejects_primary_residence_assignment_after_sale() -> None:
    with pytest.raises(ValidationError, match=r"after sale at month 2"):
        _primary_residence_validation_scenario(
            primary_residence_events=[SetPrimaryResidenceEvent(month=3, agent_id="alice", property_id="home")],
            property_lifecycle_events=[PropertySaleEvent(month=2, property_id="home", closing_cost_pct=6.0)],
        )


if __name__ == "__main__":
    pytest_bazel.main()
