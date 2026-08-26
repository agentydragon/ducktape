"""The target-allocation policy, end to end through the engine.

The unit tests in `target_allocation_test.py` prove the policy's arithmetic. These prove the
ENGINE runs it: that the observation it builds is the agent's real state, that the orders
come back and are executed against real lots, and that the money moves with both legs.

Prices are pinned rather than sampled, so every number below is exact.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.deterministic import Deterministic
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.series import SecuritySymbol
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.product.asset_key import SecurityKey
from finance.augur.sim.engine.jax_engine import _program_impl
from finance.augur.sim.scenario import (
    Agent,
    FixedAmount,
    InitialAccountBalance,
    InitialLot,
    RecurringObligation,
    Scenario,
    SleeveTarget,
    TargetAllocationPolicy,
)
from finance.augur.sim.simulate import simulate
from finance.augur.sim.test_state_helpers import asset_lots, cash_balances, rollout_status

_HORIZON = 4
_STOCK = SecurityKey(symbol=SecuritySymbol("vti"))
_BOND = SecurityKey(symbol=SecuritySymbol("bnd"))
_PRICE = Decimal(100)
# Flat, so a sale's proceeds are exactly units x price and nothing drifts month to month.
_PATH = [float(_PRICE)] * (_HORIZON + 1)


def _scenario(
    *,
    opening_cash: Decimal | int,
    floor: Decimal | int,
    ceiling: Decimal | int,
    stock_units: float = 900.0,
    bond_units: float = 100.0,
    rent: Decimal | int = 0,
    income: Decimal | int = 0,
    purchase_slots: int = 0,
    rebalance_tolerance: float | None = None,
    weights: tuple[int, int] = (1, 1),
) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=opening_cash),
            # Funded only when it owes something, so an unfunded payer can never fail a rollout.
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance=income * (_HORIZON + 1)),
        ],
        initial_lots=[
            InitialLot(
                lot_id="stock",
                agent_id="alice",
                account_id="checking",
                asset=_STOCK,
                quantity=stock_units,
                cost_basis_per_unit=_PRICE,
                purchase_month_index=0,
            ),
            InitialLot(
                lot_id="bond",
                agent_id="alice",
                account_id="checking",
                asset=_BOND,
                quantity=bond_units,
                cost_basis_per_unit=_PRICE,
                purchase_month_index=0,
            ),
        ],
        recurring_obligations=[
            *(
                [
                    RecurringObligation(
                        start_month=1,
                        obligation_id="rent",
                        obligation_type="rent",
                        agent_id="alice",
                        from_account_id="checking",
                        to_agent_id="landlord",
                        to_account_id="checking",
                        amount_due=FixedAmount(amount=rent),
                    )
                ]
                if rent
                else []
            ),
            # The landlord paying alice: an inflow, so it is not in alice's scheduled outflow and the
            # band only sees it the month AFTER it lands. That is what makes the buy side fire more
            # than once — an (s,S) band above its ceiling invests down to the floor and then sits.
            *(
                [
                    RecurringObligation(
                        start_month=1,
                        obligation_id="income",
                        obligation_type="cash_spend",
                        agent_id="landlord",
                        from_account_id="checking",
                        to_agent_id="alice",
                        to_account_id="checking",
                        amount_due=FixedAmount(amount=income),
                    )
                ]
                if income
                else []
            ),
        ],
        target_allocation_policies=[
            TargetAllocationPolicy(
                agent_id="alice",
                account_id="checking",
                # Equal weights against a 9:1 holding, so the stock sleeve is the overweight one
                # and every sale must come out of it first.
                sleeves=[SleeveTarget(asset=_STOCK, weight=weights[0]), SleeveTarget(asset=_BOND, weight=weights[1])],
                cash_floor=floor,
                cash_ceiling=ceiling,
                purchase_slots_per_sleeve=purchase_slots,
                rebalance_tolerance=rebalance_tolerance,
            )
        ],
        tax_profiles=[],
        horizon_months=_HORIZON,
        external_series=SeriesModelBundle.independent(
            asset_prices=AssetPriceGroups(
                security={
                    SecuritySymbol("vti"): Deterministic(levels=_PATH),
                    SecuritySymbol("bnd"): Deterministic(levels=_PATH),
                }
            )
        ),
    )


def _run(scenario: Scenario):
    return simulate(scenario, rollout_count=1, locations={})


def _cash(scenario: Scenario) -> list[int]:
    run = _run(scenario)
    return [
        int(v)
        for v in cash_balances(run)
        .filter(pl.col("agent_id") == "alice")
        .sort("month_index")
        .get_column("balance_quanta")
        .to_list()
    ]


def _lots(scenario: Scenario, *, month: int) -> dict[str, float]:
    run = _run(scenario)
    rows = asset_lots(run).filter(pl.col("month_index") == month).to_dicts()
    return {str(row["lot_id"]): float(row["remaining_quantity"]) for row in rows}


def test_a_month_inside_the_band_sells_nothing() -> None:
    """Drift alone never triggers a trade. The portfolio is 9:1 against a 1:1 target — as far
    from target as this scenario gets — and the policy still does nothing while cash sits
    inside the band. Rebalancing rides cashflow only."""

    scenario = _scenario(opening_cash=50_000, floor=10_000, ceiling=90_000)

    assert _lots(scenario, month=_HORIZON) == {"stock": 900.0, "bond": 100.0}
    assert _cash(scenario)[-1] == 5_000_000


def test_crossing_the_floor_refills_to_the_ceiling() -> None:
    """(s,S), through the engine. Cash below the floor is raised to the CEILING, not back to
    the floor — refilling to the floor would put the agent back at its trigger next month,
    making it a forced seller into every dip.

    $5,000 with a $10,000 floor and a $40,000 ceiling raises $35,000, which at $100/unit is
    350 units out of the overweight stock sleeve.
    """

    scenario = _scenario(opening_cash=5_000, floor=10_000, ceiling=40_000)

    assert _cash(scenario)[1] == 4_000_000
    assert _lots(scenario, month=1) == {"stock": 550.0, "bond": 100.0}


def test_the_raise_comes_out_of_the_overweight_sleeve() -> None:
    """Water-filling, observed end to end. Stock is worth $90,000 and bonds $10,000 against
    equal weights, so the first $80,000 of any raise comes entirely from stock — the level
    where the two sleeves meet. The bond sleeve is untouched here, which is what
    "don't sell the underweight sleeve" means when it is not a slogan."""

    scenario = _scenario(opening_cash=0, floor=1_000, ceiling=30_000)

    assert _lots(scenario, month=1) == {"stock": 600.0, "bond": 100.0}


def test_the_band_is_measured_after_the_months_obligations() -> None:
    """The decision is made against the balance the month will END at, not the balance sitting
    there before the bills — which is what lets funding happen once a month like a person.

    Month 0 has no rent and $12,000 sits inside the band, so nothing happens. Month 1 brings
    $5,000 of rent: a policy reading the CURRENT balance sees $12,000, above the $10,000 floor,
    and sells nothing — leaving $7,000 after the rent, below the floor it was supposed to hold.
    Reading the PROJECTED balance sees $7,000 and raises to the $30,000 ceiling, so $23,000 is
    sold (230 units of the overweight stock) and the rent settles out of the refilled account.

    Asserted exactly rather than as "sold something": the wrong reading also sells on later
    months, so an inequality would pass against the defect this test exists to catch.
    """

    scenario = _scenario(opening_cash=12_000, floor=10_000, ceiling=30_000, rent=5_000)

    # Index N is the state ENTERING month N, so month 1's sale shows at index 2.
    assert _lots(scenario, month=1) == {"stock": 900.0, "bond": 100.0}
    assert _lots(scenario, month=2) == {"stock": 670.0, "bond": 100.0}
    assert _cash(scenario)[2] == 3_000_000
    assert rollout_status(_run(scenario)).get_column("status").to_list() == ["active"]


def test_a_sale_the_sleeves_cannot_cover_does_not_mint_money() -> None:
    """Asking for more than the portfolio holds drains it and stops. The cash tensor still
    conserves, which is the property that catches a disposal crediting proceeds with no
    matching debit — and it is the ONLY thing that sees it, since net worth stays correct
    when a lot leaves as its cash arrives."""

    scenario = _scenario(opening_cash=0, floor=1_000, ceiling=10_000_000)
    run = _run(scenario)
    state = np.asarray(run.output.state.cash, dtype=np.int64)
    totals = state.sum(axis=tuple(range(1, state.ndim)))

    assert np.all(totals == totals[0])
    assert _lots(scenario, month=1) == {"stock": 0.0, "bond": 0.0}


def test_a_sale_shows_up_as_a_lot_disposition() -> None:
    """A sale the ledger records but the disposition frame does not is a sale nobody can audit:
    cash and lots move, and the row explaining WHY is missing.

    The target-allocation policy needs its own disposition group rather than the liquidity one —
    the two policy kinds index their own dense rows, so a shared output row would have them writing
    over each other's policies. This asserts the row exists, is attributed to the selling agent
    and the sleeve's asset, and reconciles against the lots the run actually gave up.
    """

    scenario = _scenario(opening_cash=5_000, floor=10_000, ceiling=40_000)
    run = _run(scenario)
    rows = run.events_log.lot_dispositions.filter(pl.col("agent_id") == "alice").to_dicts()

    assert [str(row["lot_id"]) for row in rows] == ["stock"]
    assert str(rows[0]["asset_id"]) == "security:vti"
    assert float(rows[0]["units_sold"]) == 350.0
    assert int(rows[0]["proceeds_quanta"]) == 3_500_000
    assert int(rows[0]["cost_basis_consumed_quanta"]) == 3_500_000
    # The bond sleeve was never touched, so it must not appear at all — an over-broad decode
    # would emit a zero-unit row for it and the equality above is what refuses that.


def test_configuring_purchase_slots_changes_nothing_until_they_are_filled() -> None:
    """Slots are capacity, not behaviour. A policy given room to buy still holds only what it
    started with until something fills them, and the empty slots must not disturb the sale side:
    they join the same FIFO pool as the sleeve's real lots, so a slot that counted as a lot
    would shift what a sale reaches for.

    They sort LAST by construction rather than by luck — their FIFO rank is above every real
    month — which is what lets the sale order stay compile-time derivable once a policy starts
    filling them in months that differ per rollout.
    """

    without = _scenario(opening_cash=5_000, floor=10_000, ceiling=40_000)
    with_slots = without.model_copy(
        update={
            "target_allocation_policies": [
                without.target_allocation_policies[0].model_copy(update={"purchase_slots_per_sleeve": 3})
            ]
        }
    )

    lots = _lots(with_slots, month=1)

    assert {lot_id: q for lot_id, q in lots.items() if not lot_id.startswith("allocation_sale_buy")} == _lots(
        without, month=1
    )
    # Six slots, two sleeves by three, and every one of them still empty.
    assert sorted(lot_id for lot_id in lots if lot_id.startswith("allocation_sale_buy")) == [
        f"allocation_sale_buy_p0_s{sleeve}_{k}" for sleeve in (0, 1) for k in (0, 1, 2)
    ]
    assert all(q == 0.0 for lot_id, q in lots.items() if lot_id.startswith("allocation_sale_buy"))
    assert _cash(with_slots) == _cash(without)


def test_surplus_above_the_ceiling_is_invested_into_the_underweight_sleeve() -> None:
    """The buy side, end to end. $100,000 against a $10,000 floor and a $20,000 ceiling invests
    $90,000 — down to the FLOOR, not to the ceiling, for the same (s,S) reason a raise goes to
    the far edge.

    Where it goes is water-filling in reverse: stock is worth $90,000 and bonds $10,000 against
    equal weights, so the deposit levels them at $95,000 each — $85,000 into bonds and $5,000
    into stock. That is the asymmetry with the sale side made visible: a raise came entirely
    out of stock, and the deposit goes overwhelmingly the other way.
    """

    scenario = _scenario(opening_cash=100_000, floor=10_000, ceiling=20_000, purchase_slots=1)
    lots = _lots(scenario, month=1)

    assert lots["allocation_sale_buy_p0_s0_0"] == 50.0
    assert lots["allocation_sale_buy_p0_s1_0"] == 850.0
    # The holdings it started with are untouched: this month bought, it did not rebalance.
    assert lots["stock"] == 900.0
    assert lots["bond"] == 100.0
    # Exactly the floor. A quantum of overshoot would show here as $10,000 minus the overshoot,
    # which is the band spending money it promised to keep.
    assert _cash(scenario)[1] == 1_000_000


def test_a_purchase_records_the_price_its_rollout_paid() -> None:
    """Basis comes from the purchase, and it is not knowable at compile time: the slot carries
    whatever its own rollout paid the month it crossed the band. Reading the plan's static column
    would report 0, making the whole proceeds a gain on the eventual sale."""

    run = _run(_scenario(opening_cash=100_000, floor=10_000, ceiling=20_000, purchase_slots=1))
    bought = (
        asset_lots(run)
        .filter((pl.col("lot_id") == "allocation_sale_buy_p0_s1_0") & (pl.col("month_index") == 1))
        .to_dicts()[0]
    )

    assert int(bought["cost_basis_per_unit_quanta"]) == 10_000


def test_a_purchase_does_not_mint_or_burn_money() -> None:
    """The cash leg, checked the only way that catches a missing one: the cash tensor conserves.
    Net worth would look right either way — a lot arrives as its cash leaves — so a purchase that
    debited nobody would be invisible to every other assertion in this file."""

    scenario = _scenario(opening_cash=100_000, floor=10_000, ceiling=20_000, purchase_slots=1)
    state = np.asarray(_run(scenario).output.state.cash, dtype=np.int64)
    totals = state.sum(axis=tuple(range(1, state.ndim)))

    assert np.all(totals == totals[0])


def test_successive_purchases_fill_successive_slots() -> None:
    """The cursor. Each month's buy takes the next free slot, so two purchases are two lots with
    two purchase months — which is the whole reason a purchase cannot share a slot: they have
    different holding periods and would net to one wrong basis.

    Income arrives as an inflow, so the band only sees it the month AFTER it lands and the policy
    invests in months 2 and 3. Both go to bonds: at $10,000 against stock's $90,000, the bond
    sleeve is still underweight after both deposits.
    """

    scenario = _scenario(opening_cash=0, floor=0, ceiling=1_000, income=30_000, purchase_slots=2)
    lots = (
        asset_lots(_run(scenario))
        .filter(pl.col("lot_id").str.starts_with("allocation_sale_buy_p0_s1_") & (pl.col("month_index") == _HORIZON))
        .sort("lot_id")
        .to_dicts()
    )

    assert [float(row["remaining_quantity"]) for row in lots] == [300.0, 300.0]
    assert [int(row["purchase_month_index"]) for row in lots] == [2, 3]


def test_a_runtime_purchase_keeps_its_month_when_later_sold() -> None:
    """Disposition metadata comes from runtime lot state, not the slot's compile-time placeholder."""

    base = _scenario(
        opening_cash=0, floor=0, ceiling=1_000, stock_units=0, bond_units=0, income=30_000, purchase_slots=1
    )
    [income] = base.recurring_obligations
    scenario = base.model_copy(
        update={
            "recurring_obligations": [
                income.model_copy(update={"end_month": 1}),
                RecurringObligation(
                    start_month=3,
                    end_month=3,
                    obligation_id="one_time_rent",
                    obligation_type="rent",
                    agent_id="alice",
                    from_account_id="checking",
                    to_agent_id="landlord",
                    to_account_id="checking",
                    amount_due=FixedAmount(amount=10_000),
                ),
            ]
        }
    )

    rows = (
        _run(scenario)
        .events_log.lot_dispositions.filter(pl.col("lot_id").str.starts_with("allocation_sale_buy_"))
        .to_dicts()
    )

    assert rows
    assert {int(row["purchase_month_index"]) for row in rows} == {2}


