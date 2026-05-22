"""End-to-end tests for the spike-1 simulator.

L1 baseline: Alice gives Bob $5, single rollout, one-month horizon.
The simulator advances state via the apply_events pipeline, records
the transfer on the event log, and produces a long-form
state-over-time frame that satisfies the conservation invariant.
"""

from __future__ import annotations

import polars as pl
import pytest
import pytest_bazel
from pydantic import ValidationError

from augur.model.gbm import GeometricBrownian
from augur.model.series_model import SeriesModelBundle
from augur.sim.external_series import EXTERNAL_SERIES_VALUES_FRAME, ExternalSeriesContext
from augur.sim.replay import assert_replay_invariant_holds
from augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    InitialLot,
    LiquidityPolicy,
    MortgageFinancing,
    PropertyTaxPolicy,
    RecurringObligation,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledObligation,
    ScheduledPropertyPurchase,
    ScheduledTransfer,
    SeriesIndexedAmount,
    TaxProfile,
)
from augur.sim.simulate import simulate, simulate_with_external_series


def _external_series_context_for_levels(series_id: str, levels_by_rollout: list[list[float]]) -> ExternalSeriesContext:
    return ExternalSeriesContext(
        series_values=EXTERNAL_SERIES_VALUES_FRAME.normalize(
            pl.DataFrame(
                [
                    {"rollout_index": rollout_index, "month_index": month_index, "series_id": series_id, "value": level}
                    for rollout_index, levels in enumerate(levels_by_rollout)
                    for month_index, level in enumerate(levels)
                ]
            )
        )
    )


def _alice_bob_scenario() -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="bob")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=10.0),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance_usd=20.0),
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=0,
                cause_id="bob_gives_alice_5",
                from_agent_id="bob",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=5.0,
            )
        ],
        tax_profiles=[],
        horizon_months=1,
    )


def test_series_indexed_amount_parses_from_scenario_data() -> None:
    scenario = Scenario.model_validate(
        {
            "agents": [{"agent_id": "alice"}, {"agent_id": "landlord"}],
            "initial_cash": [
                {"agent_id": "alice", "account_id": "checking", "balance_usd": 10_000.0},
                {"agent_id": "landlord", "account_id": "checking", "balance_usd": 0.0},
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
                    "amount_due_usd": {
                        "kind": "series_indexed",
                        "base_amount_usd": 1_000.0,
                        "series_id": "rent:san_francisco_ca",
                        "base_month_index": 0,
                        "adjustment_period_months": 12,
                    },
                }
            ],
            "tax_profiles": [],
            "horizon_months": 13,
        }
    )

    amount = scenario.recurring_obligations[0].amount_due_usd
    assert isinstance(amount, SeriesIndexedAmount)
    assert amount.series_id == "rent:san_francisco_ca"


def test_series_indexed_amount_cannot_fire_before_base_month() -> None:
    rent_series_id = "rent:san_francisco_ca"
    scenario = _series_indexed_rent_obligation_scenario(
        SeriesIndexedAmount(
            base_amount_usd=1_000.0, series_id=rent_series_id, base_month_index=1, adjustment_period_months=12
        ),
        horizon_months=2,
    )
    external_series = _external_series_context_for_levels(rent_series_id, levels_by_rollout=[[100.0, 110.0, 120.0]])

    with pytest.raises(ValueError, match="before base month 1"):
        simulate_with_external_series(scenario, rollout_count=1, external_series=external_series)


def test_series_indexed_amount_requires_external_series_coverage() -> None:
    rent_series_id = "rent:san_francisco_ca"
    scenario = _series_indexed_rent_obligation_scenario(
        SeriesIndexedAmount(
            base_amount_usd=1_000.0, series_id=rent_series_id, base_month_index=0, adjustment_period_months=12
        ),
        horizon_months=13,
    )
    external_series = _external_series_context_for_levels(rent_series_id, levels_by_rollout=[[100.0] * 12])

    with pytest.raises(KeyError, match="missing rollout"):
        simulate_with_external_series(scenario, rollout_count=1, external_series=external_series)


def test_series_indexed_amount_rejects_zero_base_level() -> None:
    rent_series_id = "rent:san_francisco_ca"
    scenario = _series_indexed_rent_obligation_scenario(
        SeriesIndexedAmount(
            base_amount_usd=1_000.0, series_id=rent_series_id, base_month_index=0, adjustment_period_months=12
        ),
        horizon_months=1,
    )
    external_series = _external_series_context_for_levels(rent_series_id, levels_by_rollout=[[0.0, 100.0]])

    with pytest.raises(ValueError, match="zero base level"):
        simulate_with_external_series(scenario, rollout_count=1, external_series=external_series)


def _series_indexed_rent_obligation_scenario(amount: SeriesIndexedAmount, *, horizon_months: int) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=20_000.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                obligation_id="outside_rent",
                obligation_type="outside_rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=amount,
            )
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def test_scenario_requires_explicit_tax_profiles() -> None:
    with pytest.raises(ValidationError, match="tax_profiles"):
        Scenario.model_validate(
            {
                "agents": [{"agent_id": "alice"}],
                "initial_cash": [{"agent_id": "alice", "account_id": "checking", "balance_usd": 100.0}],
                "horizon_months": 1,
            }
        )


def test_scenario_rejects_duplicate_liquidity_policy_accounts() -> None:
    with pytest.raises(ValidationError, match=r"duplicate liquidity policies.*alice/checking"):
        Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0)],
            liquidity_policies=[
                LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=["vti"]),
                LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=["qqq"]),
            ],
            tax_profiles=[],
            horizon_months=1,
        )


def test_scenario_rejects_duplicate_lot_purchase_months_within_fifo_pool() -> None:
    with pytest.raises(ValidationError, match=r"duplicate initial lot purchase months.*alice/vti@-12.*old_a.*old_b"):
        Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
            initial_lots=[
                InitialLot(
                    lot_id="old_a",
                    agent_id="alice",
                    asset_id="vti",
                    purchase_month_index=-12,
                    quantity=10.0,
                    cost_basis_per_unit_usd=80.0,
                ),
                InitialLot(
                    lot_id="old_b",
                    agent_id="alice",
                    asset_id="vti",
                    purchase_month_index=-12,
                    quantity=5.0,
                    cost_basis_per_unit_usd=90.0,
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
        "amount_usd": 100.0,
        "income_category": "gift",
    }
    recurring_data = {
        "start_month": 0,
        "cause_id": "gift",
        "from_agent_id": "bob",
        "from_account_id": "checking",
        "to_agent_id": "alice",
        "to_account_id": "checking",
        "amount_usd": 100.0,
        "income_category": "gift",
    }

    with pytest.raises(ValidationError, match=r"Input should be 'ordinary'"):
        ScheduledTransfer.model_validate(scheduled_data)
    with pytest.raises(ValidationError, match=r"Input should be 'ordinary'"):
        RecurringTransfer.model_validate(recurring_data)


def test_scenario_rejects_out_of_horizon_scheduled_asset_sales() -> None:
    with pytest.raises(ValidationError, match=r"scheduled asset sale 'late_sale'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
            initial_lots=[
                InitialLot(
                    lot_id="seed",
                    agent_id="alice",
                    asset_id="vti",
                    purchase_month_index=0,
                    quantity=1.0,
                    cost_basis_per_unit_usd=100.0,
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=2,
                    cause_id="late_sale",
                    agent_id="alice",
                    asset_id="vti",
                    quantity=1.0,
                    proceeds_account_id="checking",
                    price_per_unit_usd=100.0,
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )

    with pytest.raises(ValidationError, match=r"scheduled asset sale 'pre_sale'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice")],
            initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=-1,
                    cause_id="pre_sale",
                    agent_id="alice",
                    asset_id="vti",
                    quantity=1.0,
                    proceeds_account_id="checking",
                    price_per_unit_usd=100.0,
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
                InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100_000.0),
                InitialAccountBalance(agent_id="seller", account_id="checking", balance_usd=0.0),
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
                    purchase_price_usd=500_000.0,
                    down_payment_usd=100_000.0,
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )

    with pytest.raises(ValidationError, match=r"scheduled property purchase 'pre_purchase'.*outside scenario horizon"):
        Scenario(
            agents=[Agent(agent_id="alice"), Agent(agent_id="seller")],
            initial_cash=[
                InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100_000.0),
                InitialAccountBalance(agent_id="seller", account_id="checking", balance_usd=0.0),
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
                    purchase_price_usd=500_000.0,
                    down_payment_usd=100_000.0,
                )
            ],
            tax_profiles=[],
            horizon_months=2,
        )


