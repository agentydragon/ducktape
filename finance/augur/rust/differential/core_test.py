"""Rust/JAX differential coverage for opening balances, transfers, amount schedules,
grouped obligations, failure freezing, and the encoded fixture.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from dataclasses import replace
from decimal import Decimal
from typing import Any

import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey, LocationId, RentKey
from finance.augur.rust.differential.backend import assert_backends_agree, run_rust
from finance.augur.rust.differential.fixture import fixture_for
from finance.augur.rust.differential.fixtures import VTI, cash_spend, checking, failure_case, shared_case, transfer
from finance.augur.rust.fixture_encoder import UnsupportedScenarioError
from finance.augur.sim.locations import Location
from finance.augur.sim.scenario import (
    FixedAmount,
    InitialLot,
    ObligationType,
    RecurringObligation,
    RecurringPropertyCashflow,
    RecurringTransfer,
    ScheduledPropertyCashflow,
    ScheduledPropertyPurchase,
    SeriesIndexedAmount,
)
from finance.augur.sim.testing.case import Case, flat, levels, scenario

TEST_INFLATION = InflationKey()
TEST_RENT = RentKey(location_id=LocationId("test"))

# A place with no property tax, so the property below exists only to gate the cashflows
# that name it.
UNTAXED_LOCATION = Location(
    location_id="test",
    display_name="Test",
    jurisdiction_ids=[],
    annual_property_tax_rate=0.0,
    annual_special_assessment=Decimal(0),
)


def recurring_obligation_case() -> Case:
    """Two obligations sharing a payer and source account, one of which is unaffordable."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(1_000)), ("landlord", Decimal(0)), ("utility", Decimal(0))),
            horizon_months=3,
            tax_profiles=[],
            recurring_obligations=[
                RecurringObligation(
                    start_month=0,
                    end_month=2,
                    obligation_id="rent",
                    obligation_type=ObligationType.CASH_SPEND,
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="landlord",
                    to_account_id="checking",
                    amount_due=Decimal(600),
                ),
                RecurringObligation(
                    start_month=1,
                    end_month=2,
                    obligation_id="utility",
                    obligation_type=ObligationType.CASH_SPEND,
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="utility",
                    to_account_id="checking",
                    amount_due=Decimal("0.01"),
                ),
            ],
        ),
        rollout_count=2,
    )


def series_indexed_amount_case() -> Case:
    """Scalar, tagged-fixed and index-scaled amounts on every flow that carries one.

    The two rollouts move the levels in opposite directions and the rent-indexed amounts use a
    12-month adjustment period, so the periodic reset boundary lands inside the horizon.
    """

    inflation_indexed = SeriesIndexedAmount(base_amount=Decimal("1.01"), series=TEST_INFLATION)
    annual_rent_indexed = SeriesIndexedAmount(
        base_amount=Decimal("10.01"), series=TEST_RENT, adjustment_period_months=12
    )
    return Case(
        scenario=scenario(
            checking(
                ("alice", Decimal(200_000)),
                ("bob", Decimal(200_000)),
                ("seller", Decimal(0)),
                ("tenant", Decimal(200_000)),
                ("landlord", Decimal(0)),
                ("manager", Decimal(0)),
            ),
            horizon_months=14,
            tax_profiles=[],
            scheduled_transfers=[
                transfer("indexed-gift", month=2, from_agent_id="bob", to_agent_id="alice", amount=inflation_indexed),
                transfer(
                    "tagged-fixed-gift",
                    month=3,
                    from_agent_id="bob",
                    to_agent_id="alice",
                    amount=FixedAmount(amount=Decimal("-0.17")),
                ),
                transfer("zero-gift", month=4, from_agent_id="bob", to_agent_id="alice", amount=Decimal(0)),
            ],
            recurring_transfers=[
                RecurringTransfer(
                    start_month=0,
                    end_month=13,
                    cause_id="annual-indexed-paycheck",
                    from_agent_id="bob",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=annual_rent_indexed,
                )
            ],
            scheduled_obligations=[
                cash_spend(
                    "indexed-bill", month=2, agent_id="alice", to_agent_id="landlord", amount_due=inflation_indexed
                )
            ],
            recurring_obligations=[
                RecurringObligation(
                    start_month=0,
                    end_month=13,
                    obligation_id="indexed-rent",
                    obligation_type=ObligationType.CASH_SPEND,
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="landlord",
                    to_account_id="checking",
                    amount_due=annual_rent_indexed,
                )
            ],
            scheduled_property_purchases=[
                ScheduledPropertyPurchase(
                    month=0,
                    cause_id="buy-test-home",
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
                    month=2,
                    property_id="home",
                    cause_id="indexed-repair",
                    from_agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="manager",
                    to_account_id="checking",
                    amount=inflation_indexed,
                )
            ],
            recurring_property_cashflows=[
                RecurringPropertyCashflow(
                    start_month=0,
                    end_month=13,
                    property_id="home",
                    cause_id="indexed-property-rent",
                    from_agent_id="tenant",
                    from_account_id="checking",
                    to_agent_id="alice",
                    to_account_id="checking",
                    amount=annual_rent_indexed,
                )
            ],
        ),
        rollout_count=2,
        locations={"test": UNTAXED_LOCATION},
        series={
            TEST_INFLATION: levels(
                [
                    [Decimal(1), Decimal("1.25"), *([Decimal("1.5")] * 13)],
                    [Decimal(1), Decimal("1.5"), *([Decimal("1.25")] * 13)],
                ]
            ),
            TEST_RENT: levels(
                [[*([Decimal(1)] * 12), *([Decimal("1.1")] * 3)], [*([Decimal(1)] * 12), *([Decimal("1.25")] * 3)]]
            ),
        },
    )