def test_running_out_of_purchase_slots_aborts_the_run() -> None:
    """Aborting, not dropping the surplus purchase — and aborting the RUN, not failing the
    rollouts that hit the wall. Dropping it is a policy that silently stops investing partway
    through the horizon; failing only the affected rollouts drops exactly the paths that traded
    most, and since trading tracks volatility that biases what survives toward calm.

    Same scenario as the test above with one slot instead of two, so the second purchase has
    nowhere to go.
    """

    scenario = _scenario(opening_cash=0, floor=0, ceiling=1_000, income=30_000, purchase_slots=1)

    with pytest.raises(ValueError, match="ran out of purchase slots: 1 configured, 2 needed"):
        _run(scenario)


def test_a_drifted_portfolio_is_rebalanced_in_a_quiet_month() -> None:
    """The mechanism neither side of the band can express. Cash sits at $50,000 inside a
    [$10,000, $90,000] band, so nothing is being funded and nothing is being invested — and yet
    the portfolio is 9:1 against a 1:1 target.

    Without a tolerance this is exactly `test_a_month_inside_the_band_sells_nothing`. With one,
    $40,000 crosses: 400 units of stock sold and 400 units of bonds bought, landing both sleeves
    on $50,000. The sale and the purchase are two independent legs of the engine — the sell runs
    before settlement and the buy after — so this also pins that they meet in the same month.
    """

    scenario = _scenario(opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1, rebalance_tolerance=0.25)
    lots = _lots(scenario, month=1)

    assert lots["stock"] == 500.0
    assert lots["allocation_sale_buy_p0_s1_0"] == 400.0
    # Untouched: the bond sleeve was the underweight one, so the trim never reaches it.
    assert lots["bond"] == 100.0
    assert lots["allocation_sale_buy_p0_s0_0"] == 0.0
    # Cash-neutral to the cent. A rebalance is a portfolio operation, not a funding one.
    assert _cash(scenario)[1] == 5_000_000