def test_alice_gives_bob_five_dollars_one_rollout() -> None:
    """One scheduled transfer at month 0 moves $5 from Bob to Alice.
    After month 0: Alice $15, Bob $15. The transfer is on the log;
    the post-step cross-section reflects it; total cash in the
    system is conserved at every month."""
    result = simulate(_alice_bob_scenario(), rollout_count=1)

    initial = result.cash_balances.filter(pl.col("month_index") == 0).sort("agent_id")
    assert initial.get_column("balance_usd").to_list() == [10.0, 20.0]

    post = result.cash_balances.filter(pl.col("month_index") == 1).sort("agent_id")
    assert post.get_column("balance_usd").to_list() == [15.0, 15.0]

    # Conservation invariant: total cash unchanged at every month.
    totals = (
        result.cash_balances.group_by("month_index").agg(pl.col("balance_usd").sum().alias("total")).sort("month_index")
    )
    assert totals.get_column("total").to_list() == [30.0, 30.0]

    # The transfer is on the log.
    assert result.events_log.transfers.height == 1
    txn = result.events_log.transfers.row(0, named=True)
    assert txn["from_agent_id"] == "bob"
    assert txn["to_agent_id"] == "alice"
    assert txn["amount_usd"] == 5.0
    assert txn["month_index"] == 0


def test_apply_events_is_only_mutation_replays_from_log() -> None:
    """Smoke test for the replay-invariant helper on the L1 baseline
    scenario. The helper checks every state frame the engine touches;
    most scenario tests below also invoke it after their assertions to
    extend the invariant coverage cheaply."""
    scenario = _alice_bob_scenario()
    result = simulate(scenario, rollout_count=1)
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_no_scheduled_transfers_leaves_balances_unchanged() -> None:
    """Multi-month horizon with no events should carry initial cash
    forward unchanged. Exercises the empty-event-log path through
    the loop."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0)],
        tax_profiles=[],
        horizon_months=5,
    )

    result = simulate(scenario, rollout_count=1)

    # Six rows: initial month 0 through end-of-horizon month 5.
    assert result.cash_balances.height == 6
    assert result.cash_balances.get_column("balance_usd").to_list() == [100.0] * 6
    assert result.events_log.transfers.is_empty()


def test_rejects_zero_rollout_count() -> None:
    with pytest.raises(ValueError, match="rollout_count"):
        simulate(_alice_bob_scenario(), rollout_count=0)


def test_recurring_paycheck_accrues_monthly() -> None:
    """Alice receives a $3000 paycheck every month from a payroll
    sink for 12 months. Starting cash $1000; ending cash
    $1000 + 12 × $3000 = $37000. One Transfer event per month on
    the log."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1000.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=3000.0,
            )
        ],
        tax_profiles=[],
        horizon_months=12,
    )

    result = simulate(scenario, rollout_count=1)

    alice_final = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 12))
        .get_column("balance_usd")
        .item()
    )
    assert alice_final == 1000.0 + 12 * 3000.0

    # Conservation: payroll sink goes negative by the same amount.
    payroll_final = (
        result.cash_balances.filter((pl.col("agent_id") == "payroll") & (pl.col("month_index") == 12))
        .get_column("balance_usd")
        .item()
    )
    assert payroll_final == -12 * 3000.0

    # 12 paycheck events on the log (one per month).
    assert result.events_log.transfers.height == 12
    assert set(result.events_log.transfers.get_column("month_index").to_list()) == set(range(12))
    assert set(result.events_log.transfers.get_column("cause_id").unique().to_list()) == {"alice_paycheck"}


def test_recurring_transfer_bounded_by_end_month() -> None:
    """Recurring transfer with end_month=4 fires months 0-4
    (inclusive), then stops. Asserts the end_month bound is
    honored — no events at month 5+."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="sink")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="sink", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=4,
                cause_id="bounded_pay",
                from_agent_id="sink",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=100.0,
            )
        ],
        tax_profiles=[],
        horizon_months=10,
    )

    result = simulate(scenario, rollout_count=1)
    assert result.events_log.transfers.height == 5  # months 0..4

    # Alice's balance plateaus at 500.0 from month 5 onward.
    balances = (
        result.cash_balances.filter(pl.col("agent_id") == "alice")
        .sort("month_index")
        .get_column("balance_usd")
        .to_list()
    )
    assert balances == [0.0, 100.0, 200.0, 300.0, 400.0, 500.0, 500.0, 500.0, 500.0, 500.0, 500.0]


def test_one_thousand_rollouts_identical_when_inputs_are() -> None:
    """L3: scale the rollout dimension to 1000. With deterministic
    inputs (no external path variation, same scenario), every rollout produces
    the same trajectory. Exercises the polars cross-join expansion
    of the rollout column at scale; asserts the engine has no
    Python loop over rollouts (otherwise this would be too slow)."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="employer")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1000.0),
            InitialAccountBalance(agent_id="employer", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="employer",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=2000.0,
            )
        ],
        tax_profiles=[],
        horizon_months=24,
    )
    rollout_count = 1000

    result = simulate(scenario, rollout_count=rollout_count)

    # Every rollout: Alice ends at 1000 + 24×2000 = 49000.
    alice_final = result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 24)).sort(
        "rollout_index"
    )
    assert alice_final.height == rollout_count
    assert alice_final.get_column("balance_usd").to_list() == [49000.0] * rollout_count

    # Event log expands rollouts × months: 1000 × 24 = 24000 events.
    assert result.events_log.transfers.height == rollout_count * 24

    # Conservation at every month, across every rollout.
    totals = (
        result.cash_balances.group_by(["rollout_index", "month_index"])
        .agg(pl.col("balance_usd").sum().alias("total"))
        .sort(["rollout_index", "month_index"])
    )
    assert totals.get_column("total").unique().to_list() == [1000.0]


def test_combined_one_off_and_recurring() -> None:
    """A scenario with both a recurring monthly paycheck and a
    one-off bonus transfer at month 5. Both fire through the same
    Transfer event path; the log shows both. Tests that the step
    emits both kinds in one call."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="employer")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="employer", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="employer",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=1000.0,
            )
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=5,
                cause_id="alice_bonus",
                from_agent_id="employer",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=5000.0,
            )
        ],
        tax_profiles=[],
        horizon_months=10,
    )

    result = simulate(scenario, rollout_count=1)

    # 10 paycheck events + 1 bonus = 11.
    assert result.events_log.transfers.height == 11

    # Alice at end-of-horizon: 10 × $1000 paychecks + $5000 bonus = $15000.
    alice_final = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 10))
        .get_column("balance_usd")
        .item()
    )
    assert alice_final == 15000.0
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_initial_lot_partial_sale_consumes_units_credits_proceeds() -> None:
    """L4 part A — single-lot scenario. Alice has 100 units of VTI
    bought 24 months pre-horizon at $80/unit (so cost basis $8000).
    At month 3 she sells 30 units at $120/unit; proceeds = $3600
    credit to checking. After the sale: lot has 70 units remaining,
    cash up by $3600. One lot_disposition row records the FIFO
    consumption with cost_basis_consumed = 30 × $80 = $2400."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti_seed",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=80.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=3,
                cause_id="alice_partial_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=30.0,
                price_per_unit_usd=120.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[],
        horizon_months=6,
    )

    result = simulate(scenario, rollout_count=1)

    # Pre-sale: month 3 cross-section still has 100 units (apply for
    # month M produces the M+1 cross-section).
    lots_at_m3 = result.asset_lots.filter(pl.col("month_index") == 3)
    assert lots_at_m3.get_column("remaining_quantity").to_list() == [100.0]

    # Post-sale: month 4 onward, 70 units remain.
    for month in (4, 5, 6):
        snapshot = result.asset_lots.filter(pl.col("month_index") == month)
        assert snapshot.get_column("remaining_quantity").to_list() == [70.0]

    # Cash: 0 at month 0..3, then $3600 at month 4 onward.
    cash_trajectory = (
        result.cash_balances.filter(pl.col("agent_id") == "alice")
        .sort("month_index")
        .get_column("balance_usd")
        .to_list()
    )
    assert cash_trajectory == [0.0, 0.0, 0.0, 0.0, 3600.0, 3600.0, 3600.0]

    # Disposition log: one row, with FIFO from the seeded lot.
    assert result.events_log.lot_dispositions.height == 1
    disp = result.events_log.lot_dispositions.row(0, named=True)
    assert disp["lot_id"] == "alice_vti_seed"
    assert disp["cause_id"] == "alice_partial_sale"
    assert disp["month_index"] == 3
    assert disp["purchase_month_index"] == -24
    assert disp["units_sold"] == 30.0
    assert disp["cost_basis_consumed_usd"] == 2400.0
    assert disp["proceeds_usd"] == 3600.0
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_initial_lot_full_sale_zeros_remaining_quantity() -> None:
    """Selling all 100 units exhausts the lot. Remaining quantity
    drops to 0; the lot row persists in the asset_lots frame with
    `remaining_quantity = 0` (lots are not deleted on full
    disposition — they remain in state for historical reference)."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti_seed",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-12,
                quantity=100.0,
                cost_basis_per_unit_usd=90.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=2,
                cause_id="full_liquidation",
                agent_id="alice",
                asset_id="vti",
                quantity=100.0,
                price_per_unit_usd=150.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[],
        horizon_months=3,
    )

    result = simulate(scenario, rollout_count=1)

    remaining_after = result.asset_lots.filter(pl.col("month_index") == 3).get_column("remaining_quantity").item()
    assert remaining_after == 0.0

    assert result.events_log.lot_dispositions.height == 1
    disp = result.events_log.lot_dispositions.row(0, named=True)
    assert disp["units_sold"] == 100.0
    assert disp["proceeds_usd"] == 15000.0
    assert disp["cost_basis_consumed_usd"] == 9000.0


def test_asset_sale_scales_across_rollouts() -> None:
    """The lot frame fans across rollouts identically when inputs
    are deterministic; the disposition resolution is vectorized
    over the rollout dimension."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="seed",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=0,
                quantity=50.0,
                cost_basis_per_unit_usd=100.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=1,
                cause_id="sale",
                agent_id="alice",
                asset_id="vti",
                quantity=20.0,
                price_per_unit_usd=110.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[],
        horizon_months=2,
    )
    rollout_count = 100
    result = simulate(scenario, rollout_count=rollout_count)

    # Every rollout has one disposition.
    assert result.events_log.lot_dispositions.height == rollout_count
    # Every rollout's lot row at end-of-horizon has 30 units remaining.
    end_state = result.asset_lots.filter(pl.col("month_index") == 2)
    assert end_state.height == rollout_count
    assert end_state.get_column("remaining_quantity").unique().to_list() == [30.0]


