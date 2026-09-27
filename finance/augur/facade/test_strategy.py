"""A strategy refuses every decision left unmade, and lowers to a household trading exactly as a hand-built one."""

from collections.abc import Callable
from fractions import Fraction
from itertools import combinations
from typing import Any

import pytest
import pytest_bazel

from finance.augur.facade.strategy import (
    AccumulateCash,
    CashBand,
    CashflowOnly,
    DriftBand,
    ManagedSleeve,
    Reinvestment,
    ReinvestSurplus,
    SecuritySleeve,
    Strategy,
    TargetAllocation,
    lower,
)
from finance.augur.policy import cash_band_household
from finance.augur.sim.actions import Action
from finance.augur.sim.bills import Biller
from finance.augur.sim.books import AccountRef
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId, PortfolioId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedHoldingPool,
    PreparedLot,
    PreparedRecurringObligation,
    PreparedSeries,
    PreparedTlhPortfolio,
)
from finance.augur.sim.tlh import TlhAssumptions, TlhOpeningCohort
from finance.augur.sim.world import World

OWNER = AgentId("test-owner")
CHECKING = AccountId("test-checking")
BROKERAGE = AccountId("test-brokerage")
PAYEE = AccountRef(agent_id=AgentId("test-payee"), account_id=AccountId("test-checking"))
GROWTH, LEGACY, STEADY, INDEX = (AssetId(f"test-{name}") for name in ("growth", "legacy", "steady", "index"))
MANAGED = PortfolioId("test-managed")
HORIZON = 5
# No modeled harvest: the managed sleeve's value moves only with its index and its own trades.
QUIET = TlhAssumptions(
    peak_annual_yield=0, floor_annual_yield=0, maturity_decay_exponent=1, drawdown_sensitivity=0, short_term_fraction=1
)
BAND = CashBand(floor=1_000, ceiling=2_000)
# Weights over different denominators, so only scaling by the common one gives the policy's 3 : 2 : 1.
ALLOCATION = TargetAllocation(
    weights={
        SecuritySleeve(asset_id=GROWTH): Fraction(1, 2),
        SecuritySleeve(asset_id=STEADY): Fraction(1, 3),
        ManagedSleeve(portfolio_id=MANAGED): Fraction(1, 6),
        SecuritySleeve(asset_id=LEGACY): Fraction(0),
    }
)
# Each reinvestment declaration beside the `reinvest` a hand-built household sets for it.
CHOICES: list[tuple[Reinvestment, cash_band_household.Reinvest | None]] = [
    (
        ReinvestSurplus(rebalancing=DriftBand(tolerance_ppb=250_000_000)),
        cash_band_household.Reinvest(rebalance_tolerance_ppb=250_000_000),
    ),
    (ReinvestSurplus(rebalancing=CashflowOnly()), cash_band_household.Reinvest(rebalance_tolerance_ppb=None)),
    (AccumulateCash(reason="test: surplus stays idle"), None),
]


def securities(*weights: object) -> dict[SecuritySleeve, object]:
    return {SecuritySleeve(asset_id=AssetId(f"test-{index}")): weight for index, weight in enumerate(weights)}