def test_a_rebalanced_portfolio_then_sits_still() -> None:
    """One trigger, not one per month. Once both sleeves are on target the drift is zero, so a
    flat price path produces exactly one rebalance over the horizon — which is why a single
    purchase slot per sleeve is enough here, and why a policy that re-triggered every month
    would exhaust its slots and abort instead of quietly churning."""

    scenario = _scenario(opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1, rebalance_tolerance=0.25)
    run = _run(scenario)
    trades = run.events_log.lot_dispositions.filter(pl.col("agent_id") == "alice").to_dicts()

    assert [(str(row["lot_id"]), float(row["units_sold"])) for row in trades] == [("stock", 400.0)]
    assert _lots(scenario, month=_HORIZON) == _lots(scenario, month=1)


def test_a_tolerance_wider_than_the_drift_changes_nothing() -> None:
    """Configuring a rebalance is not asking for one. The fixture is 80% off target, so a 100%
    tolerance leaves it exactly where an unconfigured policy would."""

    with_tolerance = _scenario(
        opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1, rebalance_tolerance=1.0
    )
    without = _scenario(opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1)

    assert _lots(with_tolerance, month=_HORIZON) == _lots(without, month=_HORIZON)
    assert _cash(with_tolerance) == _cash(without)


def test_a_rebalance_does_not_mint_or_burn_money() -> None:
    """Two legs, two counterparties, one conserved tensor. A rebalance is the first month in
    which the agent both sells and buys, so it is the first chance for the sell leg's credit and
    the buy leg's debit to disagree."""

    scenario = _scenario(opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1, rebalance_tolerance=0.25)
    state = np.asarray(_run(scenario).output.state.cash, dtype=np.int64)
    totals = state.sum(axis=tuple(range(1, state.ndim)))

    assert np.all(totals == totals[0])