def test_lot_disposition_replay_invariant() -> None:
    """Replay invariant on an L4-style scenario with lot dispositions.
    Exercises the cap-gains YTD bucket path through the helper."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=50.0)],
        initial_lots=[
            InitialLot(
                lot_id="seed",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-6,
                quantity=40.0,
                cost_basis_per_unit_usd=75.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=1,
                cause_id="partial",
                agent_id="alice",
                asset_id="vti",
                quantity=10.0,
                price_per_unit_usd=200.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[],
        horizon_months=2,
    )
    rollout_count = 3
    result = simulate(scenario, rollout_count=rollout_count)
    assert_replay_invariant_holds(scenario, result, rollout_count=rollout_count)


def test_fifo_sale_crossing_two_lots() -> None:
    """L4 part B — multi-lot FIFO crossing. Alice has two lots of
    VTI: lot A (older, 6 months pre-horizon, 100 units @ $80) and
    lot B (month 2, 50 units @ $100). At month 8 she sells 120
    units at $200/unit; FIFO consumes the full 100 units of lot A
    plus 20 units of lot B. Proceeds = 120 × $200 = $24000."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="lot_a_old",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-6,
                quantity=100.0,
                cost_basis_per_unit_usd=80.0,
            ),
            InitialLot(
                lot_id="lot_b_younger",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=2,
                quantity=50.0,
                cost_basis_per_unit_usd=100.0,
            ),
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=8,
                cause_id="big_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=120.0,
                price_per_unit_usd=200.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[],
        horizon_months=10,
    )

    result = simulate(scenario, rollout_count=1)

    # Two disposition rows for one sale (FIFO crossed two lots).
    assert result.events_log.lot_dispositions.height == 2
    by_lot = {
        row["lot_id"]: row
        for row in result.events_log.lot_dispositions.sort("purchase_month_index").iter_rows(named=True)
    }
    assert by_lot["lot_a_old"]["units_sold"] == 100.0
    assert by_lot["lot_a_old"]["cost_basis_consumed_usd"] == 8000.0
    assert by_lot["lot_a_old"]["proceeds_usd"] == 20000.0
    assert by_lot["lot_b_younger"]["units_sold"] == 20.0
    assert by_lot["lot_b_younger"]["cost_basis_consumed_usd"] == 2000.0
    assert by_lot["lot_b_younger"]["proceeds_usd"] == 4000.0

    # Post-sale lot snapshot: lot A is empty, lot B has 30 units.
    post = (
        result.asset_lots.filter(pl.col("month_index") == 9)
        .sort("lot_id")
        .select("lot_id", "remaining_quantity")
        .to_dicts()
    )
    assert post == [
        {"lot_id": "lot_a_old", "remaining_quantity": 0.0},
        {"lot_id": "lot_b_younger", "remaining_quantity": 30.0},
    ]

    # Cash credited with full $24000.
    assert (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 9))
        .get_column("balance_usd")
        .item()
        == 24000.0
    )
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_same_month_scheduled_sales_consume_lots_sequentially() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="old",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=80.0,
            ),
            InitialLot(
                lot_id="new",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-6,
                quantity=100.0,
                cost_basis_per_unit_usd=100.0,
            ),
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=1,
                cause_id="first_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=70.0,
                price_per_unit_usd=150.0,
                proceeds_account_id="checking",
            ),
            ScheduledAssetSale(
                month=1,
                cause_id="second_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=70.0,
                price_per_unit_usd=150.0,
                proceeds_account_id="checking",
            ),
        ],
        tax_profiles=[],
        horizon_months=2,
    )

    result = simulate(scenario, rollout_count=1)

    dispositions = result.events_log.lot_dispositions.sort(["cause_id", "purchase_month_index"])
    assert dispositions.select("cause_id", "lot_id", "units_sold").to_dicts() == [
        {"cause_id": "first_sale", "lot_id": "old", "units_sold": pytest.approx(70.0)},
        {"cause_id": "second_sale", "lot_id": "old", "units_sold": pytest.approx(30.0)},
        {"cause_id": "second_sale", "lot_id": "new", "units_sold": pytest.approx(40.0)},
    ]

    end_lots = result.asset_lots.filter(pl.col("month_index") == 2).sort("lot_id")
    assert end_lots.select("lot_id", "remaining_quantity").to_dicts() == [
        {"lot_id": "new", "remaining_quantity": pytest.approx(60.0)},
        {"lot_id": "old", "remaining_quantity": pytest.approx(0.0)},
    ]
    final_cash = result.cash_balances.filter(pl.col("month_index") == 2).get_column("balance_usd").item()
    assert final_cash == pytest.approx(21_000.0)
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_fifo_holding_period_classification_per_disposition() -> None:
    """The disposition log carries `purchase_month_index` and
    sale-time `month_index` so downstream tax classification can
    compute holding period = sale - purchase per disposition row.
    LTCG split happens at 12 months; here the older lot is 18
    months old (LTCG) and the younger lot is 4 months old (STCG)."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="long_held",
                agent_id="alice",
                asset_id="btc",
                purchase_month_index=-12,
                quantity=2.0,
                cost_basis_per_unit_usd=20000.0,
            ),
            InitialLot(
                lot_id="short_held",
                agent_id="alice",
                asset_id="btc",
                purchase_month_index=2,
                quantity=1.0,
                cost_basis_per_unit_usd=40000.0,
            ),
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=6,
                cause_id="liquidate",
                agent_id="alice",
                asset_id="btc",
                quantity=2.5,
                price_per_unit_usd=60000.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[],
        horizon_months=7,
    )

    result = simulate(scenario, rollout_count=1)
    dispositions = result.events_log.lot_dispositions.with_columns(
        holding_period_months=pl.col("month_index") - pl.col("purchase_month_index")
    ).sort("purchase_month_index")

    rows = dispositions.iter_rows(named=True)
    long_disp = next(rows)
    short_disp = next(rows)

    assert long_disp["lot_id"] == "long_held"
    assert long_disp["holding_period_months"] == 18  # ≥12 → LTCG
    assert long_disp["units_sold"] == 2.0
    assert short_disp["lot_id"] == "short_held"
    assert short_disp["holding_period_months"] == 4  # <12 → STCG
    assert short_disp["units_sold"] == 0.5


def test_sales_of_two_different_assets_are_independent() -> None:
    """Two sales at different months on different assets resolve
    against their own lots independently. Tests that the
    `(agent, asset)` filter in FIFO doesn't bleed across assets."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="vti_lot",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=0,
                quantity=10.0,
                cost_basis_per_unit_usd=100.0,
            ),
            InitialLot(
                lot_id="qqq_lot",
                agent_id="alice",
                asset_id="qqq",
                purchase_month_index=0,
                quantity=10.0,
                cost_basis_per_unit_usd=200.0,
            ),
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=2,
                cause_id="sell_vti",
                agent_id="alice",
                asset_id="vti",
                quantity=4.0,
                price_per_unit_usd=150.0,
                proceeds_account_id="checking",
            ),
            ScheduledAssetSale(
                month=5,
                cause_id="sell_qqq",
                agent_id="alice",
                asset_id="qqq",
                quantity=3.0,
                price_per_unit_usd=250.0,
                proceeds_account_id="checking",
            ),
        ],
        tax_profiles=[],
        horizon_months=6,
    )

    result = simulate(scenario, rollout_count=1)
    assert result.events_log.lot_dispositions.height == 2

    end_lots = result.asset_lots.filter(pl.col("month_index") == 6).sort("lot_id")
    by_lot = {row["lot_id"]: row["remaining_quantity"] for row in end_lots.iter_rows(named=True)}
    assert by_lot == {"qqq_lot": 7.0, "vti_lot": 6.0}

    # Cash: 4×150 + 3×250 = $1350.
    assert (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 6))
        .get_column("balance_usd")
        .item()
        == 1350.0
    )


