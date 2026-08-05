"""The target-allocation policy, end to end through the engine.

The unit tests in `target_allocation_test.py` prove the policy's arithmetic. These prove the
ENGINE runs it: that the observation it builds is the agent's real state, that the orders
come back and are executed against real lots, and that the money moves with both legs.

Prices are pinned rather than sampled, so every number below is exact.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest_bazel

from finance.augur.model.deterministic import Deterministic
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.series import SecuritySymbol
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.product.asset_key import SecurityKey
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

_HORIZON = 4
_STOCK = SecurityKey(symbol=SecuritySymbol("vti"))
_BOND = SecurityKey(symbol=SecuritySymbol("bnd"))
_PRICE = 100.0
# Flat, so a sale's proceeds are exactly units x price and nothing drifts month to month.
_PATH = [_PRICE] * (_HORIZON + 1)


def _scenario(
    *,
    opening_cash: float,
    floor: float,
    ceiling: float,
    stock_units: float = 900.0,
    bond_units: float = 100.0,
    rent: float = 0.0,
) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=opening_cash),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="stock",
                agent_id="alice",
                account_id="checking",
                asset=_STOCK,
                quantity=stock_units,
                cost_basis_per_unit_usd=_PRICE,
                purchase_month_index=0,
            ),
            InitialLot(
                lot_id="bond",
                agent_id="alice",
                account_id="checking",
                asset=_BOND,
                quantity=bond_units,
                cost_basis_per_unit_usd=_PRICE,
                purchase_month_index=0,
            ),
        ],
        recurring_obligations=[
            RecurringObligation(
                start_month=1,
                obligation_id="rent",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=FixedAmount(amount_usd=rent),
            )
        ]
        if rent
        else [],
        target_allocation_policies=[
            TargetAllocationPolicy(
                agent_id="alice",
                account_id="checking",
                # Equal weights against a 9:1 holding, so the stock sleeve is the overweight one
                # and every sale must come out of it first.
                sleeves=[SleeveTarget(asset=_STOCK, weight=1), SleeveTarget(asset=_BOND, weight=1)],
                cash_floor_usd=floor,
                cash_ceiling_usd=ceiling,
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


def _cash(scenario: Scenario) -> list[float]:
    run = _run(scenario)
    return [
        float(v)
        for v in run.cash_balances.filter(pl.col("agent_id") == "alice")
        .sort("month_index")
        .get_column("balance_usd")
        .to_list()
    ]


def _lots(scenario: Scenario, *, month: int) -> dict[str, float]:
    run = _run(scenario)
    rows = run.asset_lots.filter(pl.col("month_index") == month).to_dicts()
    return {str(row["lot_id"]): float(row["remaining_quantity"]) for row in rows}


def test_a_month_inside_the_band_sells_nothing() -> None:
    """Drift alone never triggers a trade. The portfolio is 9:1 against a 1:1 target — as far
    from target as this scenario gets — and the policy still does nothing while cash sits
    inside the band. Rebalancing rides cashflow only."""

    scenario = _scenario(opening_cash=50_000.0, floor=10_000.0, ceiling=90_000.0)

    assert _lots(scenario, month=_HORIZON) == {"stock": 900.0, "bond": 100.0}
    assert _cash(scenario)[-1] == 50_000.0


def test_crossing_the_floor_refills_to_the_ceiling() -> None:
    """(s,S), through the engine. Cash below the floor is raised to the CEILING, not back to
    the floor — refilling to the floor would put the agent back at its trigger next month,
    making it a forced seller into every dip.

    $5,000 with a $10,000 floor and a $40,000 ceiling raises $35,000, which at $100/unit is
    350 units out of the overweight stock sleeve.
    """

    scenario = _scenario(opening_cash=5_000.0, floor=10_000.0, ceiling=40_000.0)

    assert _cash(scenario)[1] == 40_000.0
    assert _lots(scenario, month=1) == {"stock": 550.0, "bond": 100.0}


def test_the_raise_comes_out_of_the_overweight_sleeve() -> None:
    """Water-filling, observed end to end. Stock is worth $90,000 and bonds $10,000 against
    equal weights, so the first $80,000 of any raise comes entirely from stock — the level
    where the two sleeves meet. The bond sleeve is untouched here, which is what
    "don't sell the underweight sleeve" means when it is not a slogan."""

    scenario = _scenario(opening_cash=0.0, floor=1_000.0, ceiling=30_000.0)

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

    scenario = _scenario(opening_cash=12_000.0, floor=10_000.0, ceiling=30_000.0, rent=5_000.0)

    # Index N is the state ENTERING month N, so month 1's sale shows at index 2.
    assert _lots(scenario, month=1) == {"stock": 900.0, "bond": 100.0}
    assert _lots(scenario, month=2) == {"stock": 670.0, "bond": 100.0}
    assert _cash(scenario)[2] == 30_000.0
    assert _run(scenario).rollout_status.get_column("status").to_list() == ["active"]


def test_a_sale_the_sleeves_cannot_cover_does_not_mint_money() -> None:
    """Asking for more than the portfolio holds drains it and stops. The cash tensor still
    conserves, which is the property that catches a disposal crediting proceeds with no
    matching debit — and it is the ONLY thing that sees it, since net worth stays correct
    when a lot leaves as its cash arrives."""

    scenario = _scenario(opening_cash=0.0, floor=1_000.0, ceiling=10_000_000.0)
    run = _run(scenario)
    state = np.asarray(run.buffers.state.cash_state, dtype=np.int64)
    totals = state.sum(axis=tuple(range(1, state.ndim)))

    assert np.all(totals == totals[0])
    assert _lots(scenario, month=1) == {"stock": 0.0, "bond": 0.0}


if __name__ == "__main__":
    pytest_bazel.main()