def test_rebalancing_without_somewhere_to_buy_is_rejected() -> None:
    """Config-time, because the alternative is a policy that only ever sells: every trigger
    would move the overweight sleeve into cash with no leg to put it back, draining the
    portfolio a little more each time it fires."""

    with pytest.raises(ValueError, match="no purchase slots"):
        _scenario(opening_cash=50_000, floor=10_000, ceiling=90_000, rebalance_tolerance=0.25)


def test_sweeping_sleeve_weights_does_not_recompile() -> None:
    """A sleeve weight is swept numeric config, so it must be TRACED, not part of the static key.

    It used to be folded into `_Static` through `_FoldedSleeve.weight`, which made every distinct
    weight vector its own XLA program: an eleven-point allocation sweep paid eleven full compiles,
    minutes apiece at a realistic path count, and that is what made a 2000-path sweep unrunnable.
    Nothing about a weight is a shape — only ratios matter, and the water-fill divides by
    `sum(weight)` at runtime.

    Asserted on JAX's own compile cache rather than wall time, which would be flaky.
    """

    _run(_scenario(opening_cash=50_000, floor=10_000, ceiling=90_000, weights=(1, 1)))
    warmed = _program_impl._cache_size()

    for weights in ((3, 7), (19, 81), (50, 50)):
        _run(_scenario(opening_cash=50_000, floor=10_000, ceiling=90_000, weights=weights))

    assert _program_impl._cache_size() == warmed, (
        "changing sleeve weights added a compiled variant, so weights are back in the static key"
    )


if __name__ == "__main__":
    pytest_bazel.main()