def test_series_driven_sale_uses_deterministic_price_curve(deterministic_series_bundle) -> None:
    """L5 — when a ScheduledAssetSale omits `price_per_unit_usd`,
    the engine reads the per-month price from the scenario's
    SeriesModelBundle. With a Deterministic model the price is identical
    across rollouts; the sale's proceeds reflect the configured
    month-N price."""
    horizon = 6
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="seed",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-3,
                quantity=10.0,
                cost_basis_per_unit_usd=90.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=4,
                cause_id="sampled_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=4.0,
                proceeds_account_id="checking",
            )
        ],
        external_series=deterministic_series_bundle([100.0, 110.0, 120.0, 130.0, 150.0, 160.0, 170.0]),
        tax_profiles=[],
        horizon_months=horizon,
    )

    result = simulate(scenario, rollout_count=1)

    # Sale at month 4 used the month-4 price of $150 → 4 × 150 = $600.
    assert result.events_log.lot_dispositions.height == 1
    disp = result.events_log.lot_dispositions.row(0, named=True)
    assert disp["units_sold"] == 4.0
    assert disp["proceeds_usd"] == 600.0

    # External series values on the run match the configured path.
    vti = result.series_values.filter(pl.col("series_id") == "vti").sort("month_index")
    assert vti.get_column("value").to_list() == [100.0, 110.0, 120.0, 130.0, 150.0, 160.0, 170.0]


def test_gbm_series_diverges_across_rollouts_same_seed_is_reproducible() -> None:
    """L10.1 — GBM paths produce different per-rollout trajectories
    (so sale proceeds differ across rollouts) but a fixed rollout-seed vector
    reproduces the same values across runs."""
    bundle = SeriesModelBundle.independent(
        {"vti": GeometricBrownian(initial_value=100.0, monthly_log_return_mu=0.005, monthly_log_return_sigma=0.05)}
    )
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="seed",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=0,
                quantity=5.0,
                cost_basis_per_unit_usd=100.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=3,
                cause_id="sampled_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=5.0,
                proceeds_account_id="checking",
            )
        ],
        external_series=bundle,
        tax_profiles=[],
        horizon_months=6,
    )

    result_a = simulate(scenario, rollout_count=200)
    result_b = simulate(scenario, rollout_count=200)

    # Reproducibility: same seed -> same values across two runs.
    assert result_a.series_values.sort(["rollout_index", "month_index"]).equals(
        result_b.series_values.sort(["rollout_index", "month_index"])
    )

    # Divergence: distinct per-rollout proceeds — far more than one
    # cluster, but bounded by the GBM variance. Loose check: at
    # least 100 distinct cash balances across 200 rollouts.
    cash_at_end = result_a.cash_balances.filter(
        (pl.col("agent_id") == "alice") & (pl.col("month_index") == 6)
    ).get_column("balance_usd")
    assert cash_at_end.n_unique() > 100