@pytest.mark.parametrize(
    ("declare", "arguments", "error", "match"),
    [
        pytest.param(TargetAllocation, {"weights": {}}, ValueError, "names nothing", id="empty_weights"),
        pytest.param(
            TargetAllocation, {"weights": securities(Fraction(0), Fraction(0))}, ValueError, "exactly 1", id="all_zero"
        ),
        pytest.param(
            TargetAllocation,
            {"weights": securities(Fraction(1, 2), Fraction(1, 3))},
            ValueError,
            "exactly 1",
            id="short_of_one",
        ),
        pytest.param(
            TargetAllocation,
            {"weights": securities(Fraction(3, 2), Fraction(-1, 2))},
            ValueError,
            "nonnegative",
            id="negative_weight",
        ),
        # These binary floats sum to exactly 1, yet neither is the weight its author meant.
        pytest.param(TargetAllocation, {"weights": securities(0.6, 0.4)}, TypeError, "exact Fraction", id="floats"),
        pytest.param(ReinvestSurplus, {}, TypeError, "rebalancing", id="rebalancing_omitted"),
        pytest.param(ReinvestSurplus, {"rebalancing": None}, TypeError, "rebalancing", id="rebalancing_none"),
        pytest.param(DriftBand, {"tolerance_ppb": -1}, ValueError, "nonnegative", id="negative_tolerance"),
        pytest.param(DriftBand, {"tolerance_ppb": 0.25}, TypeError, "parts per billion", id="fractional_tolerance"),
        pytest.param(
            Strategy, {"allocation": ALLOCATION, "cash_band": BAND}, TypeError, "reinvestment", id="reinvest_omitted"
        ),
        pytest.param(
            Strategy,
            {"allocation": ALLOCATION, "cash_band": BAND, "reinvestment": None},
            TypeError,
            "reinvestment",
            id="reinvest_none",
        ),
        pytest.param(AccumulateCash, {"reason": " "}, ValueError, "reason", id="blank_reason"),
        pytest.param(CashBand, {"floor": 2, "ceiling": 1}, ValueError, "must not exceed", id="inverted_band"),
        pytest.param(CashBand, {"floor": -1, "ceiling": 1}, ValueError, "must not be negative", id="negative_floor"),
        pytest.param(CashBand, {"floor": 0, "ceiling": 1.5}, TypeError, "integer currency quanta", id="float_ceiling"),
    ],
)
def test_a_decision_left_unmade_or_malformed_is_refused(
    declare: Callable[..., object], arguments: dict[str, Any], error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        declare(**arguments)


def compose() -> World:
    """5,000 in cash beside 30 growth, 20 steady and 10 legacy units and a 1,000 managed portfolio.

    A 500 bill falls due every month and a 4,000 one in month 3. Growth doubles in month 2,
    so the next month the band holds has drift to trade.
    """
    prices = {GROWTH: (100, 100, 200, 200, 200, 200), STEADY: (100,) * 6, LEGACY: (50,) * 6, INDEX: (100,) * 6}
    world = World(
        MarketPath(
            [
                PreparedSeries(series_id=f"security:{asset_id}", snapshots=HORIZON + 1, values=path)
                for asset_id, path in prices.items()
            ],
            0,
            rollout_count=1,
        ),
        horizon_months=HORIZON,
    )
    for account, opening in (
        (AccountRef(agent_id=OWNER, account_id=CHECKING), 5_000),
        (AccountRef(agent_id=OWNER, account_id=BROKERAGE), 0),
        (PAYEE, 0),
    ):
        world.declare_account(PreparedAccount(account=account, opening_balance=opening))
    for asset_id, units in ((GROWTH, 30), (STEADY, 20), (LEGACY, 10)):
        world.declare_pool(
            PreparedHoldingPool(agent_id=OWNER, account_id=BROKERAGE, asset_id=asset_id, quantity_scale=1)
        )
        world.hold(
            PreparedLot(
                lot_id=LotId(f"opening-{asset_id}"),
                agent_id=OWNER,
                account_id=BROKERAGE,
                asset_id=asset_id,
                purchase_month=-24,
                quantity_scale=1,
                units=units,
                basis=units * 50,
            )
        )
    world.declare_portfolio(
        PreparedTlhPortfolio(
            portfolio_id=MANAGED,
            owner_agent_id=OWNER,
            account_id=BROKERAGE,
            asset_id=INDEX,
            initial_cohorts=(TlhOpeningCohort(value=1_000, cost_basis=1_000, purchase_month_index=-24),),
            assumptions=QUIET,
        )
    )
    for obligation_id, amount, (start, end) in (("test-monthly", 500, (0, None)), ("test-lump", 4_000, (3, 3))):
        world.track(
            Biller(
                PreparedRecurringObligation(
                    start_month=start,
                    end_month=end,
                    obligation_id=obligation_id,
                    obligation_type="cash_spend",
                    from_account=AccountRef(agent_id=OWNER, account_id=CHECKING),
                    to_account=PAYEE,
                    amount_due=amount,
                    property_id=None,
                    deduction_category=None,
                    deductible_fraction_ppb=1_000_000_000,
                )
            )
        )
    return world


def trades(household: cash_band_household.CashBandHousehold) -> list[list[Action]]:
    """Each month's actions, in the order the world executed them."""
    world = compose()
    household.check(world)
    world.track(household)
    world.start()
    months = []
    while not world.finished:
        world.step()
        months.append([receipt.action for receipt in world.previous_receipts])
    assert world.failed_month is None
    return months


def declared(reinvestment: Reinvestment) -> cash_band_household.CashBandHousehold:
    return lower(
        Strategy(allocation=ALLOCATION, reinvestment=reinvestment, cash_band=BAND),
        agent_id=OWNER,
        cash_account_id=CHECKING,
        source_account_ids=(BROKERAGE,),
        cause_id_prefix="test",
    )


def hand_built(reinvest: cash_band_household.Reinvest | None) -> cash_band_household.CashBandHousehold:
    """The declared household written against the policy, its sleeves in the lowering's canonical order."""
    return cash_band_household.CashBandHousehold(
        OWNER,
        cash_account_id=CHECKING,
        floor=1_000,
        ceiling=2_000,
        sleeves=(
            cash_band_household.SecuritySleeve(asset_id=GROWTH, weight=3),
            cash_band_household.SecuritySleeve(asset_id=LEGACY, weight=0),
            cash_band_household.SecuritySleeve(asset_id=STEADY, weight=2),
            cash_band_household.ManagedSleeve(portfolio_id=MANAGED, weight=1),
        ),
        source_account_ids=(BROKERAGE,),
        reinvest=reinvest,
        cause_id_prefix="test",
    )


def test_a_declared_strategy_trades_as_the_equivalent_hand_built_household() -> None:
    runs = [(trades(declared(choice)), trades(hand_built(reinvest))) for choice, reinvest in CHOICES]
    for lowered, expected in runs:
        assert lowered == expected
    # Every choice trades differently on this path, so none of those equalities holds by accident.
    assert all(first != second for (first, _), (second, _) in combinations(runs, 2))


if __name__ == "__main__":
    pytest_bazel.main()
