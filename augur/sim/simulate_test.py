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

from augur.sim.apply import apply_events
from augur.sim.market import DeterministicPath, GeometricBrownianPath, MarketBundle
from augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    InitialLot,
    RecurringTransfer,
    Scenario,
    ScheduledAssetSale,
    ScheduledTransfer,
)
from augur.sim.simulate import _initial_state, simulate


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
        horizon_months=1,
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
    """Replay invariant: re-applying the event log to the initial
    state from scratch produces the same final cross-section as the
    incrementally-maintained one. If apply_events ever drifts from
    the log this test catches it."""
    scenario = _alice_bob_scenario()
    rollout_count = 1
    result = simulate(scenario, rollout_count=rollout_count)

    final_incremental = result.cash_balances.filter(pl.col("month_index") == int(scenario.horizon_months)).sort(
        ["rollout_index", "agent_id", "account_id"]
    )

    # Re-derive: apply the entire event log to a fresh initial state.
    initial = _initial_state(scenario, rollout_count)
    from_log = apply_events(initial, result.events_log).cash_balances.sort(["rollout_index", "agent_id", "account_id"])

    assert from_log.equals(final_incremental.drop("month_index"))


def test_no_scheduled_transfers_leaves_balances_unchanged() -> None:
    """Multi-month horizon with no events should carry initial cash
    forward unchanged. Exercises the empty-event-log path through
    the loop."""
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100.0)],
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
    inputs (no market path, same scenario), every rollout produces
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
    """Replaying the event log from the initial state must
    reproduce the incremental end-state — for both cash and lots.
    This catches drift between `apply_events` and the live loop."""
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
        horizon_months=2,
    )
    rollout_count = 3

    result = simulate(scenario, rollout_count=rollout_count)

    initial = _initial_state(scenario, rollout_count)
    replayed = apply_events(initial, result.events_log)

    final_cash = (
        result.cash_balances.filter(pl.col("month_index") == 2)
        .drop("month_index")
        .sort(["rollout_index", "agent_id", "account_id"])
    )
    final_lots = (
        result.asset_lots.filter(pl.col("month_index") == 2).drop("month_index").sort(["rollout_index", "lot_id"])
    )

    assert replayed.cash_balances.sort(["rollout_index", "agent_id", "account_id"]).equals(final_cash)
    assert replayed.asset_lots.sort(["rollout_index", "lot_id"]).equals(final_lots)


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


def test_market_driven_sale_uses_deterministic_price_curve() -> None:
    """L5 — when a ScheduledAssetSale omits `price_per_unit_usd`,
    the engine reads the per-month price from the scenario's
    MarketBundle. With a DeterministicPath the price is identical
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
                cause_id="market_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=4.0,
                proceeds_account_id="checking",
            )
        ],
        market=MarketBundle(
            paths=[DeterministicPath(asset_id="vti", prices_usd=[100.0, 110.0, 120.0, 130.0, 150.0, 160.0, 170.0])]
        ),
        horizon_months=horizon,
    )

    result = simulate(scenario, rollout_count=1)

    # Sale at month 4 used the month-4 price of $150 → 4 × 150 = $600.
    assert result.events_log.lot_dispositions.height == 1
    disp = result.events_log.lot_dispositions.row(0, named=True)
    assert disp["units_sold"] == 4.0
    assert disp["proceeds_usd"] == 600.0

    # Market prices on the run match the configured path.
    vti = result.market_prices.filter(pl.col("asset_id") == "vti").sort("month_index")
    assert vti.get_column("price_per_unit_usd").to_list() == [100.0, 110.0, 120.0, 130.0, 150.0, 160.0, 170.0]


def test_gbm_market_diverges_across_rollouts_same_seed_is_reproducible() -> None:
    """L10.1 — GBM paths produce different per-rollout trajectories
    (so sale proceeds differ across rollouts) but a fixed `rng_seed`
    reproduces the same prices across runs."""
    bundle = MarketBundle(
        paths=[
            GeometricBrownianPath(
                asset_id="vti",
                initial_price_usd=100.0,
                monthly_log_return_mu=0.005,
                monthly_log_return_sigma=0.05,
                rng_seed=42,
            )
        ]
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
                cause_id="market_sale",
                agent_id="alice",
                asset_id="vti",
                quantity=5.0,
                proceeds_account_id="checking",
            )
        ],
        market=bundle,
        horizon_months=6,
    )

    result_a = simulate(scenario, rollout_count=200)
    result_b = simulate(scenario, rollout_count=200)

    # Reproducibility: same seed → same prices across two runs.
    assert result_a.market_prices.sort(["rollout_index", "month_index"]).equals(
        result_b.market_prices.sort(["rollout_index", "month_index"])
    )

    # Divergence: distinct per-rollout proceeds — far more than one
    # cluster, but bounded by the GBM variance. Loose check: at
    # least 100 distinct cash balances across 200 rollouts.
    cash_at_end = result_a.cash_balances.filter(
        (pl.col("agent_id") == "alice") & (pl.col("month_index") == 6)
    ).get_column("balance_usd")
    assert cash_at_end.n_unique() > 100


def test_explicit_sale_price_overrides_market() -> None:
    """If `ScheduledAssetSale.price_per_unit_usd` is set the engine
    uses that scalar; market is ignored for that sale. This is the
    test-fixture path used in L4 tests; still valid in the
    market-aware engine."""
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
        market=MarketBundle(paths=[DeterministicPath(asset_id="vti", prices_usd=[10.0, 10.0, 10.0])]),
        horizon_months=2,
    )

    result = simulate(scenario, rollout_count=1)
    assert result.events_log.lot_dispositions.get_column("proceeds_usd").item() == 3.0 * 99.0


if __name__ == "__main__":
    pytest_bazel.main()