def test_year_end_tax_accrual_federal_and_california_single_filer() -> None:
    """L7 — Alice gets $200k of W-2 income in year 0. At month 11
    the engine computes federal + CA tax on (200000 - std_deduction)
    and writes one tax_liability row per jurisdiction.

    Federal: $200,000 - $14,600 = $185,400 taxable.
      10% × 11600 + 12% × 35550 + 22% × 53375 + 24% × 84875
      = 1160.00 + 4266.00 + 11742.50 + 20370.00 = 37538.50
    California: $200,000 - $5,363 = $194,637 taxable.
      1% × 10412 + 2% × 14272 + 4% × 14275 + 6% × 15122 + 8% × 14269
      + 9.3% × 126287 = 104.12 + 285.44 + 571.00 + 907.32 + 1141.52
      + 11744.69 = 14754.09
    """
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=200_000.0 / 12.0,
                income_category="ordinary",
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status="single",
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=12,
    )

    result = simulate(scenario, rollout_count=1)

    # 12 paycheck transfers fired (income_category = "ordinary").
    assert result.events_log.transfers.filter(pl.col("income_category") == "ordinary").height == 12

    # Two tax accruals at month 11 — federal + CA — for one rollout.
    accruals = result.events_log.tax_accruals.sort("jurisdiction_id")
    assert accruals.height == 2
    accruals_by_jurisdiction = {row["jurisdiction_id"]: row for row in accruals.iter_rows(named=True)}
    assert accruals_by_jurisdiction["federal_us"]["amount_usd"] == pytest.approx(37538.50, abs=0.01)
    assert accruals_by_jurisdiction["california"]["amount_usd"] == pytest.approx(14754.09, abs=0.02)
    assert accruals_by_jurisdiction["federal_us"]["month_index"] == 11
    assert accruals_by_jurisdiction["federal_us"]["tax_year_end_month"] == 11
    breakdowns = {row["jurisdiction_id"]: row for row in result.events_log.tax_breakdowns.iter_rows(named=True)}
    assert breakdowns["federal_us"]["ordinary_income_usd"] == pytest.approx(200_000.0, abs=1e-6)
    assert breakdowns["federal_us"]["ordinary_taxable_usd"] == pytest.approx(185_400.0, abs=1e-6)
    assert breakdowns["federal_us"]["ordinary_tax_usd"] == pytest.approx(37_538.50, abs=0.01)
    assert breakdowns["federal_us"]["total_tax_usd"] == pytest.approx(37_538.50, abs=0.01)

    # tax_liabilities at end-of-horizon has two rows (one per
    # jurisdiction) with matching amounts.
    end_liabilities = result.tax_liabilities.filter(pl.col("month_index") == 12).sort("jurisdiction_id")
    assert end_liabilities.height == 2
    assert end_liabilities.get_column("amount_owed_usd").to_list()[0] == pytest.approx(14754.09, abs=0.02)
    assert end_liabilities.get_column("amount_owed_usd").to_list()[1] == pytest.approx(37538.50, abs=0.01)

    # YTD reflects accumulated income across the year; the year-end
    # reset at month 11 (visible at month_index 12) drops it back
    # to 0. At month_index 11 (post-month-10) Alice has had 11
    # paychecks.
    ytd_alice = result.ordinary_income_ytd.filter(pl.col("agent_id") == "alice").sort("month_index")
    ytd_values = ytd_alice.get_column("ordinary_income_usd").to_list()
    assert ytd_values[11] == pytest.approx(11 * (200_000.0 / 12.0), abs=1e-6)
    assert ytd_values[12] == 0.0
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_year_end_tax_includes_long_term_capital_gain_under_federal_ltcg_schedule() -> None:
    """L8 — Alice gets $50k W-2 wages, plus sells a long-held VTI
    lot (24 months pre-horizon) for a $20k gain at month 6.

    Federal taxable ordinary = 50000 - 14600 = 35400.
      10% × 11600 + 12% × 23800 = 1160 + 2856 = 4016.
    LTCG stacks above ordinary. The 0% bracket ends at 47025, so
    11625 of LTCG falls in 0%; the remaining 8375 falls in 15%.
      LTCG tax = 8375 × 0.15 = 1256.25.
    Federal total = 4016 + 1256.25 = 5272.25.

    California taxes LTCG as ordinary income.
      Total CA taxable = 50000 + 20000 - 5363 = 64637.
      1% × 10412 + 2% × 14272 + 4% × 14275 + 6% × 15122 + 8% × 10556
      = 104.12 + 285.44 + 571.00 + 907.32 + 844.48 = 2712.36."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_long_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=80.0,
            )
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=50_000.0 / 12.0,
                income_category="ordinary",
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=6,
                cause_id="alice_long_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=100.0,
                price_per_unit_usd=280.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status="single",
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=12,
    )

    result = simulate(scenario, rollout_count=1)

    accruals = {row["jurisdiction_id"]: row for row in result.events_log.tax_accruals.iter_rows(named=True)}
    assert accruals["federal_us"]["amount_usd"] == pytest.approx(5272.25, abs=0.01)
    assert accruals["california"]["amount_usd"] == pytest.approx(2712.36, abs=0.01)
    breakdowns = {row["jurisdiction_id"]: row for row in result.events_log.tax_breakdowns.iter_rows(named=True)}
    assert breakdowns["federal_us"]["ordinary_taxable_usd"] == pytest.approx(35_400.0, abs=1e-6)
    assert breakdowns["federal_us"]["capital_gain_taxable_usd"] == pytest.approx(20_000.0, abs=1e-6)
    assert breakdowns["federal_us"]["ordinary_tax_usd"] == pytest.approx(4_016.0, abs=0.01)
    assert breakdowns["federal_us"]["capital_gain_tax_usd"] == pytest.approx(1_256.25, abs=0.01)
    assert breakdowns["california"]["ordinary_taxable_usd"] == pytest.approx(64_637.0, abs=1e-6)
    assert breakdowns["california"]["capital_gain_tax_usd"] == 0.0

    # YTD captured the LTCG ($20k) before year-end reset.
    cg_at_month_11 = result.capital_gains_ytd.filter((pl.col("month_index") == 11) & (pl.col("agent_id") == "alice"))
    assert cg_at_month_11.height == 1
    row = cg_at_month_11.row(0, named=True)
    assert row["classification"] == "ltcg"
    assert row["gain_usd"] == pytest.approx(20_000.0, abs=1e-6)
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_e2e_pinned_ltcg_tax_safe_harbor_and_cash_numerics() -> None:
    """Pinned deterministic e2e: wages + a long-held asset sale +
    federal/CA year tax + estimated-tax safe harbor + true-up.

    Alice earns $50k, sells a long-held VTI lot for $28k proceeds
    and $20k gain, and has $4k of prior-year tax. The safe-harbor
    quarterlies pay $1k at months 3/5/8/12; the month-12 true-up
    pays the remaining $3,984.61. Ending cash is:

      1000 + 50000 + 28000 - 7984.61 = 71015.39.
    """
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1_000.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_long_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=80.0,
            )
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=50_000.0 / 12.0,
                income_category="ordinary",
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=6,
                cause_id="alice_long_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=100.0,
                price_per_unit_usd=280.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status="single",
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
                prior_year_tax_usd=4_000.0,
            )
        ],
        horizon_months=13,
    )

    result = simulate(scenario, rollout_count=1)

    accruals = {row["jurisdiction_id"]: row for row in result.events_log.tax_accruals.iter_rows(named=True)}
    assert accruals["federal_us"]["amount_usd"] == pytest.approx(5272.25, abs=0.01)
    assert accruals["california"]["amount_usd"] == pytest.approx(2712.36, abs=0.01)

    tax_payments = result.events_log.transfers.filter(pl.col("cause_id").str.contains("tax")).sort(
        ["month_index", "cause_id"]
    )
    assert tax_payments.select("month_index", "cause_id", "amount_usd").to_dicts() == [
        {"month_index": 3, "cause_id": "alice_estimated_tax_q1_y0", "amount_usd": pytest.approx(1_000.0)},
        {"month_index": 5, "cause_id": "alice_estimated_tax_q2_y0", "amount_usd": pytest.approx(1_000.0)},
        {"month_index": 8, "cause_id": "alice_estimated_tax_q3_y0", "amount_usd": pytest.approx(1_000.0)},
        {"month_index": 12, "cause_id": "alice_estimated_tax_q4_y0", "amount_usd": pytest.approx(1_000.0)},
        {"month_index": 12, "cause_id": "alice_tax_true_up_y0", "amount_usd": pytest.approx(3_984.61, abs=0.02)},
    ]
    assert tax_payments.get_column("amount_usd").sum() == pytest.approx(7_984.61, abs=0.02)

    tax_settlement = result.events_log.tax_settlements.row(0, named=True)
    assert tax_settlement["month_index"] == 12
    assert tax_settlement["tax_year_end_month"] == 11
    assert tax_settlement["amount_usd"] == pytest.approx(7_984.61, abs=0.02)
    liabilities_due = result.tax_liabilities.filter(pl.col("month_index") == 12).get_column("amount_owed_usd").sum()
    assert liabilities_due == pytest.approx(7_984.61, abs=0.02)
    liabilities_settled = result.tax_liabilities.filter(pl.col("month_index") == 13).get_column("amount_owed_usd").sum()
    assert liabilities_settled == pytest.approx(0.0, abs=1e-6)

    final_cash = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 13))
        .get_column("balance_usd")
        .item()
    )
    assert final_cash == pytest.approx(71_015.39, abs=0.02)

    final_lot = result.asset_lots.filter((pl.col("lot_id") == "alice_long_vti") & (pl.col("month_index") == 13))
    assert final_lot.get_column("remaining_quantity").item() == 0.0

    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_e2e_pinned_multi_asset_ltcg_stcg_tax_breakdown_numerics() -> None:
    """Pinned tax aggregation e2e: wages plus two asset sales.

    Alice earns $50k, sells one long-held lot for $10k LTCG and one
    short-held lot for $1.5k STCG. Federal ordinary taxable income is
    50000 + 1500 - 14600 = 36900, producing $4,196 ordinary tax. The
    $10k LTCG still fits under the 0% LTCG bracket after stacking, so
    capital-gain tax is $0.
    """
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_long_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=100.0,
            ),
            InitialLot(
                lot_id="alice_short_ixus",
                agent_id="alice",
                asset_id="ixus",
                purchase_month_index=0,
                quantity=10.0,
                cost_basis_per_unit_usd=50.0,
            ),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=50_000.0 / 12.0,
                income_category="ordinary",
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=6,
                cause_id="alice_long_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=100.0,
                price_per_unit_usd=200.0,
                proceeds_account_id="checking",
            ),
            ScheduledAssetSale(
                month=6,
                cause_id="alice_short_sale",
                agent_id="alice",
                asset_id="ixus",
                quantity=10.0,
                price_per_unit_usd=200.0,
                proceeds_account_id="checking",
            ),
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice", filing_status="single", jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs"
            )
        ],
        horizon_months=12,
    )

    result = simulate(scenario, rollout_count=1)

    accrual = result.events_log.tax_accruals.row(0, named=True)
    assert accrual["amount_usd"] == pytest.approx(4_196.0, abs=0.01)
    breakdown = result.events_log.tax_breakdowns.row(0, named=True)
    assert breakdown["ordinary_income_usd"] == pytest.approx(50_000.0, abs=1e-6)
    assert breakdown["ltcg_usd"] == pytest.approx(10_000.0, abs=1e-6)
    assert breakdown["stcg_usd"] == pytest.approx(1_500.0, abs=1e-6)
    assert breakdown["ordinary_taxable_usd"] == pytest.approx(36_900.0, abs=1e-6)
    assert breakdown["ordinary_tax_usd"] == pytest.approx(4_196.0, abs=0.01)
    assert breakdown["capital_gain_tax_usd"] == pytest.approx(0.0, abs=1e-6)

    gains = {
        row["classification"]: row["gain_usd"]
        for row in result.capital_gains_ytd.filter((pl.col("month_index") == 11) & (pl.col("agent_id") == "alice"))
        .sort("classification")
        .iter_rows(named=True)
    }
    assert gains == {"ltcg": pytest.approx(10_000.0), "stcg": pytest.approx(1_500.0)}
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_e2e_pinned_tax_payments_force_asset_liquidation_and_settle_liability(deterministic_series_bundle) -> None:
    """Pinned obligation e2e: taxes are due-now outflows.

    Alice earns $50k and spends every paycheck on rent, so estimated
    taxes must be funded by selling VTI. Federal tax is $4,016.
    Prior-year safe harbor is $2,000: three $500 estimates in April,
    June, September; then January Q4 $500 plus $2,016 true-up.
    """
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll"), Agent(agent_id="landlord"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti_seed",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=100.0,
            )
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=50_000.0 / 12.0,
                income_category="ordinary",
            ),
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_rent",
                from_agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_usd=50_000.0 / 12.0,
            ),
        ],
        external_series=deterministic_series_bundle([100.0] * 14),
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status="single",
                jurisdiction_ids=["federal_us"],
                tax_authority_agent_id="irs",
                prior_year_tax_usd=2_000.0,
            )
        ],
        liquidity_policies=[LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=["vti"])],
        horizon_months=13,
    )

    result = simulate(scenario, rollout_count=1)

    tax_payments = result.events_log.transfers.filter(pl.col("cause_id").str.contains("tax")).sort(
        ["month_index", "cause_id"]
    )
    assert tax_payments.select("month_index", "cause_id", "amount_usd").to_dicts() == [
        {"month_index": 3, "cause_id": "alice_estimated_tax_q1_y0", "amount_usd": pytest.approx(500.0)},
        {"month_index": 5, "cause_id": "alice_estimated_tax_q2_y0", "amount_usd": pytest.approx(500.0)},
        {"month_index": 8, "cause_id": "alice_estimated_tax_q3_y0", "amount_usd": pytest.approx(500.0)},
        {"month_index": 12, "cause_id": "alice_estimated_tax_q4_y0", "amount_usd": pytest.approx(500.0)},
        {"month_index": 12, "cause_id": "alice_tax_true_up_y0", "amount_usd": pytest.approx(2_016.0)},
    ]
    assert result.events_log.tax_settlements.get_column("amount_usd").sum() == pytest.approx(4_016.0, abs=0.01)

    policy_sales = result.events_log.lot_dispositions.filter(pl.col("cause_id").str.starts_with("liquidity_sale"))
    assert policy_sales.sort("month_index").select("month_index", "units_sold", "proceeds_usd").to_dicts() == [
        {"month_index": 3, "units_sold": pytest.approx(5.0), "proceeds_usd": pytest.approx(500.0)},
        {"month_index": 5, "units_sold": pytest.approx(5.0), "proceeds_usd": pytest.approx(500.0)},
        {"month_index": 8, "units_sold": pytest.approx(5.0), "proceeds_usd": pytest.approx(500.0)},
        {"month_index": 12, "units_sold": pytest.approx(25.16), "proceeds_usd": pytest.approx(2_516.0)},
    ]

    final_cash = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 13))
        .get_column("balance_usd")
        .item()
    )
    assert final_cash == pytest.approx(0.0, abs=1e-6)
    remaining_vti = (
        result.asset_lots.filter((pl.col("lot_id") == "alice_vti_seed") & (pl.col("month_index") == 13))
        .get_column("remaining_quantity")
        .item()
    )
    assert remaining_vti == pytest.approx(59.84, abs=1e-6)
    final_due = result.tax_liabilities.filter(pl.col("month_index") == 13).get_column("amount_owed_usd").sum()
    assert final_due == pytest.approx(0.0, abs=1e-6)
    assert result.rollout_status.row(0, named=True)["status"] == "active"
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_explicit_empty_tax_profiles_means_no_year_end_accrual() -> None:
    """An explicit no-tax scenario emits no year-end accruals."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=5_000.0,
                income_category="ordinary",
            )
        ],
        tax_profiles=[],
        horizon_months=12,
    )

    result = simulate(scenario, rollout_count=1)
    assert result.events_log.tax_accruals.is_empty()
    assert result.tax_liabilities.is_empty()


