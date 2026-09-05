"""Rust/JAX differential coverage for target-allocation liquidity sales, post-settlement
purchases, and quiet-band drift rebalancing.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

import json
from decimal import Decimal

import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import SecurityDistributionKey, SecuritySymbol
from finance.augur.rust import simulator
from finance.augur.rust.case_fixture import fixture_for
from finance.augur.rust.differential.backend import BACKENDS, assert_backends_agree
from finance.augur.sim.scenario import (
    DistributionTaxSlice,
    InitialLot,
    ObligationType,
    RecurringObligation,
    SecurityDistribution,
)
from finance.augur.sim.testing.case import Case, flat, levels
from finance.augur.sim.testing.fixtures import (
    BND,
    VTI,
    allocation_case,
    allocation_lots,
    allocation_policy,
    cash_spend,
    checking,
    flat_sleeve_prices,
    target_allocation_case,
    target_allocation_purchase_case,
)
from finance.augur.sim.testing.simulation_result import Backend, SimulationResult

VTI_DISTRIBUTION = SecurityDistributionKey(symbol=SecuritySymbol("vti"))


def target_allocation_failure_case() -> Case:
    """A rent obligation larger than everything the band can raise."""

    return allocation_case(
        horizon_months=2,
        initial_cash=checking(("alice", Decimal(0)), ("landlord", Decimal(0)), ("irs", Decimal(0))),
        initial_lots=[
            InitialLot(
                lot_id="vti",
                agent_id="alice",
                account_id="brokerage-a",
                asset=VTI,
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit=Decimal(50),
            ),
            InitialLot(
                lot_id="bnd",
                agent_id="alice",
                account_id="brokerage-b",
                asset=BND,
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit=Decimal(100),
            ),
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=1,
                end_month=1,
                obligation_id="rent",
                obligation_type=ObligationType.OUTSIDE_RENT,
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due=Decimal(50_000),
            )
        ],
        tax_profiles=[],
    )


PURCHASE_ACCOUNTS = (("alice", Decimal(100_000)), ("landlord", Decimal(0)), ("irs", Decimal(0)))


def target_allocation_purchase_then_sale_case() -> Case:
    """A bought lot that must sell only after the real lots ahead of it in FIFO rank."""

    return allocation_case(
        horizon_months=2,
        initial_cash=checking(*PURCHASE_ACCOUNTS),
        initial_lots=allocation_lots(bulk_lot_id="zz-real-same-month", older_lot_account_id="brokerage-b"),
        policy=allocation_policy(
            source_account_ids=("brokerage-b",), cash_ceiling=Decimal(20_000), purchase_slots_per_sleeve=1
        ),
        recurring_obligations=[
            RecurringObligation(
                start_month=1,
                end_month=1,
                obligation_id="rent",
                obligation_type=ObligationType.OUTSIDE_RENT,
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due=Decimal(175_000),
            )
        ],
    )


def target_allocation_purchase_distribution_case() -> Case:
    """A purchase slot that later earns the distribution its bought units qualify for."""

    return allocation_case(
        horizon_months=2,
        initial_cash=checking(*PURCHASE_ACCOUNTS),
        initial_lots=allocation_lots(older_lot_account_id="brokerage-b"),
        policy=allocation_policy(cash_ceiling=Decimal(20_000), purchase_slots_per_sleeve=1),
        security_distributions=[
            SecurityDistribution(
                asset=VTI,
                agent_id="alice",
                holding_account_id="brokerage-a",
                to_account_id="checking",
                tax_character=(DistributionTaxSlice(fraction=1.0),),
            )
        ],
        series={
            **flat_sleeve_prices(horizon_months=2),
            VTI_DISTRIBUTION: flat(Decimal(1), rollout_count=1, horizon_months=2),
        },
    )


def target_allocation_rebalance_case(*, rebalance_tolerance: float = 0.25) -> Case:
    """Cash inside the band but the sleeves drifted past the quiet-band tolerance."""

    return allocation_case(
        horizon_months=2,
        initial_cash=checking(("alice", Decimal(50_000)), ("landlord", Decimal(0)), ("irs", Decimal(0))),
        policy=allocation_policy(
            cash_ceiling=Decimal(90_000), purchase_slots_per_sleeve=1, rebalance_tolerance=rebalance_tolerance
        ),
    )


def monthly_income_case(*, purchase_slots: int, rising_bond_price: bool) -> Case:
    """Three months of income into a policy with a tight ceiling, so it buys every month."""

    return allocation_case(
        horizon_months=3,
        initial_cash=checking(("alice", Decimal(0)), ("landlord", Decimal(90_000)), ("irs", Decimal(0))),
        policy=allocation_policy(
            cash_floor=Decimal(0), cash_ceiling=Decimal(10_000), purchase_slots_per_sleeve=purchase_slots
        ),
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                end_month=2,
                obligation_id="income",
                obligation_type=ObligationType.CASH_SPEND,
                agent_id="landlord",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_due=Decimal(30_000),
            )
        ],
        series={
            VTI: flat(Decimal(100), rollout_count=1, horizon_months=3),
            BND: (
                levels([[Decimal(100), Decimal(200), Decimal(300), Decimal(300)]])
                if rising_bond_price
                else flat(Decimal(100), rollout_count=1, horizon_months=3)
            ),
        },
    )


def _cash(result: SimulationResult, agent_id: str, month: int) -> int:
    row = result.cash.filter(
        (pl.col("agent_id") == agent_id) & (pl.col("account_id") == "checking") & (pl.col("month_index") == month)
    )
    return int(row.get_column("balance_quanta").item())


def test_backends_agree_on_liquidity_sales_before_obligation_funding() -> None:
    """The band raises cash before the grouped funding check, in source-account order."""

    result = assert_backends_agree(target_allocation_case())
    dispositions = result.events.lot_dispositions.sort("source_account_id")

    # Source-account order decides which lots are reached, not lot id.
    assert dispositions.get_column("source_account_id").to_list() == ["brokerage-a", "brokerage-b"]
    assert dispositions.get_column("lot_id").to_list() == ["z-source-first", "a-source-second"]
    assert dispositions.get_column("units_sold").to_list() == [100.0, 130.0]
    assert result.events.obligation_settlements.get_column("attempted_funding_sources").unique().to_list() == [
        "security:vti,security:bnd"
    ]
    assert result.rollout_status.get_column("status").to_list() == ["active"]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


def test_backends_agree_on_post_settlement_purchases() -> None:
    """Buys are decided pre-settlement and execute after, clamped to then-current cash."""

    result = assert_backends_agree(target_allocation_purchase_case())
    bought = result.lots.filter(
        (pl.col("month_index") == 1) & pl.col("lot_id").str.starts_with("allocation_sale_buy")
    ).sort("lot_id")

    assert bought.get_column("asset_id").to_list() == ["security:vti", "security:bnd"]
    assert bought.get_column("remaining_quantity_quanta").to_list() == [50_000_000, 850_000_000]
    assert bought.get_column("cost_basis_per_unit_quanta").unique().to_list() == [10_000]
    assert bought.get_column("account_id").unique().to_list() == ["brokerage-a"]
    assert _cash(result, "alice", 1) == 1_000_000


def test_backends_agree_on_quiet_band_drift_rebalancing() -> None:
    """Rebalancing is all-or-nothing and returns every sleeve to its floored target."""

    result = assert_backends_agree(target_allocation_rebalance_case())
    remaining = {
        row["lot_id"]: row["remaining_quantity_quanta"]
        for row in result.lots.filter(pl.col("month_index") == 1).to_dicts()
    }

    assert remaining == {
        "a-source-second": 500_000_000,
        "allocation_sale_buy_p0_s0_0": 0,
        "allocation_sale_buy_p0_s1_0": 400_000_000,
        "bond": 100_000_000,
        "z-source-first": 0,
    }
    assert _cash(result, "alice", 1) == 5_000_000
    sold = result.events.lot_dispositions.sort("lot_id")
    assert sold.get_column("lot_id").to_list() == ["a-source-second", "z-source-first"]
    assert sold.get_column("units_sold").to_list() == [300.0, 100.0]


def test_backends_agree_that_purchased_lots_join_the_pool_after_real_lots() -> None:
    """A bought lot sells only once the real lots ahead of it in FIFO rank are exhausted."""

    result = assert_backends_agree(target_allocation_purchase_then_sale_case())
    sold = result.events.lot_dispositions.filter((pl.col("month_index") == 1) & (pl.col("asset_id") == "security:vti"))

    assert {row["lot_id"]: row["units_sold"] for row in sold.to_dicts()} == {
        "allocation_sale_buy_p0_s0_0": 25.0,
        "z-source-first": 100.0,
        "zz-real-same-month": 800.0,
    }


def test_backends_agree_that_a_purchase_slot_pool_receives_later_distributions() -> None:
    result = assert_backends_agree(target_allocation_purchase_distribution_case())
    slot = result.lots.filter(
        (pl.col("month_index") == 1) & (pl.col("lot_id") == "allocation_sale_buy_p0_s0_0")
    ).to_dicts()[0]

    assert slot["account_id"] == "brokerage-a"
    assert slot["remaining_quantity_quanta"] == 50_000_000
    # Nothing is held at the first distribution; the bought units earn the second.
    assert result.distributions.sort("month_index").get_column("amount_quanta").to_list() == [0, 5_000]


def test_backends_agree_that_a_failed_settlement_suppresses_decided_purchases() -> None:
    """Buys decided before settlement must not execute once the settlement fails."""

    result = assert_backends_agree(
        allocation_case(
            horizon_months=2,
            initial_cash=checking(*PURCHASE_ACCOUNTS),
            policy=allocation_policy(cash_ceiling=Decimal(20_000), purchase_slots_per_sleeve=1),
            scheduled_obligations=[
                cash_spend(
                    "unfundable", month=0, agent_id="alice", to_agent_id="landlord", amount_due=Decimal(100_000_000)
                )
            ],
        )
    )

    assert result.rollout_status.get_column("failed_month").to_list() == [0]
    assert not any(cause.startswith("allocation_sale_buy_") for cause in result.journal.get_column("cause_id"))
    frozen = result.lots.filter(pl.col("month_index") == 1)
    assert frozen.get_column("remaining_quantity_quanta").unique().to_list() == [0]


def test_backends_agree_that_successive_purchases_keep_distinct_months_and_prices() -> None:
    """Each month's buy fills its own slot at the price that month's rollout observed."""

    result = assert_backends_agree(monthly_income_case(purchase_slots=2, rising_bond_price=True))
    slots = {
        row["lot_id"]: row
        for row in result.lots.filter((pl.col("month_index") == 3) & pl.col("lot_id").str.contains("_s1_")).to_dicts()
    }

    first, second = slots["allocation_sale_buy_p0_s1_0"], slots["allocation_sale_buy_p0_s1_1"]
    assert (first["purchase_month_index"], first["cost_basis_per_unit_quanta"]) == (1, 20_000)
    assert (second["purchase_month_index"], second["cost_basis_per_unit_quanta"]) == (2, 30_000)


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_every_backend_aborts_when_purchase_slots_are_exhausted(backend: Backend) -> None:
    """A dropped purchase would be a silent wrong answer, so both engines refuse instead."""

    with pytest.raises(ValueError, match="ran out of purchase slots: 1 configured, 2 needed"):
        backend(monthly_income_case(purchase_slots=1, rising_bond_price=False))


def test_the_rust_validator_refuses_rebalancing_without_purchase_slots() -> None:
    """A rebalance with nowhere to buy back into would only ever sell, draining the portfolio.

    `TargetAllocationPolicy` refuses the combination outright, so no authored case can carry
    it to the Rust validator's own check; the document is built by dropping the slots from an
    encoded one. The Python half of the rule is `sim/test_target_allocation_e2e.py`.
    """

    fixture = fixture_for(target_allocation_rebalance_case())
    fixture["scenario"]["target_allocation_policies"][0]["purchase_slots_per_sleeve"] = 0

    with pytest.raises(ValueError, match="invalid configuration"):
        simulator.simulate_forensic_json(json.dumps(fixture))


def test_backends_agree_on_insufficient_funding_failure_metadata() -> None:
    result = assert_backends_agree(target_allocation_failure_case())

    assert result.events.rollout_failures.to_dicts() == [
        {
            "rollout_index": 0,
            "month_index": 1,
            "cause_id": "rent_m1_failure",
            "agent_id": "alice",
            "deficit_quanta": 5_000_000,
            "obligation_id": "rent_m1",
            "obligation_type": "outside_rent",
            "amount_due_quanta": 5_000_000,
            "amount_paid_quanta": 0,
            "shortfall_quanta": 5_000_000,
            "attempted_funding_sources": "security:vti,security:bnd",
        }
    ]
    assert result.rollout_status.get_column("failed_month").to_list() == [1]
    assert result.journal.get_column("imbalance_quanta").unique().to_list() == [0]


if __name__ == "__main__":
    pytest_bazel.main()