def test_backends_agree_on_the_shared_case() -> None:
    """Opening balances, transfers, a FIFO sale, and the events each produces."""

    result = assert_backends_agree(shared_case())

    # The sale consumes one of the two units the lot opened with.
    assert result.lots.filter(pl.col("month_index") == 3).get_column("remaining_quantity_quanta").to_list() == [
        1_000_000,
        1_000_000,
    ]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_failure_freeze_semantics() -> None:
    """A rollout that cannot fund an obligation stops and reports zero value thereafter."""

    result = assert_backends_agree(failure_case())

    assert result.rollout_status.to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 0},
        {"rollout_index": 1, "status": "failed_insufficient_cash", "failed_month": 0},
    ]
    frozen = result.cash.filter(pl.col("month_index") > 0)
    assert frozen.get_column("balance_quanta").unique().to_list() == [0]


def test_backends_agree_on_grouped_recurring_obligations() -> None:
    """Obligations sharing a payer and source account settle all-or-none."""

    result = assert_backends_agree(recurring_obligation_case())

    assert result.rollout_status.to_dicts() == [
        {"rollout_index": 0, "status": "failed_insufficient_cash", "failed_month": 1},
        {"rollout_index": 1, "status": "failed_insufficient_cash", "failed_month": 1},
    ]


def test_backends_agree_on_fixed_and_series_indexed_amounts() -> None:
    """Scalar, tagged-fixed and index-scaled amounts, including periodic reset boundaries."""

    result = assert_backends_agree(series_indexed_amount_case())

    due = result.events.obligation_settlements.sort("rollout_index")
    assert due.filter(pl.col("obligation_id") == "indexed-bill_m2").get_column("amount_due_quanta").to_list() == [
        152,
        126,
    ]
    # A 12-month adjustment period holds the rent flat through month 11 and resets at 12.
    assert due.filter(pl.col("obligation_id") == "indexed-rent_m12").get_column("amount_due_quanta").to_list() == [
        1_101,
        1_251,
    ]
    assert result.rollout_status.get_column("status").unique().to_list() == ["active"]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_the_encoding_refuses_a_lot_whose_total_basis_is_not_whole_quanta() -> None:
    """Rust stores a lot's total basis where the scenario states a per-unit one.

    Only the encoding can catch this: the JAX engine holds the per-unit basis and would
    round the product itself, so a lot whose units and per-unit basis do not multiply out to
    whole quanta is refused at the boundary rather than costing a different amount per side.
    """

    case = Case(
        scenario=scenario(
            checking(("alice", Decimal(0))),
            horizon_months=1,
            tax_profiles=[],
            initial_lots=[
                InitialLot(
                    lot_id="fractional",
                    agent_id="alice",
                    account_id="checking",
                    asset=VTI,
                    purchase_month_index=-12,
                    quantity=0.000003,
                    cost_basis_per_unit=Decimal("0.01"),
                )
            ],
        ),
        rollout_count=1,
        series={VTI: flat(Decimal(100), rollout_count=1, horizon_months=1)},
    )

    with pytest.raises(UnsupportedScenarioError, match="whole number of currency quanta"):
        run_rust(case)


@pytest.mark.parametrize("rollout_count", [1, 17])
def test_the_encoded_fixture_contains_no_floating_point_numbers(rollout_count: int) -> None:
    """The fixture is what crosses to Rust, so a float in it is an integer the encoder lost."""

    case = replace(
        shared_case(),
        rollout_count=rollout_count,
        series={VTI: flat(Decimal(150), rollout_count=rollout_count, horizon_months=3)},
    )

    def walk(value: Any) -> None:
        assert not isinstance(value, float)
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(fixture_for(case))


if __name__ == "__main__":
    pytest_bazel.main()