def test_year_end_tax_payment_debits_agent_cash() -> None:
    """The year-end tax accrual is followed by a January true-up
    payment to the tax authority. Alice earns $200k of W-2 income
    across year 0; with no prior-year safe-harbor amount configured,
    the full tax is paid as the month-12 true-up."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=200_000.0 / 12.0,
                income_category="ordinary",
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status="single",
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=13,
    )

    result = simulate(scenario, rollout_count=1)

    # Year-end tax: $37538.50 federal + $14754.09 CA = $52292.59.
    tax_payments = result.events_log.transfers.filter(pl.col("cause_id").str.contains("tax"))
    assert tax_payments.height == 1
    assert tax_payments.get_column("amount_usd").sum() == pytest.approx(52_292.59, abs=0.02)
    assert tax_payments.row(0, named=True)["cause_id"] == "alice_tax_true_up_y0"
    # Tax true-up fires in January after the year-end accrual.
    assert set(tax_payments.get_column("month_index").to_list()) == {12}
    assert result.events_log.tax_settlements.height == 1
    settlement = result.events_log.tax_settlements.row(0, named=True)
    assert settlement["cause_id"] == "alice_tax_settlement_y0"
    assert settlement["amount_usd"] == pytest.approx(52_292.59, abs=0.02)

    due_before_payment = result.tax_liabilities.filter(pl.col("month_index") == 12).get_column("amount_owed_usd").sum()
    assert due_before_payment == pytest.approx(52_292.59, abs=0.02)
    due_after_payment = result.tax_liabilities.filter(pl.col("month_index") == 13).get_column("amount_owed_usd").sum()
    assert due_after_payment == pytest.approx(0.0, abs=1e-6)

    # Cash flow: $200k income - $52292.59 tax = $147707.41 at end of horizon.
    alice_end_cash = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 13))
        .get_column("balance_usd")
        .item()
    )
    assert alice_end_cash == pytest.approx(200_000.0 - 52_292.59, abs=0.02)
    # The IRS sink accumulates the tax inflows.
    irs_end_cash = (
        result.cash_balances.filter((pl.col("agent_id") == "irs") & (pl.col("month_index") == 13))
        .get_column("balance_usd")
        .item()
    )
    assert irs_end_cash == pytest.approx(52_292.59, abs=0.02)
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_tax_payment_can_trigger_rollout_failure_when_unfunded() -> None:
    """When the tax-payment true-up transfer exceeds the
    agent's cash plus liquidity-policy sale proceeds, due-now
    settlement fails the rollout. The "mandatory obligation that
    fails the scenario if unpaid" pattern works for any cash outflow
    — taxes here, rent in other tests, later mortgages."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="payroll"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="payroll", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_paycheck",
                from_agent_id="payroll",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=500_000.0 / 12.0,  # big tax bill
                income_category="ordinary",
            ),
            RecurringTransfer(
                start_month=0,
                end_month=11,
                cause_id="alice_rent",
                from_agent_id="alice",
                from_account_id="checking",
                to_agent_id="payroll",  # use payroll as sink
                to_account_id="checking",
                amount_usd=500_000.0 / 12.0,  # spend it all on rent
            ),
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status="single",
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ],
        liquidity_policies=[
            LiquidityPolicy(
                agent_id="alice",
                account_id="checking",
                asset_preference_chain=[],  # no assets to sell
            )
        ],
        horizon_months=13,
    )

    result = simulate(scenario, rollout_count=1)
    # Alice has $0 cash after year 0 (income == rent), no assets,
    # but the tax bill arrives in January. Failure fires at month 12.
    failures = result.events_log.rollout_failures
    assert failures.height == 1
    assert failures.row(0, named=True)["month_index"] == 12
    assert result.rollout_status.row(0, named=True)["status"] == "failed_insufficient_cash"
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_due_now_obligation_sells_assets_and_settles(deterministic_series_bundle) -> None:
    """A required obligation uses cash first, sells configured assets
    for the remaining shortfall, then pays the counterparty in full."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=10.0,
                cost_basis_per_unit_usd=50.0,
            )
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
                obligation_id="rent_due",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=500.0,
            )
        ],
        external_series=deterministic_series_bundle([100.0, 100.0]),
        liquidity_policies=[LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=["vti"])],
        tax_profiles=[],
        horizon_months=1,
    )

    result = simulate(scenario, rollout_count=1)

    accrual = result.events_log.obligation_accruals.row(0, named=True)
    assert accrual["obligation_id"] == "rent_due_m0"
    assert accrual["amount_due_usd"] == pytest.approx(500.0)

    settlement = result.events_log.obligation_settlements.row(0, named=True)
    assert settlement["amount_paid_usd"] == pytest.approx(500.0)
    assert settlement["shortfall_usd"] == pytest.approx(0.0)
    assert settlement["attempted_funding_sources"] == "vti"

    funding_sale = result.events_log.lot_dispositions.row(0, named=True)
    assert funding_sale["cause_id"] == "liquidity_sale_m0_vti"
    assert funding_sale["units_sold"] == pytest.approx(4.0)
    assert funding_sale["proceeds_usd"] == pytest.approx(400.0)

    final_cash = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 1))
        .get_column("balance_usd")
        .item()
    )
    assert final_cash == pytest.approx(0.0)
    assert result.events_log.rollout_failures.is_empty()
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_series_indexed_recurring_rent_obligation_resets_yearly_by_rollout() -> None:
    """Alice pays rent to a landlord. The rent is fixed within each
    lease year and resets annually using each rollout's rent series path."""
    rent_series_id = "rent:san_francisco_ca"
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=20_000.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                obligation_id="outside_rent",
                obligation_type="outside_rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=SeriesIndexedAmount(
                    base_amount_usd=1_000.0, series_id=rent_series_id, base_month_index=0, adjustment_period_months=12
                ),
            )
        ],
        tax_profiles=[],
        horizon_months=13,
    )
    external_series = _external_series_context_for_levels(
        rent_series_id, levels_by_rollout=[[100.0] * 12 + [110.0], [100.0] * 12 + [90.0]]
    )

    result = simulate_with_external_series(scenario, rollout_count=2, external_series=external_series)

    accruals = result.events_log.obligation_accruals.sort(["rollout_index", "month_index"])
    for rollout_index in (0, 1):
        first_year = accruals.filter((pl.col("rollout_index") == rollout_index) & (pl.col("month_index") < 12))
        assert first_year.get_column("amount_due_usd").to_list() == pytest.approx([1_000.0] * 12)

    reset_amounts = (
        accruals.filter(pl.col("month_index") == 12).sort("rollout_index").get_column("amount_due_usd").to_list()
    )
    assert reset_amounts == pytest.approx([1_100.0, 900.0])

    final_cash = result.cash_balances.filter(pl.col("month_index") == 13).sort(["rollout_index", "agent_id"])
    assert final_cash.get_column("balance_usd").to_list() == pytest.approx([6_900.0, 13_100.0, 7_100.0, 12_900.0])
    assert result.events_log.rollout_failures.is_empty()
    assert_replay_invariant_holds(scenario, result, rollout_count=2)


def test_series_indexed_recurring_transfer_uses_same_amount_schedule() -> None:
    """Tenant rent income uses the same path-indexed amount machinery
    as due-now rent obligations."""
    rent_series_id = "rent:san_francisco_ca"
    scenario = Scenario(
        agents=[Agent(agent_id="tenant"), Agent(agent_id="alice")],
        initial_cash=[
            InitialAccountBalance(agent_id="tenant", account_id="checking", balance_usd=20_000.0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
        ],
        recurring_transfers=[
            RecurringTransfer(
                start_month=0,
                cause_id="tenant_rent",
                from_agent_id="tenant",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount_usd=SeriesIndexedAmount(
                    base_amount_usd=1_500.0, series_id=rent_series_id, base_month_index=0, adjustment_period_months=12
                ),
            )
        ],
        tax_profiles=[],
        horizon_months=13,
    )
    external_series = _external_series_context_for_levels(rent_series_id, levels_by_rollout=[[200.0] * 12 + [240.0]])

    result = simulate_with_external_series(scenario, rollout_count=1, external_series=external_series)

    transfers = result.events_log.transfers.sort("month_index")
    assert transfers.filter(pl.col("month_index") < 12).get_column("amount_usd").to_list() == pytest.approx(
        [1_500.0] * 12
    )
    assert transfers.filter(pl.col("month_index") == 12).get_column("amount_usd").item() == pytest.approx(1_800.0)

    final_cash = result.cash_balances.filter(pl.col("month_index") == 13).sort("agent_id")
    assert final_cash.get_column("balance_usd").to_list() == pytest.approx([19_800.0, 200.0])
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_due_now_obligation_failure_aborts_payment() -> None:
    """If cash plus configured funding sources cannot cover a required
    obligation, no partial payment is made and the rollout fails."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
                obligation_id="rent_due",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=500.0,
            )
        ],
        liquidity_policies=[LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=[])],
        tax_profiles=[],
        horizon_months=1,
    )

    result = simulate(scenario, rollout_count=1)

    settlement = result.events_log.obligation_settlements.row(0, named=True)
    assert settlement["amount_due_usd"] == pytest.approx(500.0)
    assert settlement["amount_paid_usd"] == pytest.approx(0.0)
    assert settlement["shortfall_usd"] == pytest.approx(500.0)
    assert result.events_log.transfers.is_empty()

    failure = result.events_log.rollout_failures.row(0, named=True)
    assert failure["obligation_id"] == "rent_due_m0"
    assert failure["obligation_type"] == "rent"
    assert failure["shortfall_usd"] == pytest.approx(500.0)
    assert result.rollout_status.row(0, named=True)["status"] == "failed_insufficient_cash"
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_policy_without_sale_orders_fails_hard_demand_even_with_assets(deterministic_series_bundle) -> None:
    """A liquidity policy owns sale decisions. If it emits no sale
    orders, settlement will fail a hard demand even when sellable
    assets are present."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=10.0,
                cost_basis_per_unit_usd=50.0,
            )
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
                obligation_id="rent_due",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=500.0,
            )
        ],
        external_series=deterministic_series_bundle([100.0, 100.0]),
        liquidity_policies=[LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=[])],
        tax_profiles=[],
        horizon_months=1,
    )

    result = simulate(scenario, rollout_count=1)

    assert result.events_log.lot_dispositions.is_empty()
    settlement = result.events_log.obligation_settlements.row(0, named=True)
    assert settlement["amount_paid_usd"] == pytest.approx(0.0)
    assert settlement["shortfall_usd"] == pytest.approx(500.0)
    assert result.events_log.rollout_failures.height == 1
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_cash_buffer_sale_evaluates_after_hard_demands(deterministic_series_bundle) -> None:
    """Buffer policy sees post-demand cash: cash 2500 minus a 1000
    hard demand leaves 1500, below the 2000 trigger, so the policy
    sells a fixed 5000 before settlement pays the demand."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=2500.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=50.0,
            )
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
                obligation_id="rent_due",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=1000.0,
            )
        ],
        external_series=deterministic_series_bundle([100.0, 100.0]),
        liquidity_policies=[
            LiquidityPolicy(
                agent_id="alice",
                account_id="checking",
                asset_preference_chain=["vti"],
                cash_buffer_trigger_below_usd=2000.0,
                cash_buffer_sale_usd=5000.0,
            )
        ],
        tax_profiles=[],
        horizon_months=1,
    )

    result = simulate(scenario, rollout_count=1)

    sale = result.events_log.lot_dispositions.row(0, named=True)
    assert sale["units_sold"] == pytest.approx(50.0)
    assert sale["proceeds_usd"] == pytest.approx(5000.0)
    alice_final = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 1))
        .get_column("balance_usd")
        .item()
    )
    assert alice_final == pytest.approx(6500.0)
    assert result.events_log.rollout_failures.is_empty()
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_cash_buffer_not_triggered_when_post_demand_cash_is_enough(deterministic_series_bundle) -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=3500.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-24,
                quantity=100.0,
                cost_basis_per_unit_usd=50.0,
            )
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
                obligation_id="rent_due",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=1000.0,
            )
        ],
        external_series=deterministic_series_bundle([100.0, 100.0]),
        liquidity_policies=[
            LiquidityPolicy(
                agent_id="alice",
                account_id="checking",
                asset_preference_chain=["vti"],
                cash_buffer_trigger_below_usd=2000.0,
                cash_buffer_sale_usd=5000.0,
            )
        ],
        tax_profiles=[],
        horizon_months=1,
    )

    result = simulate(scenario, rollout_count=1)

    assert result.events_log.lot_dispositions.is_empty()
    alice_final = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 1))
        .get_column("balance_usd")
        .item()
    )
    assert alice_final == pytest.approx(2500.0)
    assert result.events_log.rollout_failures.is_empty()
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_unfilled_cash_buffer_sale_does_not_fail_without_hard_demand() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1000.0)],
        liquidity_policies=[
            LiquidityPolicy(
                agent_id="alice",
                account_id="checking",
                asset_preference_chain=[],
                cash_buffer_trigger_below_usd=2000.0,
                cash_buffer_sale_usd=5000.0,
            )
        ],
        tax_profiles=[],
        horizon_months=1,
    )

    result = simulate(scenario, rollout_count=1)

    assert result.events_log.lot_dispositions.is_empty()
    assert result.events_log.rollout_failures.is_empty()
    assert result.rollout_status.row(0, named=True)["status"] == "active"
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_same_account_hard_demands_settle_all_or_none() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord"), Agent(agent_id="utility")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=600.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="utility", account_id="checking", balance_usd=0.0),
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
                obligation_id="rent_due",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=500.0,
            ),
            ScheduledObligation(
                month=0,
                obligation_id="utility_due",
                obligation_type="utility",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="utility",
                to_account_id="checking",
                amount_due_usd=500.0,
            ),
        ],
        tax_profiles=[],
        horizon_months=1,
    )

    result = simulate(scenario, rollout_count=1)

    settlements = result.events_log.obligation_settlements.sort("obligation_id")
    assert settlements.select("obligation_id", "amount_paid_usd", "shortfall_usd").to_dicts() == [
        {"obligation_id": "rent_due_m0", "amount_paid_usd": pytest.approx(0.0), "shortfall_usd": pytest.approx(500.0)},
        {
            "obligation_id": "utility_due_m0",
            "amount_paid_usd": pytest.approx(0.0),
            "shortfall_usd": pytest.approx(500.0),
        },
    ]
    assert result.events_log.transfers.is_empty()
    assert result.events_log.rollout_failures.height == 2
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_explicit_sale_price_overrides_sampled_series(deterministic_series_bundle) -> None:
    """If `ScheduledAssetSale.price_per_unit_usd` is set the engine
    uses that scalar; sampled series is ignored for that sale. This is the
    test-fixture path used in L4 tests; still valid in the
    external-series-aware engine."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="seed",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=0,
                quantity=10.0,
                cost_basis_per_unit_usd=50.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=1,
                cause_id="fixed_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=3.0,
                price_per_unit_usd=99.0,
                proceeds_account_id="checking",
            )
        ],
        external_series=deterministic_series_bundle([10.0, 10.0, 10.0]),
        tax_profiles=[],
        horizon_months=2,
    )

    result = simulate(scenario, rollout_count=1)
    assert result.events_log.lot_dispositions.get_column("proceeds_usd").item() == 3.0 * 99.0


def test_real_estate_purchase_mortgage_and_property_tax_numerics() -> None:
    """First real-estate slice: purchase creates property state,
    owner stake, mortgage liability, and monthly carrying-cost cash
    flows. Month 0 books purchase cash; month 1 books one mortgage
    payment and one property-tax transfer."""
    scenario = Scenario(
        agents=[
            Agent(agent_id="alice"),
            Agent(agent_id="seller"),
            Agent(agent_id="bank"),
            Agent(agent_id="sf_tax_collector"),
        ],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=120_000.0),
            InitialAccountBalance(agent_id="seller", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="bank", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="sf_tax_collector", account_id="checking", balance_usd=0.0),
        ],
        scheduled_property_purchases=[
            ScheduledPropertyPurchase(
                month=0,
                cause_id="alice_buys_sf_home",
                property_id="sf_home",
                location_id="san_francisco",
                buyer_agent_id="alice",
                buyer_account_id="checking",
                seller_agent_id="seller",
                purchase_price_usd=500_000.0,
                down_payment_usd=100_000.0,
                buyer_closing_cost_usd=10_000.0,
                mortgage=MortgageFinancing(
                    liability_id="sf_home_mortgage",
                    lender_agent_id="bank",
                    principal_usd=400_000.0,
                    annual_interest_rate=0.06,
                    term_months=360,
                ),
            )
        ],
        property_tax_policies=[
            PropertyTaxPolicy(
                property_id="sf_home",
                owner_agent_id="alice",
                tax_authority_agent_id="sf_tax_collector",
                annual_tax_rate=0.012,
            )
        ],
        tax_profiles=[],
        horizon_months=2,
    )

    result = simulate(scenario, rollout_count=1)

    final_property = result.property_state.filter(pl.col("month_index") == 2).row(0, named=True)
    assert final_property["location_id"] == "san_francisco"
    assert final_property["purchase_month_index"] == 0
    assert final_property["adjusted_basis_usd"] == pytest.approx(510_000.0)

    final_stake = result.property_stakes.filter(pl.col("month_index") == 2).row(0, named=True)
    assert final_stake["agent_id"] == "alice"
    assert final_stake["ownership_pct"] == pytest.approx(1.0)
    assert final_stake["contribution_used_usd"] == pytest.approx(110_000.0)
    assert final_stake["equity_ledger_usd"] == pytest.approx(100_000.0)

    mortgage_payment = 400_000.0 * 0.005 / (1.0 - (1.005**-360))
    final_liability = result.liabilities.filter(pl.col("month_index") == 2).row(0, named=True)
    assert final_liability["principal_usd"] == pytest.approx(400_000.0 - (mortgage_payment - 2_000.0))
    assert final_liability["interest_paid_ytd_usd"] == pytest.approx(2_000.0)
    assert final_liability["principal_paid_ytd_usd"] == pytest.approx(mortgage_payment - 2_000.0)

    final_cash = (
        result.cash_balances.filter((pl.col("month_index") == 2) & (pl.col("agent_id") == "alice"))
        .get_column("balance_usd")
        .item()
    )
    assert final_cash == pytest.approx(120_000.0 - 110_000.0 - mortgage_payment - 510.0)

    assert result.events_log.property_purchases.height == 1
    assert result.events_log.mortgage_originations.height == 1
    assert result.events_log.mortgage_payments.height == 1
    assert result.events_log.transfers.filter(pl.col("cause_id") == "sf_home_property_tax_m1").height == 1
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_liquidity_policy_covers_monthly_spend_deficit(deterministic_series_bundle) -> None:
    """L9 — Alice has $1k cash, a $5k/month spend, and 200 units of
    VTI at $100/unit sampled price. The liquidity policy sees the
    due-now rent demand, sells the amount cash cannot already cover,
    and settlement pays the rent in full. At month 0 it sells $4k of
    VTI (40 units). The lot is large enough to cover all three months
    of spend, so cash stays at $0 through end-of-horizon."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=1000.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-1,
                quantity=200.0,
                cost_basis_per_unit_usd=50.0,
            )
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                obligation_id="alice_rent",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=5000.0,
            )
        ],
        external_series=deterministic_series_bundle([100.0] * 4),
        liquidity_policies=[LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=["vti"])],
        tax_profiles=[],
        horizon_months=3,
    )

    result = simulate(scenario, rollout_count=1)

    # Month-0 sale: deficit was 4000, sold 40 units at $100 = $4000.
    m0_dispositions = result.events_log.lot_dispositions.filter(pl.col("month_index") == 0)
    assert m0_dispositions.height == 1
    assert m0_dispositions.row(0, named=True)["units_sold"] == pytest.approx(40.0, abs=1e-6)
    assert m0_dispositions.row(0, named=True)["proceeds_usd"] == pytest.approx(4000.0, abs=1e-6)

    # End-of-horizon (month 3) cash for Alice should be at the floor (0).
    end_cash = (
        result.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("month_index") == 3))
        .get_column("balance_usd")
        .item()
    )
    assert end_cash == pytest.approx(0.0, abs=1e-6)
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


def test_rollout_marked_failed_when_assets_exhausted(deterministic_series_bundle) -> None:
    """L11 — when the liquidity policy cannot emit enough sale
    proceeds for a hard demand, settlement marks the rollout failed."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                asset_id="vti",
                purchase_month_index=-1,
                quantity=5.0,  # only $500 of VTI at $100/unit
                cost_basis_per_unit_usd=80.0,
            )
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=0,
                obligation_id="alice_rent",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=1000.0,
            )
        ],
        external_series=deterministic_series_bundle([100.0, 100.0]),
        liquidity_policies=[LiquidityPolicy(agent_id="alice", account_id="checking", asset_preference_chain=["vti"])],
        tax_profiles=[],
        horizon_months=1,
    )

    result = simulate(scenario, rollout_count=1)

    # Failure event fired at month 0: rent demand was $1000, but
    # only $500 of VTI could be liquidated, so no rent payment fires.
    assert result.events_log.rollout_failures.height == 1
    failure = result.events_log.rollout_failures.row(0, named=True)
    assert failure["month_index"] == 0
    assert failure["deficit_usd"] == pytest.approx(1000.0, abs=1e-6)
    assert failure["agent_id"] == "alice"

    status_row = result.rollout_status.row(0, named=True)
    assert status_row["status"] == "failed_insufficient_cash"
    assert status_row["failed_month"] == 0

    failed_cash = result.cash_balances.filter((pl.col("rollout_index") == 0) & (pl.col("month_index") >= 1))
    assert failed_cash.get_column("balance_usd").to_list() == [0.0, 0.0]
    failed_lots = result.asset_lots.filter((pl.col("rollout_index") == 0) & (pl.col("month_index") >= 1))
    assert failed_lots.get_column("remaining_quantity").to_list() == [0.0]
    assert_replay_invariant_holds(scenario, result, rollout_count=1)


if __name__ == "__main__":
    pytest_bazel.main()
